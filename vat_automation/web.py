from __future__ import annotations

import argparse
import asyncio
import atexit
import hmac
import json
import math
import queue
import secrets
import shutil
import sys
import tempfile
import threading
import traceback
import uuid
import webbrowser
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from . import tls
from .auth import (
    USERNAME_PATTERN,
    LoginThrottle,
    SessionSigner,
    UserStore,
    normalize_username,
)
from .config import (
    DEFAULT_IDENTITY_DOCUMENTS,
    Settings,
    load_settings,
)
from .credential_store import CredentialStore
from .document_parser import (
    extract_document,
    prepare_document_values,
)
from .envfile import (
    default_env_path,
    env_credentials,
    load_env_file,
)
from .runner import AutomationStopped, VatAutomation, is_skip_edit_action


ALLOWED_ENV_KEYS = {
    "HMRC_EMAIL",
    "HMRC_PASSWORD",
    "HMRC_USER_ID",
    "HMRC_FULL_NAME",
    "HMRC_SIGN_IN_METHOD",
    "HMRC_IS_TAX_AGENT",
    "HMRC_ACCESS_AS_BUSINESS",
    "HMRC_MFA_METHOD",
    "HMRC_MFA_PHONE",
    "HMRC_MFA_PHONE_IS_UK",
    "HMRC_MFA_PHONE_COUNTRY",
}
ACTIVE_STATES = {
    "starting", "running", "waiting_code", "pausing", "paused", "reviewing",
    "editing",
}
SESSION_COOKIE = "vat_session"
CSRF_COOKIE = "vat_csrf"
CSRF_HEADER = "x-csrf-token"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
# 暂停和最终复核都会挂起自动化线程并让 Chrome 一直开着，必须有上限。
PAUSE_TIMEOUT_SECONDS = 30 * 60
FINAL_REVIEW_TIMEOUT_SECONDS = 30 * 60
STATIC_DIR = Path(__file__).resolve().parent / "static"


class StartRequest(BaseModel):
    # 配置文件路径由服务端启动参数固定，不接受客户端指定。
    model_config = ConfigDict(extra="forbid")

    fresh_session: bool = True
    resume: bool = False
    credentials: dict[str, str] = Field(default_factory=dict)
    extracted_values: dict[str, str] = Field(default_factory=dict)
    extracted_confirmed: bool = False


class CodeRequest(BaseModel):
    code: str = Field(min_length=1, max_length=32)


class CredentialSaveRequest(BaseModel):
    # 保存前允许手改的字段（例如核对/修正 Gateway User ID）。
    credentials: dict[str, str] = Field(default_factory=dict)
    # 该客户的资料袋里解析出来的项目编号；用来当存储键，和 EORI 那轮对齐。
    project_code: str = Field(default="", max_length=64)


class ContinueRequest(BaseModel):
    extracted_values: dict[str, str] = Field(default_factory=dict)
    extracted_confirmed: bool = False


class FinalSubmitRequest(BaseModel):
    confirmed: bool = False


class FinalEditRequest(BaseModel):
    target: str = Field(min_length=1, max_length=64)


class RemoteEditSubmitRequest(BaseModel):
    answers: dict[str, str | bool | list[str]] = Field(default_factory=dict)
    action: str = Field(default="", max_length=160)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class SetupRequest(BaseModel):
    token: str = Field(min_length=1, max_length=128)
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class UserCreateRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)
    admin: bool = False


class UserNameRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)


@dataclass
class UserSession:
    """一个同事的独立工作区。证件、状态、凭据都不与其他人共享。"""

    username: str
    upload_dir: Path
    state: dict[str, Any]
    code_queue: queue.Queue[str] = field(default_factory=lambda: queue.Queue(maxsize=1))
    pause_requested: threading.Event = field(default_factory=threading.Event)
    resume_event: threading.Event = field(default_factory=threading.Event)
    review_event: threading.Event = field(default_factory=threading.Event)
    edit_event: threading.Event = field(default_factory=threading.Event)
    pending_extracted_values: dict[str, str] = field(default_factory=dict)
    pending_edit_answers: dict[str, str | bool | list[str]] = field(default_factory=dict)
    pending_edit_action: str = ""
    identity_documents: list[Path] = field(default_factory=list)
    active_identity_documents: list[Path] = field(default_factory=list)
    final_review: dict[str, Any] = field(default_factory=dict)
    final_review_edit: dict[str, Any] = field(default_factory=dict)
    # 登录信息只留在进程内存里，方便失败后重试；重启服务即失效，绝不落盘。
    saved_credentials: dict[str, str] = field(default_factory=dict)
    # 上一次运行实际使用的登录信息，以及新建账号拿到的 Gateway User ID。
    last_credentials: dict[str, str] = field(default_factory=dict)
    gateway_user_id: str = ""
    # 凭据存储的键：优先用资料里的项目编号，保证按客户对齐；没有则退回登录账号。
    credential_key: str = ""
    # 进程内存里记住的凭据属于哪个客户；换客户时不沿用。
    saved_credentials_key: str = ""
    review_action: str = ""
    review_target: str = ""
    thread: threading.Thread | None = None


def _initial_state() -> dict[str, Any]:
    return {
        "status": "idle",
        "message": "尚未启动",
        "heading": "",
        "url": "",
        "events": [],
        "identity_documents": 0,
    }


class JobManager:
    def __init__(
        self,
        config_path: Path,
        env_path: Path | None = None,
        credential_store: CredentialStore | None = None,
    ) -> None:
        self.config_path = config_path
        # 默认凭据来源：项目 .env（本机 0600，已忽略），网页留空时自动使用。
        self.env_path = (env_path or default_env_path()).expanduser().resolve()
        # 主要存储：按账号保存的 HMRC 登录信息（0600，git 忽略）。
        self.credential_store = credential_store or CredentialStore(
            Path("hmrc-credentials.json")
        )
        self._lock = threading.Lock()
        self._sessions: dict[str, UserSession] = {}
        self._busy_with: str | None = None
        # "最近操作"在内存里为空时，用审计日志的最近记录兜底（带文件变化缓存）。
        self._events_cache: list[dict[str, Any]] = []
        self._events_cache_key: tuple[str, int, int, str, int] | None = None
        # 流程名和身份证明数量都由流程配置决定（VAT 三份；EORI 不需要上传，为 0）。
        self.flow_name = ""
        self.default_sign_in_method = ""
        self.identity_documents_required = DEFAULT_IDENTITY_DOCUMENTS
        self._refresh_config()
        self._upload_root = Path(tempfile.mkdtemp(prefix="uk-vat-private-uploads-"))
        self._upload_root.chmod(0o700)
        atexit.register(shutil.rmtree, self._upload_root, True)

    def env_credential_keys(self) -> list[str]:
        """`.env`/进程环境里可用的凭据键名（只返回名字，不返回值）。"""
        return sorted(env_credentials(ALLOWED_ENV_KEYS))

    def recent_audit_events(self, username: str, limit: int = 20) -> list[dict[str, Any]]:
        """服务重启/新任务开始后，用审计日志里的最近记录填充"最近操作"。

        审计里只记事件、URL 和页面标题，不含填写值，所以可以安全回显。
        """
        try:
            settings = load_settings(self.config_path)
            path = settings.artifacts_dir / "audit.jsonl"
            stat = path.stat()
        except (OSError, ValueError):
            return []
        cache_key = (str(path), stat.st_mtime_ns, stat.st_size, username, limit)
        with self._lock:
            if self._events_cache_key == cache_key:
                return list(self._events_cache)
            self._events_cache_key = cache_key
            self._events_cache = []
        events: list[dict[str, Any]] = []
        try:
            with path.open("r", encoding="utf-8") as stream:
                for line in stream:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if record.get("user") != username:
                        continue
                    events.append(
                        {
                            "event": str(record.get("event", "")),
                            "heading": str(record.get("heading", "")),
                            "text": str(record.get("text", "")),
                        }
                    )
        except OSError:
            return []
        events = events[-limit:]
        with self._lock:
            self._events_cache = list(events)
        return list(events)

    @staticmethod
    def credential_key_for(session: UserSession, extracted_values: Mapping[str, str]) -> str:
        """凭据按客户对齐：优先项目编号，其次登录账号。"""
        project_code = str(extracted_values.get("project_code", "")).strip()
        return project_code or session.username

    @staticmethod
    def _session_credential_key(session: UserSession) -> str:
        return session.credential_key or session.username

    def _refresh_config(self) -> None:
        """把配置里的流程名、身份证明数量读进来，供界面显示和启动校验。"""
        try:
            settings = load_settings(self.config_path)
        except (OSError, ValueError):
            # 配置尚未就绪或不可解析时保持默认值，真正启动时会再报错。
            return
        self.flow_name = settings.flow_name
        self.default_sign_in_method = settings.default_sign_in_method
        self.identity_documents_required = settings.identity_documents_required

    def session(self, username: str) -> UserSession:
        name = normalize_username(username)
        if not USERNAME_PATTERN.match(name):
            raise ValueError(f"非法用户名：{username}")
        with self._lock:
            existing = self._sessions.get(name)
            if existing is not None:
                return existing
            upload_dir = self._upload_root / name
            upload_dir.mkdir(parents=True, exist_ok=True)
            upload_dir.chmod(0o700)
            created = UserSession(
                username=name, upload_dir=upload_dir, state=_initial_state()
            )
            self._sessions[name] = created
            return created

    def snapshot(self, username: str) -> dict[str, Any]:
        session = self.session(username)
        with self._lock:
            busy = self._busy_with
            events = list(session.state["events"])
            payload = {
                **session.state,
                "events": events,
                "identity_documents": len(session.identity_documents),
                "identity_required": self.identity_documents_required,
                "credentials_saved": bool(session.saved_credentials)
                and session.saved_credentials_key == session.credential_key,
                "credentials_env": self.env_credential_keys(),
                "credentials_stored": self.credential_store.public(
                    self._session_credential_key(session)
                ),
                "credential_key": self._session_credential_key(session),
                "gateway_user_id": session.gateway_user_id,
                "env_path": str(self.env_path),
                "flow_name": self.flow_name,
                "sign_in_method": self.default_sign_in_method,
                "final_review": {
                    "available": bool(session.final_review),
                    "pdf": bool(session.final_review.get("pdf")),
                    "png": bool(session.final_review.get("png")),
                    "editable": bool(session.final_review.get("editable")),
                    "changes": list(session.final_review.get("changes", [])),
                    "edit": dict(session.final_review_edit),
                },
                "user": session.username,
                "busy_with": busy if busy and busy != session.username else "",
                "config": self.config_path.name,
            }
        # 内存里还没有事件（刚重启或还没跑过任务）时，用审计日志兜底显示。
        # 注意要放在锁外面调用，recent_audit_events 自己会加锁。
        if not events:
            payload["events"] = self.recent_audit_events(username)
        return payload

    def start(self, username: str, request: StartRequest) -> None:
        session = self.session(username)
        if not self.config_path.is_file():
            raise ValueError(f"服务端配置文件不存在：{self.config_path}")
        self._refresh_config()
        if request.fresh_session and request.resume:
            raise ValueError("全新会话不能同时启用断点恢复。")
        if not request.extracted_confirmed:
            raise ValueError("请先在网页中检查并确认文档提取结果。")
        if (
            self.identity_documents_required
            and len(session.identity_documents) != self.identity_documents_required
        ):
            raise ValueError(
                f"请先保存正好 {self.identity_documents_required} 份身份证明文件。"
            )

        provided = {
            key: value.strip()
            for key, value in request.credentials.items()
            if key in ALLOWED_ENV_KEYS and value.strip()
        }
        with self._lock:
            if session.state["status"] in ACTIVE_STATES:
                raise RuntimeError("你已有自动化任务正在运行。")
            if self._busy_with is not None and self._busy_with != session.username:
                raise RuntimeError(
                    f"当前有 {self._busy_with} 的任务在运行，请等待其结束后再开始。"
                )
            # 只补缺的键，优先级：网页填写 > 该客户存储 > 本机内存记住 > .env/进程环境。
            session.credential_key = self.credential_key_for(
                session, request.extracted_values
            )
            stored = self.credential_store.get(session.credential_key)
            if not stored and session.credential_key != session.username:
                stored = self.credential_store.get(session.username)
            # 内存里记住的凭据只在同一客户内复用，避免张冠李戴。
            remembered = (
                dict(session.saved_credentials)
                if session.saved_credentials_key == session.credential_key
                else {}
            )
            env_values = env_credentials(ALLOWED_ENV_KEYS)
            credentials = {**env_values, **remembered, **stored, **provided}
            # 该客户已经存过 Government Gateway 账号时默认用它登录（建号那一步已经做过），
            # 表单或 .env 显式指定登录方式时以它们为准。
            if (
                "HMRC_SIGN_IN_METHOD" not in provided
                and "HMRC_SIGN_IN_METHOD" not in env_values
                and credentials.get("HMRC_USER_ID")
            ):
                credentials["HMRC_SIGN_IN_METHOD"] = "Government Gateway"
            if provided or stored or remembered:
                session.saved_credentials = dict(credentials)
                session.saved_credentials_key = session.credential_key
            session.last_credentials = dict(credentials)
            session.gateway_user_id = ""
            self._busy_with = session.username
            session.state = {
                **_initial_state(),
                "status": "starting",
                "message": "正在加载配置并启动浏览器",
            }
            session.active_identity_documents = list(session.identity_documents)
            session.final_review = {}
            session.pause_requested.clear()
            session.resume_event.clear()
            session.review_event.clear()
            session.edit_event.clear()
            session.review_action = ""
            session.review_target = ""
            session.pending_extracted_values = {}
            session.pending_edit_answers = {}
            session.pending_edit_action = ""
            session.final_review_edit = {}
            while not session.code_queue.empty():
                try:
                    session.code_queue.get_nowait()
                except queue.Empty:
                    break
            session.thread = threading.Thread(
                target=self._run,
                args=(
                    session,
                    credentials,
                    dict(request.extracted_values),
                    request.fresh_session,
                    request.resume,
                ),
                daemon=True,
                name=f"vat-automation-{session.username}",
            )
            session.thread.start()

    def submit_code(self, username: str, code: str) -> None:
        session = self.session(username)
        clean = code.strip()
        with self._lock:
            if session.state["status"] != "waiting_code":
                raise RuntimeError("当前任务没有等待验证码。")
            try:
                session.code_queue.put_nowait(clean)
            except queue.Full as exc:
                raise RuntimeError("验证码已经提交，请等待页面处理。") from exc
            session.state["status"] = "running"
            session.state["message"] = "验证码已收到，正在继续"

    def clear_credentials(self, username: str) -> None:
        """清掉该账号的登录信息存储与本机内存记录（换账号时手动清理）。"""
        session = self.session(username)
        with self._lock:
            if session.state["status"] in ACTIVE_STATES:
                raise RuntimeError("自动化运行中不能清除登录信息。")
            keys = {self._session_credential_key(session), session.username}
            session.saved_credentials = {}
            session.saved_credentials_key = ""
            session.last_credentials = {}
            session.gateway_user_id = ""
        for key in keys:
            self.credential_store.clear(key)

    def _persist_credentials(self, session: UserSession) -> None:
        """跑到建号成功后自动把 Government Gateway 账号存到本机对应客户名下。"""
        values = self.last_run_credentials(session.username)
        if not values.get("HMRC_USER_ID") or not values.get("HMRC_PASSWORD"):
            return
        try:
            self.credential_store.save(self._session_credential_key(session), values)
        except (OSError, ValueError):
            # 自动保存失败不影响流程结果，网页上还能手动保存。
            return
        session.saved_credentials = dict(values)
        session.saved_credentials_key = self._session_credential_key(session)

    def remember_gateway_user_id(self, session: UserSession, user_id: str) -> None:
        """新建 Government Gateway 账号后立刻记下并落库，中途中断也不丢账号。"""
        session.gateway_user_id = str(user_id).strip()
        self._persist_credentials(session)

    def last_run_credentials(self, username: str) -> dict[str, str]:
        """上一次运行实际使用的登录信息（含新建的 Gateway User ID）。

        只给服务端内部使用（例如工作台按客户落库），不经过状态接口。
        """
        session = self.session(username)
        with self._lock:
            values = dict(session.last_credentials)
            if session.gateway_user_id:
                values["HMRC_USER_ID"] = session.gateway_user_id
        return {key: value for key, value in values.items() if str(value).strip()}

    def save_credentials(
        self,
        username: str,
        overrides: Mapping[str, str] | None = None,
        key: str = "",
    ) -> dict[str, str]:
        """把上次运行实际使用的登录信息（含新建的 Gateway User ID）存到本机。"""
        session = self.session(username)
        # 注意：last_run_credentials 自己会加锁，这里不能再持锁，否则死锁。
        if session.state["status"] in ACTIVE_STATES:
            raise RuntimeError("自动化运行中不能保存登录信息。")
        store_key = str(key).strip() or self._session_credential_key(session)
        values = {
            **self.last_run_credentials(username),
            **{
                key: str(value).strip()
                for key, value in dict(overrides or {}).items()
                if str(value).strip()
            },
        }
        if not values.get("HMRC_PASSWORD"):
            raise ValueError("还没有可保存的密码：请先填一次密码并启动过任务。")
        try:
            record = self.credential_store.save(store_key, values)
        except OSError as exc:
            raise RuntimeError(
                f"无法写入 {self.credential_store.path}：{exc}"
            ) from exc
        session.saved_credentials = dict(values)
        session.saved_credentials_key = store_key
        session.credential_key = store_key
        return {
            "status": "saved",
            "path": str(self.credential_store.path),
            "key": store_key,
            "credentials": self.credential_store.public(store_key),
            "updated_at": str(record.get("updated_at", "")),
        }

    def _record_failure(
        self,
        session: UserSession,
        event: str,
        message: str,
        exc: BaseException | None = None,
    ) -> None:
        """把启动/运行失败写进审计日志和终端，避免只留在网页上看不到。"""
        record = {
            "time": datetime.now(UTC).isoformat(),
            "event": event,
            "user": session.username,
            "message": message,
        }
        if exc is not None:
            record["error"] = f"{type(exc).__name__}: {exc}"
            traceback.print_exception(
                type(exc), exc, exc.__traceback__, file=sys.stderr
            )
        else:
            print(f"[自动化失败] {session.username}：{message}", file=sys.stderr)
        try:
            settings = load_settings(self.config_path)
            artifacts = settings.artifacts_dir
        except (OSError, ValueError):
            return
        try:
            artifacts.mkdir(parents=True, exist_ok=True)
            with (artifacts / "audit.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            return

    def pause(self, username: str) -> None:
        session = self.session(username)
        with self._lock:
            if session.state["status"] != "running":
                raise RuntimeError("只能在自动化正在运行时暂停。")
            session.pause_requested.set()
            session.state["status"] = "pausing"
            session.state["message"] = "正在等待当前页面操作到达安全暂停点"

    def continue_after_pause(self, username: str, request: ContinueRequest) -> None:
        session = self.session(username)
        if not request.extracted_confirmed:
            raise ValueError("修改资料后请先点击“确认资料无误”。")
        with self._lock:
            if session.state["status"] != "paused":
                raise RuntimeError("当前任务未处于已暂停状态。")
            session.pending_extracted_values = dict(request.extracted_values)
            session.state["message"] = "正在应用修改后的资料并继续"
            session.resume_event.set()

    def confirm_final_review(self, username: str) -> None:
        session = self.session(username)
        with self._lock:
            if session.state["status"] != "reviewing":
                raise RuntimeError("当前任务未处于待人工核对状态。")
            session.review_action = "submit"
            session.state["message"] = (
                "已收到人工提交确认，正在点击 HMRC 的提交按钮"
            )
            session.review_event.set()

    def request_final_review_edit(self, username: str, target: str) -> None:
        session = self.session(username)
        with self._lock:
            if session.state["status"] != "reviewing":
                raise RuntimeError("当前任务未处于待人工核对状态。")
            if not session.final_review.get("editable"):
                raise RuntimeError("HMRC 最终核对页没有可远程修改的 Change 项。")
            changes = session.final_review.get("changes", [])
            if target not in {str(item.get("id", "")) for item in changes}:
                raise RuntimeError("选择的最终核对项目不存在或已经失效。")
            session.review_action = "edit"
            session.review_target = target
            session.state["status"] = "editing"
            session.state["message"] = "正在打开所选 HMRC 修改页面"
            session.review_event.set()

    def submit_remote_edit(
        self, username: str, request: RemoteEditSubmitRequest
    ) -> None:
        session = self.session(username)
        with self._lock:
            if session.state["status"] != "editing":
                raise RuntimeError("当前任务未处于远程修改状态。")
            if not session.final_review_edit.get("available"):
                raise RuntimeError("HMRC 修改表单尚未准备好，请稍后重试。")
            fields = session.final_review_edit.get("fields", [])
            allowed_keys = {str(item.get("key", "")) for item in fields}
            if set(request.answers) - allowed_keys:
                raise ValueError("提交内容包含当前 HMRC 页面不存在的字段。")
            actions = [str(item) for item in session.final_review_edit.get("actions", [])]
            action = request.action or (actions[0] if actions else "")
            if action not in actions:
                raise ValueError("请选择当前 HMRC 页面提供的继续操作。")
            if not is_skip_edit_action(action):
                for field in fields:
                    if not field.get("required"):
                        continue
                    key = str(field.get("key", ""))
                    value = request.answers.get(key)
                    if value is None or value == "" or value == []:
                        raise ValueError(f"请填写必填项：{field.get('label') or key}")
                    if field.get("kind") == "checkbox" and value is not True:
                        raise ValueError(f"请勾选必填项：{field.get('label') or key}")
            session.pending_edit_answers = dict(request.answers)
            session.pending_edit_action = action
            session.final_review_edit = {"available": False}
            session.state["message"] = "正在把远程修改应用到 HMRC 页面"
            session.edit_event.set()

    def final_review_document(self, username: str, kind: str) -> Path | None:
        """返回最终复核产物的路径。路径只从服务端状态取，客户端无法指定。"""
        session = self.session(username)
        with self._lock:
            raw = session.final_review.get(kind, "")
        if not raw:
            return None
        candidate = Path(raw)
        return candidate if candidate.is_file() else None

    def store_identity_documents(
        self,
        username: str,
        documents: list[tuple[str, bytes]],
        expected: int = 3,
    ) -> list[str]:
        allowed = {
            ".jpg", ".jpeg", ".bmp", ".png", ".pdf", ".doc", ".docx",
            ".xls", ".xlsx", ".gif", ".txt",
        }
        if len(documents) != expected:
            raise ValueError(f"请一次选择 {expected} 份身份证明文件。")
        session = self.session(username)
        saved: list[Path] = []
        with self._lock:
            if session.state["status"] in ACTIVE_STATES:
                raise RuntimeError("自动化运行中不能替换身份证明文件。")
            try:
                for original_name, content in documents:
                    suffix = Path(original_name).suffix.casefold()
                    if suffix not in allowed:
                        raise ValueError(f"不支持的身份证明文件格式：{original_name}")
                    if not content:
                        raise ValueError(f"身份证明文件为空：{original_name}")
                    if len(content) > 25 * 1024 * 1024:
                        raise ValueError(f"身份证明文件超过 25MB：{original_name}")
                    path = session.upload_dir / f"{uuid.uuid4().hex}{suffix}"
                    path.write_bytes(content)
                    path.chmod(0o600)
                    saved.append(path)
            except Exception:
                for path in saved:
                    path.unlink(missing_ok=True)
                raise
            for old in session.identity_documents:
                old.unlink(missing_ok=True)
            session.identity_documents = saved
            session.state["identity_documents"] = len(saved)
        return [name for name, _ in documents]

    async def _verification_code(self, session: UserSession, heading: str) -> str:
        with self._lock:
            session.state["status"] = "waiting_code"
            session.state["heading"] = heading
            session.state["message"] = "请在网页中输入刚收到的验证码"
        return await asyncio.to_thread(session.code_queue.get)

    async def _event(self, session: UserSession, record: dict[str, Any]) -> None:
        safe = {
            key: value
            for key, value in record.items()
            if key in {"time", "event", "step", "url", "heading", "text", "reason"}
        }
        with self._lock:
            if record.get("event") == "page":
                session.state["heading"] = str(record.get("heading", ""))
                session.state["url"] = str(record.get("url", ""))
                if session.state["status"] not in {
                    "waiting_code", "pausing", "paused", "reviewing", "editing"
                }:
                    session.state["status"] = "running"
                    session.state["message"] = "浏览器自动化正在运行"
            events = session.state["events"]
            events.append(safe)
            del events[:-40]

    async def _identity_file(self, session: UserSession, _label: str) -> str | None:
        with self._lock:
            if not session.active_identity_documents:
                return None
            return str(session.active_identity_documents.pop(0))

    async def _identity_files_remaining(self, session: UserSession) -> bool:
        with self._lock:
            return bool(session.active_identity_documents)

    async def _pause_checkpoint(
        self, session: UserSession, url: str, heading: str
    ) -> dict[str, str]:
        if not session.pause_requested.is_set():
            return {}
        with self._lock:
            session.state["status"] = "paused"
            session.state["message"] = "已暂停，可修改资料并重新确认后继续"
            session.state["url"] = url
            session.state["heading"] = heading
        resumed = await asyncio.to_thread(
            session.resume_event.wait, PAUSE_TIMEOUT_SECONDS
        )
        with self._lock:
            session.resume_event.clear()
            session.pause_requested.clear()
            if not resumed:
                session.state["status"] = "running"
                raise AutomationStopped(
                    "暂停等待超过 30 分钟，已自动结束并关闭浏览器。"
                )
            updates = dict(session.pending_extracted_values)
            session.pending_extracted_values = {}
            session.state["status"] = "running"
            session.state["message"] = "已应用修改后的资料，正在继续"
        return updates

    async def _final_review(
        self, session: UserSession, info: dict[str, Any]
    ) -> dict[str, str]:
        with self._lock:
            changes = [
                {
                    "id": str(item.get("id", "")),
                    "label": str(item.get("label", "")),
                    "section": str(item.get("section", "")),
                    "field": str(item.get("field", "")),
                    "value": str(item.get("value", "")),
                }
                for item in info.get("changes", [])
                if item.get("id") and item.get("label")
            ]
            session.final_review = {
                "pdf": info.get("pdf", ""),
                "png": info.get("screenshot", ""),
                "editable": bool(info.get("editable") and changes),
                "changes": changes,
            }
            session.final_review_edit = {}
            session.review_action = ""
            session.review_target = ""
            session.state["status"] = "reviewing"
            session.state["url"] = info.get("url", "")
            session.state["heading"] = info.get("heading", "")
            session.state["message"] = (
                "已到达 Check your answers 并保存复核文件。"
                "请逐项核对，只有明确确认后程序才会提交到 HMRC。"
            )
        confirmed = await asyncio.to_thread(
            session.review_event.wait, FINAL_REVIEW_TIMEOUT_SECONDS
        )
        with self._lock:
            session.review_event.clear()
            action = session.review_action
            target = session.review_target
            session.review_action = ""
            session.review_target = ""
        if not confirmed:
            raise AutomationStopped(
                "最终复核等待超过 30 分钟，已自动结束并关闭浏览器。"
            )
        if action not in {"submit", "edit"}:
            raise AutomationStopped("最终复核未收到有效操作，已停止以避免误提交。")
        return {"action": action, "target": target}

    async def _remote_edit(
        self, session: UserSession, info: dict[str, Any]
    ) -> dict[str, Any]:
        with self._lock:
            session.edit_event.clear()
            session.pending_edit_answers = {}
            session.pending_edit_action = ""
            session.final_review_edit = {
                "available": True,
                "heading": str(info.get("heading", "")),
                "url": str(info.get("url", "")),
                "fields": list(info.get("fields", [])),
                "actions": list(info.get("actions", [])),
                "errors": list(info.get("errors", [])),
            }
            session.state["status"] = "editing"
            session.state["heading"] = str(info.get("heading", ""))
            session.state["url"] = str(info.get("url", ""))
            session.state["message"] = "请在网页中修改 HMRC 信息并继续"
        submitted = await asyncio.to_thread(
            session.edit_event.wait, FINAL_REVIEW_TIMEOUT_SECONDS
        )
        with self._lock:
            session.edit_event.clear()
            answers = dict(session.pending_edit_answers)
            action = session.pending_edit_action
            session.pending_edit_answers = {}
            session.pending_edit_action = ""
        if not submitted:
            raise AutomationStopped(
                "远程修改等待超过 30 分钟，已自动结束并关闭浏览器。"
            )
        return {"answers": answers, "action": action}

    def _set_terminal_state(self, session: UserSession, status: str, message: str) -> None:
        with self._lock:
            session.state["status"] = status
            session.state["message"] = message
            if self._busy_with == session.username:
                self._busy_with = None

    @staticmethod
    def _enable_identity_uploads(session: UserSession, settings: Settings) -> None:
        if not session.active_identity_documents:
            return
        for page in settings.pages:
            if "/file-upload/upload-document" in page.path_contains:
                page.action = "continue"

    def _run(
        self,
        session: UserSession,
        credentials: dict[str, str],
        extracted_values: dict[str, str],
        fresh_session: bool,
        resume: bool,
    ) -> None:
        try:
            settings = load_settings(self.config_path)
            bag = prepare_document_values(extracted_values)
            self._enable_identity_uploads(session, settings)

            async def execute() -> None:
                async def pause_checkpoint(url: str, heading: str) -> None:
                    updates = await self._pause_checkpoint(session, url, heading)
                    if updates:
                        bag.clear()
                        bag.update(prepare_document_values(updates))

                runner = VatAutomation(
                    settings,
                    interactive=False,
                    credentials=credentials,
                    document_values=bag,
                    verification_code_provider=partial(
                        self._verification_code, session
                    ),
                    file_upload_provider=partial(self._identity_file, session),
                    file_uploads_remaining_provider=partial(
                        self._identity_files_remaining, session
                    ),
                    pause_checkpoint_provider=pause_checkpoint,
                    final_review_provider=partial(self._final_review, session),
                    remote_edit_provider=partial(self._remote_edit, session),
                    gateway_user_id_provider=partial(
                        self.remember_gateway_user_id, session
                    ),
                    event_handler=partial(self._event, session),
                    audit_context={"user": session.username},
                )
                await runner.run(resume=resume)

            try:
                if fresh_session:
                    with tempfile.TemporaryDirectory(
                        prefix=f"uk-vat-web-profile-{session.username}-"
                    ) as directory:
                        settings.profile_dir = Path(directory)
                        asyncio.run(execute())
                else:
                    asyncio.run(execute())
            finally:
                # 跑到建号成功就自动把 GG 账号存到该客户名下（失败不影响状态）。
                self._persist_credentials(session)
        except AutomationStopped as exc:
            self._set_terminal_state(session, "stopped", str(exc))
            self._record_failure(session, "run-stopped", str(exc))
        except Exception as exc:
            self._set_terminal_state(session, "failed", str(exc))
            self._record_failure(session, "run-failed", str(exc), exc)
        else:
            self._set_terminal_state(session, "completed", "流程已完成")


@dataclass
class ServerContext:
    config_path: Path = Path("vat-config.flow.json")
    env_path: Path = field(default_factory=default_env_path)
    users: UserStore = field(default_factory=lambda: UserStore(Path("users.json")))
    signer: SessionSigner = field(default_factory=SessionSigner)
    throttle: LoginThrottle = field(default_factory=LoginThrottle)
    secure_cookies: bool = True
    manager: JobManager | None = None
    # 账号库为空时生成，打印在启动终端；凭它才能在网页上创建首个管理员，
    # 防止局域网内任何人抢先注册。
    setup_token: str | None = None

    def jobs(self) -> JobManager:
        if self.manager is None:
            self.manager = JobManager(self.config_path, self.env_path)
        return self.manager

    def needs_setup(self) -> bool:
        return self.users.is_empty()

    def ensure_setup_token(self) -> str:
        if self.setup_token is None:
            self.setup_token = secrets.token_urlsafe(9)
        return self.setup_token


context = ServerContext()
app = FastAPI(title="UK VAT Automation", docs_url=None, redoc_url=None)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _same_origin(request: Request) -> bool:
    """跨站请求伪造的第一道校验：来源必须与服务自身一致。"""
    source = request.headers.get("origin") or request.headers.get("referer") or ""
    if not source:
        return False
    parsed = urlparse(source)
    if not parsed.netloc:
        return False
    return parsed.netloc == request.headers.get("host", "")


async def require_origin(request: Request) -> None:
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    if not _same_origin(request):
        raise HTTPException(status_code=403, detail="请求来源校验失败。")


async def require_csrf(request: Request) -> None:
    await require_origin(request)
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    cookie_token = request.cookies.get(CSRF_COOKIE, "")
    header_token = request.headers.get(CSRF_HEADER, "")
    if not cookie_token or not hmac.compare_digest(cookie_token, header_token):
        raise HTTPException(status_code=403, detail="CSRF 校验失败，请刷新页面重试。")


def _session_user(request: Request) -> str | None:
    token = request.cookies.get(SESSION_COOKIE, "")
    if not token:
        return None
    username = context.signer.verify(token)
    # 账号被删除后，已签发的会话立刻失效。
    if username is None or not context.users.exists(username):
        return None
    return username


async def current_user(request: Request) -> str:
    username = _session_user(request)
    if username is None:
        raise HTTPException(status_code=401, detail="请先登录。")
    return username


async def current_admin(request: Request) -> str:
    username = await current_user(request)
    if not context.users.is_admin(username):
        raise HTTPException(status_code=403, detail="只有管理员可以管理账号。")
    return username


def _issue_session(response: Response, username: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        context.signer.issue(username),
        httponly=True,
        secure=context.secure_cookies,
        samesite="strict",
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE,
        secrets.token_urlsafe(32),
        httponly=False,
        secure=context.secure_cookies,
        samesite="strict",
        path="/",
    )


def _read_page(name: str) -> str:
    return (STATIC_DIR / name).read_text(encoding="utf-8")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> Response:
    if context.needs_setup():
        return RedirectResponse("/setup", status_code=303)
    if _session_user(request) is None:
        return RedirectResponse("/login", status_code=303)
    return HTMLResponse(_read_page("index.html"))


@app.get("/login", response_class=HTMLResponse)
async def login_page() -> Response:
    if context.needs_setup():
        return RedirectResponse("/setup", status_code=303)
    return HTMLResponse(_read_page("login.html"))


@app.get("/setup", response_class=HTMLResponse)
async def setup_page() -> Response:
    if not context.needs_setup():
        return RedirectResponse("/login", status_code=303)
    return HTMLResponse(_read_page("setup.html"))


@app.post("/api/setup")
async def setup(
    request: Request,
    payload: SetupRequest,
    response: Response,
    _: None = Depends(require_origin),
) -> dict[str, str]:
    if not context.needs_setup():
        raise HTTPException(status_code=409, detail="管理员已存在，请直接登录。")
    keys = (f"ip:{_client_ip(request)}", "setup")
    waiting = context.throttle.retry_after(*keys)
    if waiting > 0:
        raise HTTPException(
            status_code=429,
            detail=f"尝试过于频繁，请 {math.ceil(waiting)} 秒后再试。",
        )
    expected = context.ensure_setup_token()
    if not hmac.compare_digest(payload.token.strip(), expected):
        context.throttle.record_failure(*keys)
        raise HTTPException(
            status_code=403, detail="初始化码不正确，请查看启动服务的终端输出。"
        )
    try:
        context.users.add(payload.username, payload.password, admin=True)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    context.throttle.reset(*keys)
    name = normalize_username(payload.username)
    _issue_session(response, name)
    return {"status": "ok", "user": name}


@app.post("/api/login")
async def login(
    request: Request,
    payload: LoginRequest,
    response: Response,
    _: None = Depends(require_origin),
) -> dict[str, str]:
    name = normalize_username(payload.username)
    keys = (f"ip:{_client_ip(request)}", f"user:{name}")
    waiting = context.throttle.retry_after(*keys)
    if waiting > 0:
        raise HTTPException(
            status_code=429,
            detail=f"尝试过于频繁，请 {math.ceil(waiting)} 秒后再试。",
        )
    if not context.users.verify(name, payload.password):
        context.throttle.record_failure(*keys)
        raise HTTPException(status_code=401, detail="用户名或密码不正确。")
    context.throttle.reset(*keys)
    _issue_session(response, name)
    return {"status": "ok", "user": name}


@app.post("/api/logout")
async def logout(response: Response, _: None = Depends(require_origin)) -> dict[str, str]:
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    return {"status": "ok"}


@app.get("/api/status")
async def status(username: str = Depends(current_user)) -> dict[str, Any]:
    return {
        **context.jobs().snapshot(username),
        "admin": context.users.is_admin(username),
    }


@app.get("/api/users")
async def list_users(_admin: str = Depends(current_admin)) -> dict[str, Any]:
    return {"users": context.users.records()}


@app.post("/api/users")
async def create_user(
    payload: UserCreateRequest,
    admin: str = Depends(current_admin),
    _: None = Depends(require_csrf),
) -> dict[str, str]:
    existed = context.users.exists(payload.username)
    try:
        context.users.add(payload.username, payload.password, admin=payload.admin)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    name = normalize_username(payload.username)
    return {
        "status": "ok",
        "user": name,
        "action": "reset" if existed else "created",
    }


@app.post("/api/users/delete")
async def delete_user(
    payload: UserNameRequest,
    admin: str = Depends(current_admin),
    _: None = Depends(require_csrf),
) -> dict[str, str]:
    name = normalize_username(payload.username)
    if context.users.is_admin(name) and context.users.admin_count() <= 1:
        raise HTTPException(
            status_code=400, detail="不能删除最后一个管理员账号。"
        )
    if not context.users.remove(name):
        raise HTTPException(status_code=404, detail=f"账号不存在：{name}")
    return {"status": "ok", "user": name}


@app.post("/api/start")
async def start(
    request: StartRequest,
    username: str = Depends(current_user),
    _: None = Depends(require_csrf),
) -> dict[str, str]:
    try:
        context.jobs().start(username, request)
    except (OSError, ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "starting"}


@app.post("/api/credentials/clear")
async def clear_credentials(
    username: str = Depends(current_user),
    _: None = Depends(require_csrf),
) -> dict[str, str]:
    """清掉本机内存里记住的 HMRC 登录信息（换账号时用）。"""
    try:
        context.jobs().clear_credentials(username)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "cleared"}


@app.post("/api/credentials/save")
async def save_credentials(
    request: CredentialSaveRequest | None = None,
    username: str = Depends(current_user),
    _: None = Depends(require_csrf),
) -> dict[str, str]:
    """把上次运行的登录信息保存到本机私有存储，之后启动不用再手输。"""
    try:
        overrides = dict(request.credentials) if request is not None else {}
        project_code = (request.project_code if request is not None else "").strip()
        result = context.jobs().save_credentials(username, overrides, project_code)
        return {**result, "key": project_code or result.get("key", "")}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/code")
async def submit_code(
    request: CodeRequest,
    username: str = Depends(current_user),
    _: None = Depends(require_csrf),
) -> dict[str, str]:
    try:
        context.jobs().submit_code(username, request.code)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "accepted"}


@app.post("/api/pause")
async def pause(
    username: str = Depends(current_user), _: None = Depends(require_csrf)
) -> dict[str, str]:
    try:
        context.jobs().pause(username)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "pausing"}


@app.post("/api/continue")
async def continue_automation(
    request: ContinueRequest,
    username: str = Depends(current_user),
    _: None = Depends(require_csrf),
) -> dict[str, str]:
    try:
        context.jobs().continue_after_pause(username, request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "continuing"}


@app.get("/api/final-review/pdf")
async def final_review_pdf(username: str = Depends(current_user)) -> FileResponse:
    path = context.jobs().final_review_document(username, "pdf")
    if path is None:
        raise HTTPException(status_code=404, detail="当前没有可下载的最终复核 PDF。")
    return FileResponse(
        path, media_type="application/pdf", filename="check-your-answers.pdf"
    )


@app.get("/api/final-review/png")
async def final_review_png(username: str = Depends(current_user)) -> FileResponse:
    path = context.jobs().final_review_document(username, "png")
    if path is None:
        raise HTTPException(status_code=404, detail="当前没有可查看的最终复核截图。")
    return FileResponse(
        path, media_type="image/png", filename="check-your-answers.png"
    )


@app.post("/api/final-review/confirm")
async def final_review_confirm(
    request: FinalSubmitRequest,
    username: str = Depends(current_user), _: None = Depends(require_csrf)
) -> dict[str, str]:
    if not request.confirmed:
        raise HTTPException(
            status_code=400, detail="请明确确认资料无误并同意提交到 HMRC。"
        )
    try:
        context.jobs().confirm_final_review(username)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "confirmed"}


@app.post("/api/final-review/edit")
async def final_review_edit(
    request: FinalEditRequest,
    username: str = Depends(current_user), _: None = Depends(require_csrf)
) -> dict[str, str]:
    try:
        context.jobs().request_final_review_edit(username, request.target)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "editing"}


@app.post("/api/final-review/edit/submit")
async def final_review_edit_submit(
    request: RemoteEditSubmitRequest,
    username: str = Depends(current_user),
    _: None = Depends(require_csrf),
) -> dict[str, str]:
    try:
        context.jobs().submit_remote_edit(username, request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "applying"}


@app.post("/api/parse-document")
async def parse_document(
    file: UploadFile = File(...),
    username: str = Depends(current_user),
    _: None = Depends(require_csrf),
) -> dict[str, Any]:
    content = await file.read(12 * 1024 * 1024 + 1)
    if len(content) > 12 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="资料文档不能超过 12MB。")
    try:
        return extract_document(file.filename or "document.txt", content)
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/identity-documents")
async def identity_documents(
    files: list[UploadFile] = File(...),
    username: str = Depends(current_user),
    _: None = Depends(require_csrf),
) -> dict[str, Any]:
    documents: list[tuple[str, bytes]] = []
    for file in files:
        content = await file.read(25 * 1024 * 1024 + 1)
        documents.append((file.filename or "document", content))
    try:
        names = context.jobs().store_identity_documents(username, documents)
    except (OSError, ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"count": len(names), "names": names}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="启动英国 VAT 自动化本地 Web UI")
    result.add_argument("--config", type=Path, default=Path("vat-config.flow.json"))
    result.add_argument("--users", type=Path, default=Path("users.json"))
    result.add_argument("--cert-dir", type=Path, default=Path("certs"))
    result.add_argument("--host", default="127.0.0.1")
    result.add_argument("--port", type=int, default=8765)
    result.add_argument("--no-open", action="store_true")
    result.add_argument("--ssl-certfile", type=Path)
    result.add_argument("--ssl-keyfile", type=Path)
    result.add_argument(
        "--insecure-http",
        action="store_true",
        help="局域网监听时不启用 TLS，凭据与客户资料将明文过网",
    )
    return result


def main() -> None:
    args = parser().parse_args()
    env_path = load_env_file()
    context.config_path = args.config.expanduser().resolve()
    context.users = UserStore(args.users.expanduser().resolve())
    context.manager = None

    if not context.config_path.is_file():
        raise SystemExit(f"配置文件不存在：{context.config_path}")
    # uvicorn.run 不会返回，stdout 重定向到文件时块缓冲会吞掉这些提示，
    # 因此启动横幅必须立即 flush。
    def banner(text: str) -> None:
        print(text, flush=True)

    if env_path is not None:
        banner(f"已从 {env_path} 读取默认登录信息（网页留空时会自动使用）。")

    if context.needs_setup():
        token = context.ensure_setup_token()
        banner("=" * 56)
        banner("还没有任何账号。首次访问网页会引导创建管理员账号，")
        banner(f"需要输入下面这个初始化码（仅本次启动有效）：\n\n    {token}\n")
        banner("=" * 56)

    is_local = args.host in LOCAL_HOSTS
    certificate: Path | None = args.ssl_certfile
    key: Path | None = args.ssl_keyfile
    if certificate is None or key is None:
        if not is_local and not args.insecure_http:
            try:
                certificate, key = tls.ensure_certificate(
                    args.cert_dir.expanduser().resolve(), hosts=[args.host]
                )
            except (OSError, RuntimeError) as exc:
                raise SystemExit(f"无法准备 TLS 证书：{exc}") from exc
        else:
            certificate = key = None

    context.secure_cookies = certificate is not None
    scheme = "https" if certificate is not None else "http"
    display_host = "127.0.0.1" if args.host in {"0.0.0.0", "::"} else args.host

    if not is_local and certificate is None:
        banner(
            "警告：正在以明文 HTTP 监听局域网。登录密码、HMRC 账号密码、"
            "短信验证码和客户身份证件都会明文经过网络。"
        )
    if certificate is not None:
        summary = tls.describe(certificate)
        banner(f"TLS 证书：{certificate}")
        if summary:
            banner(f"证书适用地址：{summary}")
        banner("同事首次访问会看到自签证书警告，需要手动选择继续访问。")
    if not is_local:
        lan = tls.preferred_lan_ip()
        if lan:
            banner(f"同事可访问：{scheme}://{lan}:{args.port}")

    if not args.no_open:
        threading.Timer(
            1.0,
            lambda: webbrowser.open(f"{scheme}://{display_host}:{args.port}"),
        ).start()

    import uvicorn

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="info",
        ssl_certfile=str(certificate) if certificate else None,
        ssl_keyfile=str(key) if key else None,
    )


if __name__ == "__main__":
    main()
