from __future__ import annotations

import argparse
import asyncio
import atexit
import hmac
import hashlib
import json
import math
import queue
import secrets
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import traceback
import uuid
import webbrowser
import zipfile
from collections.abc import Mapping
from contextlib import suppress
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
from .authenticator import AuthenticatorFlow
from .authenticator_store import AuthenticatorError, AuthenticatorStore
from .customer_store import CustomerStore, now as record_time
from .company_identity import company_name_key, normalized_identity_values
from .document_parser import (
    extract_document,
    prepare_document_values,
    validate_application_values,
)
from .envfile import (
    default_env_path,
    env_credentials,
    load_env_file,
)
from .runner import AutomationStopped, VatAutomation, is_skip_edit_action
from .mail_pool import MailPool, binding_key
from .mail_verification import MailVerifier
from .skymail import MailError, SkyMailClient


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
    "editing", "holding", "stopping", "waiting_recorder",
}
SESSION_COOKIE = "vat_session"
CSRF_COOKIE = "vat_csrf"
CSRF_HEADER = "x-csrf-token"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
# 暂停和最终复核都会挂起自动化线程并让 Chrome 一直开着，必须有上限。
PAUSE_TIMEOUT_SECONDS = 30 * 60
FINAL_REVIEW_TIMEOUT_SECONDS = 30 * 60
AUTHENTICATOR_QR_SECONDS = 5 * 60
STATIC_DIR = Path(__file__).resolve().parent / "static"


class StartRequest(BaseModel):
    # 配置文件路径由服务端启动参数固定，不接受客户端指定。
    model_config = ConfigDict(extra="forbid")

    fresh_session: bool = True
    resume: bool = False
    enable_recording: bool = False
    use_mail_pool: bool = False
    use_authenticator: bool = False
    credentials: dict[str, str] = Field(default_factory=dict)
    extracted_values: dict[str, str] = Field(default_factory=dict)
    extracted_confirmed: bool = False


class CodeRequest(BaseModel):
    code: str = Field(min_length=1, max_length=32)


class CustomerCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class CustomerDraftRequest(BaseModel):
    values: dict[str, str] = Field(default_factory=dict)


class CompanyConfirmRequest(CustomerDraftRequest):
    independent: bool = False


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
    stop_requested: threading.Event = field(default_factory=threading.Event)
    recording_start_event: threading.Event = field(default_factory=threading.Event)
    recording: dict[str, Any] = field(default_factory=dict)
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
    run_record: dict[str, Any] = field(default_factory=dict)
    artifacts_dir: Path | None = None
    # 进程内存里记住的凭据属于哪个客户；换客户时不沿用。
    saved_credentials_key: str = ""
    review_action: str = ""
    review_target: str = ""
    # 出错停留：等人工在网页上选择"继续"或"取消"。
    hold_event: threading.Event = field(default_factory=threading.Event)
    hold_decision: str = ""
    error_hold: dict[str, Any] = field(default_factory=dict)
    thread: threading.Thread | None = None
    use_mail_pool: bool = False
    pool_existing_email: str = ""
    use_authenticator: bool = False
    # 二维码仅为当前任务临时展示，绝不进入 state/run_record 或 dataclass repr。
    authenticator_qr: bytes = field(default=b"", repr=False)
    authenticator_qr_info: dict[str, Any] = field(default_factory=dict)
    authenticator_qr_timer: threading.Timer | None = field(default=None, repr=False)
    authenticator_qr_allowed: bool = False


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
        customer_store: CustomerStore | None = None,
        customer_scope: tuple[str, str, str] | None = None,
    ) -> None:
        self.config_path = config_path
        self.customer_store = customer_store
        self.customer_scope = customer_scope
        self.mail_pool = MailPool()
        self.authenticator_store = AuthenticatorStore()
        # 默认凭据来源：项目 .env（本机 0600，已忽略），网页留空时自动使用。
        self.env_path = (env_path or default_env_path()).expanduser().resolve()
        # 主要存储：按账号保存的 HMRC 登录信息（0600，git 忽略）。
        self.credential_store = credential_store or CredentialStore(
            Path("hmrc-credentials.json")
        )
        self._lock = threading.Lock()
        self._sessions: dict[str, UserSession] = {}
        self._busy_with: str | None = None
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
        keys = ALLOWED_ENV_KEYS - {"HMRC_USER_ID"} if self.customer_scope else ALLOWED_ENV_KEYS
        return sorted(env_credentials(keys))

    def _customer_credential_key(self) -> str:
        assert self.customer_scope is not None
        owner, customer_id, _flow = self.customer_scope
        return f"customer:{owner}:{customer_id}"

    def _save_run_record(self, session: UserSession) -> None:
        if not self.customer_store or not self.customer_scope or not session.run_record:
            return
        self.customer_store.save_run(*self.customer_scope, session.run_record)

    def validate_active_company(self, session: UserSession, values: Mapping[str, str]) -> None:
        if not self.customer_store or not self.customer_scope:
            return
        self.customer_store.validate_company(*self.customer_scope[:2], values)
        if session.state["status"] in ACTIVE_STATES and session.run_record:
            original = session.run_record.get("business_name", "")
            if original and company_name_key(original) != company_name_key(values.get("business_name", "")):
                raise ValueError("运行中不能更换办理公司；请停止后另行上传该公司的资料。")

    def reset_recent_operations(self, username: str) -> None:
        """重新登录只清理已结束任务的展示记录，不改任务状态或磁盘审计。"""
        with self._lock:
            session = self._sessions.get(normalize_username(username))
            if session is not None and session.state["status"] not in ACTIVE_STATES:
                session.state["events"] = []

    @staticmethod
    def _clear_authenticator_qr(session: UserSession) -> None:
        """调用方持有 manager 锁；不改已加密保存的绑定密钥。"""
        if session.authenticator_qr_timer is not None:
            session.authenticator_qr_timer.cancel()
        session.authenticator_qr_timer = None
        session.authenticator_qr = b""
        session.authenticator_qr_info = {}
        session.authenticator_qr_allowed = False

    def clear_authenticator_qr(self, username: str, run_id: str = "") -> None:
        with self._lock:
            session = self._sessions.get(normalize_username(username))
            if session is not None and (not run_id or session.run_record.get("id") == run_id):
                self._clear_authenticator_qr(session)

    async def _authenticator_qr_ready(
        self, session: UserSession, run_id: str, gateway: str, png: bytes,
    ) -> None:
        with self._lock:
            if (not session.authenticator_qr_allowed or session.stop_requested.is_set()
                    or session.run_record.get("id") != run_id):
                return
            session.authenticator_qr_allowed = False
            session.authenticator_qr = png
            session.authenticator_qr_info = {
                "id": uuid.uuid4().hex, "run_id": run_id, "gateway_tail": gateway[-4:],
                "deadline": time.monotonic() + AUTHENTICATOR_QR_SECONDS,
            }
            # 即使没有前端轮询，也到时释放图片引用；重复进入设置页不延长展示期。
            timer = threading.Timer(AUTHENTICATOR_QR_SECONDS, self.clear_authenticator_qr,
                                    args=(session.username, run_id))
            timer.daemon = True
            session.authenticator_qr_timer = timer
            try:
                timer.start()
            except RuntimeError:
                self._clear_authenticator_qr(session)
                raise AuthenticatorError("二维码临时展示不可用。") from None

    def authenticator_qr_image(self, username: str, run_id: str) -> bytes:
        session = self.session(username)
        with self._lock:
            if not run_id or session.run_record.get("id") != run_id:
                raise ValueError("任务已变更，请刷新页面。")
            if (not session.authenticator_qr
                    or time.monotonic() >= session.authenticator_qr_info.get("deadline", 0)):
                self._clear_authenticator_qr(session)
                raise ValueError("二维码展示已结束；不会从已保存的密钥重新生成。")
            return session.authenticator_qr

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
            if self.customer_scope:
                created.credential_key = self._customer_credential_key()
            self._sessions[name] = created
            return created

    def snapshot(self, username: str) -> dict[str, Any]:
        session = self.session(username)
        try:
            mail_pool_status = self.mail_pool.status()
        except (MailError, OSError, sqlite3.Error):
            mail_pool_status = {"enabled": False, "error": "邮箱池配置不可用，请管理员检查。"}
        with self._lock:
            busy = self._busy_with
            if session.authenticator_qr and time.monotonic() >= session.authenticator_qr_info.get("deadline", 0):
                self._clear_authenticator_qr(session)
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
                "run": dict(session.run_record),
                "gateway_user_id": session.gateway_user_id or (self.credential_store.get(self._customer_credential_key()).get("HMRC_USER_ID", "") if self.customer_scope else ""),
                "error_hold": dict(session.error_hold) or {"active": False},
                "recording": dict(session.recording),
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
                "mail_pool": mail_pool_status,
                "use_mail_pool": session.use_mail_pool,
                "authenticator": {
                    "enabled": session.use_authenticator, "status": "missing",
                    "qr": {
                        "id": session.authenticator_qr_info["id"],
                        "run_id": session.authenticator_qr_info["run_id"],
                        "gateway_tail": session.authenticator_qr_info["gateway_tail"],
                        "expires_in": max(0, session.authenticator_qr_info["deadline"] - time.monotonic()),
                    } if session.authenticator_qr else None,
                },
            }
            gateway = session.gateway_user_id or session.last_credentials.get("HMRC_USER_ID", "")
            if not gateway and self.customer_scope:
                gateway = self.credential_store.get(session.credential_key).get("HMRC_USER_ID", "")
            if self.customer_scope and gateway:
                try:
                    payload["authenticator"]["status"] = self.authenticator_store.status(session.credential_key, gateway)
                except AuthenticatorError:
                    payload["authenticator"]["status"] = "unavailable"
        return payload

    def start(self, username: str, request: StartRequest) -> None:
        session = self.session(username)
        if not self.config_path.is_file():
            raise ValueError(f"服务端配置文件不存在：{self.config_path}")
        self._refresh_config()
        if self.customer_store and self.customer_scope:
            self.customer_store.validate_company(*self.customer_scope[:2], request.extracted_values)
        if self.customer_scope and request.resume:
            raise ValueError("客户隔离模式请从当前客户重新开始；不复用旧任务断点。")
        if request.fresh_session and request.resume:
            raise ValueError("全新会话不能同时启用断点恢复。")
        if request.enable_recording and (not request.fresh_session or request.resume):
            raise ValueError("允许录制时必须使用全新浏览器会话，不能复用旧浏览器配置或断点恢复。")
        if request.use_authenticator:
            if not self.customer_scope:
                raise ValueError("请先确认公司资料，再启用自动管理 Authenticator。")
            if request.enable_recording:
                raise ValueError("自动管理 Authenticator 不能与浏览器录制同时启用，以免泄露绑定密钥。")
            if not request.fresh_session or request.resume:
                raise ValueError("自动管理 Authenticator 必须使用全新浏览器会话，避免绑定到旧会话中的其他 GG。")
        if not request.extracted_confirmed:
            raise ValueError("请先在网页中检查并确认文档提取结果。")
        if (
            self.identity_documents_required
            and len(session.identity_documents) != self.identity_documents_required
        ):
            raise ValueError(
                f"请先保存正好 {self.identity_documents_required} 份身份证明文件。"
            )

        validate_application_values(request.extracted_values, is_eori="EORI" in self.flow_name.upper())

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
            session.credential_key = self._customer_credential_key() if self.customer_scope else self.credential_key_for(session, request.extracted_values)
            stored = self.credential_store.get(session.credential_key)
            if not self.customer_scope and not stored and session.credential_key != session.username:
                stored = self.credential_store.get(session.username)
            # 内存里记住的凭据只在同一客户内复用，避免张冠李戴。
            remembered = (
                dict(session.saved_credentials)
                if session.saved_credentials_key == session.credential_key
                else {}
            )
            env_values = env_credentials(ALLOWED_ENV_KEYS)
            if self.customer_scope:
                env_values.pop("HMRC_USER_ID", None)
                old_id = stored.get("HMRC_USER_ID") or remembered.get("HMRC_USER_ID")
                if old_id and provided.get("HMRC_USER_ID", old_id) != old_id and not provided.get("HMRC_PASSWORD"):
                    raise ValueError("GG 账号已变更，请填写对应密码，不可沿用旧账号密码。")
            credentials = {**env_values, **remembered, **stored, **provided}
            if request.use_authenticator:
                method = credentials.get("HMRC_SIGN_IN_METHOD", self.default_sign_in_method)
                if method == "Create new sign in details" and credentials.get("HMRC_USER_ID"):
                    raise ValueError("当前申请已保存 GG，请使用已有账号，避免将新验证器绑定到错误账号。")
                if method not in {"Create new sign in details", "Government Gateway"}:
                    raise ValueError("自动管理 Authenticator 仅支持 Government Gateway。")
                try:
                    self.authenticator_store.prepare()
                except (AuthenticatorError, OSError):
                    raise ValueError("Authenticator 私有存储不可用，请管理员检查主密钥、权限与依赖。") from None
                credentials["HMRC_MFA_METHOD"] = "Authenticator app for smartphone or tablet"
            if request.use_mail_pool:
                if not self.customer_scope:
                    raise ValueError("请先确认公司资料，再使用邮箱池。")
                if credentials.get("HMRC_SIGN_IN_METHOD", self.default_sign_in_method) != "Create new sign in details":
                    raise ValueError("邮箱池目前只用于创建新 GG，不会替换已有 GG 的绑定邮箱。")
                if credentials.get("HMRC_USER_ID"):
                    raise ValueError("当前申请已保存 GG 账号，请使用已有账号，避免重复建号。")
                if provided.get("HMRC_EMAIL"):
                    raise ValueError("使用邮箱池时请勿同时手填开户邮箱。")
                contact_email = request.extracted_values.get("vat_contact_email", "").strip()
                if contact_email.count("@") != 1 or any(c.isspace() for c in contact_email) or not all(contact_email.split("@")):
                    raise ValueError("使用邮箱池前请在资料区明确填写 VAT / EORI 联系邮箱，避免流程回退使用池内开户邮箱。")
                try:
                    if not self.mail_pool.status().get("enabled"):
                        raise MailError("邮箱池未启用，请先由管理员完成导入和检查。")
                except (MailError, OSError, sqlite3.Error):
                    raise ValueError("邮箱池未就绪，请先在本机完成导入、检查和启用。") from None
            # 该客户已经存过 Government Gateway 账号时默认用它登录（建号那一步已经做过），
            # 表单或 .env 显式指定登录方式时以它们为准。
            if (
                "HMRC_SIGN_IN_METHOD" not in provided
                and "HMRC_SIGN_IN_METHOD" not in env_values
                and credentials.get("HMRC_USER_ID")
            ):
                credentials["HMRC_SIGN_IN_METHOD"] = "Government Gateway"
            if self.customer_scope and credentials.get("HMRC_SIGN_IN_METHOD", self.default_sign_in_method) == "Government Gateway":
                if not credentials.get("HMRC_USER_ID") or not credentials.get("HMRC_PASSWORD"):
                    raise ValueError("请为当前客户填写 GG 账号和密码；不会沿用其他客户或全局 GG 账号。")
            if self.customer_store and self.customer_scope:
                self.customer_store.save_draft(*self.customer_scope, request.extracted_values)
                run_id = uuid.uuid4().hex
                session.run_record = {
                    "id": run_id, "customer_id": self.customer_scope[1], "flow": self.customer_scope[2],
                    "business_name": request.extracted_values.get("business_name", ""),
                    "started_at": record_time(), "finished_at": "", "status": "starting",
                    "outcome": "unverified", "reference": "", "submission_confirmed": False,
                    "message": "正在启动", "documents": {},
                }
                session.artifacts_dir = self.customer_store.folder(*self.customer_scope) / "runs" / run_id
                self._save_run_record(session)
            if provided or stored or remembered:
                session.saved_credentials = dict(credentials)
                session.saved_credentials_key = session.credential_key
            session.last_credentials = dict(credentials)
            session.use_mail_pool = request.use_mail_pool
            session.use_authenticator = request.use_authenticator
            self._clear_authenticator_qr(session)
            session.authenticator_qr_allowed = request.use_authenticator
            session.pool_existing_email = stored.get("HMRC_EMAIL", "") if request.use_mail_pool else ""
            session.gateway_user_id = ""
            self._busy_with = session.username
            session.state = {
                **_initial_state(),
                "status": "starting",
                "message": "正在加载配置并启动浏览器",
                "business_name": request.extracted_values.get("business_name", ""),
            }
            session.active_identity_documents = list(session.identity_documents)
            session.final_review = {}
            session.stop_requested.clear()
            session.recording_start_event.clear()
            session.recording = {"enabled": request.enable_recording, "endpoint": "", "target_id": ""}
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

    def stop(self, username: str) -> None:
        """仅停止该用户当前流程，清理完成前继续占用任务槽。"""
        session = self.session(username)
        with self._lock:
            if session.state["status"] not in ACTIVE_STATES:
                raise RuntimeError("当前没有正在运行的注册任务。")
            session.stop_requested.set()
            self._clear_authenticator_qr(session)
            session.state["status"] = "stopping"
            session.state["message"] = "正在停止本次注册并关闭自动化浏览器，请稍候。"

    async def _browser_ready(
        self, session: UserSession, endpoint: str, target_id: str
    ) -> None:
        with self._lock:
            self._check_stop_requested(session)
            session.recording.update(endpoint=endpoint, target_id=target_id)
            session.state["status"] = "waiting_recorder"
            session.state["message"] = "录制连接已就绪；连接录制器后，点击“开始注册流程”。"
        ready = await self._wait_for_event(
            session, session.recording_start_event, PAUSE_TIMEOUT_SECONDS
        )
        with self._lock:
            self._check_stop_requested(session)
            session.recording_start_event.clear()
            if not ready:
                raise AutomationStopped("等待连接录制器超过 30 分钟，已结束本次任务。")
            session.state["status"] = "running"
            session.state["message"] = "正在开始注册流程"

    def continue_recording(self, username: str) -> None:
        session = self.session(username)
        with self._lock:
            if session.state["status"] != "waiting_recorder":
                raise RuntimeError("当前没有等待连接录制器的任务。")
            session.recording_start_event.set()
            session.state["status"] = "starting"
            session.state["message"] = "正在开始注册流程"

    @staticmethod
    def _check_stop_requested(session: UserSession) -> None:
        if session.stop_requested.is_set():
            # 不走普通错误停留，否则会在停止时再次等待人工操作。
            raise asyncio.CancelledError

    async def _wait_for_event(
        self, session: UserSession, event: threading.Event, timeout: float
    ) -> bool:
        """可取消的等待，不留下阻塞线程阻止 asyncio.run 退出。"""
        deadline = time.monotonic() + timeout
        while True:
            self._check_stop_requested(session)
            if event.is_set():
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.1)

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
            keys = {self._session_credential_key(session)}
            if not self.customer_scope:
                keys.add(session.username)
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
    ) -> dict[str, Any]:
        """把上次运行实际使用的登录信息（含新建的 Gateway User ID）存到本机。"""
        session = self.session(username)
        # 注意：last_run_credentials 自己会加锁，这里不能再持锁，否则死锁。
        if session.state["status"] in ACTIVE_STATES:
            raise RuntimeError("自动化运行中不能保存登录信息。")
        store_key = self._customer_credential_key() if self.customer_scope else str(key).strip() or self._session_credential_key(session)
        values = {
            **self.last_run_credentials(username),
            **{
                key: str(value).strip()
                for key, value in dict(overrides or {}).items()
                if str(value).strip()
            },
        }
        if self.customer_scope:
            stored = self.credential_store.get(store_key)
            values = {**self.last_run_credentials(username), **stored, **{
                field: str(value).strip() for field, value in dict(overrides or {}).items() if str(value).strip()
            }}
            new_id = str((overrides or {}).get("HMRC_USER_ID", "")).strip()
            old_id = stored.get("HMRC_USER_ID") or session.last_credentials.get("HMRC_USER_ID")
            if old_id and new_id and old_id != new_id and not (overrides or {}).get("HMRC_PASSWORD"):
                raise ValueError("GG 账号已变更，请填写对应密码。")
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
            artifacts = session.artifacts_dir or settings.artifacts_dir
        except (OSError, ValueError):
            return
        try:
            artifacts.mkdir(parents=True, exist_ok=True)
            with (artifacts / "audit.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            return

    async def _error_hold(self, session: UserSession, payload: dict[str, Any]) -> str:
        """出错时停留：等网页上的人工决定，超时按取消处理。

        返回 "resume"（从当前页重试）或 "cancel"/"timeout"（按原样停止）。
        """
        seconds = max(0, int(payload.get("seconds") or 0))
        message = str(payload.get("message", ""))
        heading = str(payload.get("heading", ""))
        deadline = time.monotonic() + seconds
        with self._lock:
            self._check_stop_requested(session)
            session.hold_event.clear()
            session.hold_decision = ""
            session.error_hold = {
                "active": True,
                "message": message,
                "heading": heading,
                "url": str(payload.get("url", "")),
                "seconds": seconds,
                "deadline": time.time() + seconds,
            }
            session.state["status"] = "holding"
            session.state["heading"] = heading
            session.state["message"] = (
                f"出错已暂停：{message}（最多停留 {max(1, seconds // 60)} 分钟，"
                "可在网页上选择继续或取消）"
            )
        while time.monotonic() < deadline:
            if await self._wait_for_event(session, session.hold_event, 1.0):
                break
            remaining = max(0, int(deadline - time.monotonic()))
            with self._lock:
                self._check_stop_requested(session)
                session.state["message"] = (
                    f"出错已暂停（剩余 {remaining // 60} 分 {remaining % 60} 秒）：{message}"
                )
        with self._lock:
            self._check_stop_requested(session)
            decision = session.hold_decision or "timeout"
            session.hold_decision = ""
            session.error_hold = {}
            session.state["status"] = "running"
            session.state["message"] = (
                "已收到人工选择：继续从当前页面重试"
                if decision == "resume"
                else "已结束这一轮（人工取消或超时）"
            )
        return decision

    def resolve_error_hold(self, username: str, decision: str) -> None:
        """前端决定：继续当前任务或取消。"""
        if decision not in {"resume", "cancel"}:
            raise ValueError(f"无效的处理方式：{decision}")
        session = self.session(username)
        with self._lock:
            if session.state["status"] != "holding":
                raise RuntimeError("当前没有等待处理的出错暂停。")
            session.hold_decision = decision
            session.state["message"] = (
                "正在从当前页面重试" if decision == "resume" else "正在结束这一轮"
            )
            session.hold_event.set()

    def pause(self, username: str) -> None:
        session = self.session(username)
        with self._lock:
            if session.state["status"] != "running":
                raise RuntimeError("只能在自动化正在运行时暂停。")
            session.pause_requested.set()
            session.state["status"] = "pausing"
            session.state["message"] = "正在等待当前页面操作到达安全暂停点"

    def effective_application_values(self, username: str, values: Mapping[str, str]) -> dict[str, str]:
        """池内个人邮箱是本次 VAT 的运行策略，暂停修改不能换回解析邮箱。"""
        session = self.session(username)
        effective = dict(values)
        with self._lock:
            if session.state["status"] in ACTIVE_STATES and session.state.get("pool_application_email"):
                effective["email"] = session.state["pool_application_email"]
        return effective

    def continue_after_pause(self, username: str, request: ContinueRequest) -> dict[str, str]:
        session = self.session(username)
        if not request.extracted_confirmed:
            raise ValueError("修改资料后请先点击“确认资料无误”。")
        effective = self.effective_application_values(username, request.extracted_values)
        validate_application_values(effective, is_eori="EORI" in self.flow_name.upper())
        self.validate_active_company(session, effective)
        with self._lock:
            if session.state["status"] != "paused":
                raise RuntimeError("当前任务未处于已暂停状态。")
            session.pending_extracted_values = effective
            if request.extracted_values.get("business_name"):
                session.state["business_name"] = request.extracted_values["business_name"]
            session.state["message"] = "正在应用修改后的资料并继续"
            session.resume_event.set()
        return effective

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

    async def _verification_code(self, session: UserSession, heading: str, message: str = "") -> str:
        with self._lock:
            self._check_stop_requested(session)
            session.state["status"] = "waiting_code"
            session.state["heading"] = heading
            session.state["message"] = message or "请在网页中输入刚收到的验证码"
        while True:
            self._check_stop_requested(session)
            try:
                return session.code_queue.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.1)

    async def _mail_code(self, session: UserSession, verifier: MailVerifier, heading: str) -> str:
        if verifier.cursor is None or verifier.attempted:
            return await self._verification_code(session, heading, verifier.notice)
        manual = asyncio.create_task(self._verification_code(
            session, heading,
            ("正在自动读取本次 VAT 个人邮箱验证邮件；也可以直接手动输入验证码。"
             if verifier.purpose == "vat_personal_email"
             else "正在自动读取本次 GG 建号邮件；也可以直接手动输入验证码。")
        ))
        automatic = asyncio.create_task(verifier.receive())
        try:
            await asyncio.wait({manual, automatic}, return_when=asyncio.FIRST_COMPLETED)
            if manual.done():
                return manual.result()
            try:
                code = automatic.result()
            except (MailError, OSError, sqlite3.Error) as exc:
                with self._lock:
                    if session.state["status"] == "waiting_code":
                        session.state["message"] = str(exc) if isinstance(exc, MailError) else "自动收码不可用，请人工输入验证码。"
                return await manual
            if code:
                with self._lock:
                    self._check_stop_requested(session)
                    # 手动输入与自动结果恰好同时到达时优先使用用户明确输入的值。
                    if manual.done():
                        return manual.result()
                    try:
                        return session.code_queue.get_nowait()
                    except queue.Empty:
                        pass
                    session.state["status"] = "running"
                    session.state["message"] = "已匹配本次邮件，正在验证；不会展示或记录验证码。"
                return code
            return await manual
        finally:
            manual.cancel()
            automatic.cancel()
            await asyncio.gather(manual, automatic, return_exceptions=True)

    async def _event(self, session: UserSession, record: dict[str, Any]) -> None:
        safe = {
            key: value
            for key, value in record.items()
            if key in {"time", "event", "step", "url", "heading", "text", "reason"}
        }
        if str(record.get("event", "")).startswith("authenticator-"):
            safe["text"] = str(record.get("message", ""))
        with self._lock:
            if safe.get("text") and str(record.get("event", "")).startswith("authenticator-") and not session.stop_requested.is_set():
                session.state["message"] = safe["text"]
            if session.run_record and record.get("event") in {"final-review-confirmed", "application-submitted"}:
                session.run_record["submission_confirmed"] = True
                if record.get("event") == "application-submitted":
                    session.run_record.update(
                        outcome=record.get("outcome", "unverified"),
                        reference=record.get("reference", ""),
                        evidence_heading=record.get("heading", ""),
                    )
                    for kind in ("receipt_pdf", "receipt_png"):
                        if record.get(kind):
                            session.run_record["documents"][kind] = Path(record[kind]).name
                self._save_run_record(session)
            if record.get("event") == "page" and not session.stop_requested.is_set():
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
        self._check_stop_requested(session)
        if not session.pause_requested.is_set():
            return {}
        with self._lock:
            self._check_stop_requested(session)
            session.state["status"] = "paused"
            session.state["message"] = "已暂停，可修改资料并重新确认后继续"
            session.state["url"] = url
            session.state["heading"] = heading
        resumed = await self._wait_for_event(
            session, session.resume_event, PAUSE_TIMEOUT_SECONDS
        )
        with self._lock:
            self._check_stop_requested(session)
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
            self._check_stop_requested(session)
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
            if session.run_record:
                for kind in ("pdf", "png"):
                    if session.final_review.get(kind):
                        session.run_record["documents"][kind] = Path(session.final_review[kind]).name
                self._save_run_record(session)
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
        confirmed = await self._wait_for_event(
            session, session.review_event, FINAL_REVIEW_TIMEOUT_SECONDS
        )
        with self._lock:
            self._check_stop_requested(session)
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
            self._check_stop_requested(session)
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
        submitted = await self._wait_for_event(
            session, session.edit_event, FINAL_REVIEW_TIMEOUT_SECONDS
        )
        with self._lock:
            self._check_stop_requested(session)
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
            self._clear_authenticator_qr(session)
            session.recording_start_event.clear()
            if session.recording:
                session.recording.update(endpoint="", target_id="")
            if session.stop_requested.is_set():
                status = "stopped"
                message = "本次自动化已停止，可以重新开始。已发出的 HMRC 请求无法撤回，请先核实申请状态。"
                session.error_hold = {}
                session.final_review_edit = {}
                session.review_action = session.review_target = ""
                session.pending_extracted_values = {}
                session.pending_edit_answers = {}
                session.pending_edit_action = ""
                session.pause_requested.clear()
                session.resume_event.clear()
                session.review_event.clear()
                session.edit_event.clear()
                session.hold_event.clear()
                session.hold_decision = ""
                while not session.code_queue.empty():
                    with suppress(queue.Empty):
                        session.code_queue.get_nowait()
            session.state["status"] = status
            if session.run_record and status == "completed":
                message = {
                    "registered": "已从 HMRC 回执确认取得 EORI，请保存回执编号。",
                    "received": "HMRC 已接收申请，等待处理；这不表示注册已获批。",
                }.get(session.run_record.get("outcome"), "自动化已结束，结果待核实；请检查提交后页面，不要直接重复申请。")
            session.state["message"] = message
            if session.run_record:
                session.run_record.update(status=status, message=message, finished_at=record_time())
                for kind in ("pdf", "png"):
                    if session.final_review.get(kind):
                        session.run_record["documents"][kind] = Path(session.final_review[kind]).name
                try:
                    self._save_run_record(session)
                except (OSError, ValueError):
                    session.state["message"] += "（办理记录保存失败，请勿关闭本页。）"
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
        mail_allocation: dict[str, Any] | None = None
        mail_lease = uuid.uuid4().hex
        terminal = ("failed", "自动化未正常结束，请检查后再重试。")
        try:
            settings = load_settings(self.config_path)
            if self.customer_store and self.customer_scope and session.artifacts_dir:
                settings.artifacts_dir = session.artifacts_dir
                account = hashlib.sha256(credentials.get("HMRC_USER_ID", "new-account").encode()).hexdigest()[:16]
                settings.profile_dir = self.customer_store.folder(*self.customer_scope) / "browser-profile" / account
                settings.profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                settings.profile_dir.chmod(0o700)
            bag = prepare_document_values(extracted_values)
            self._enable_identity_uploads(session, settings)
            if session.use_mail_pool:
                self._check_stop_requested(session)
                mail_allocation = self.mail_pool.acquire(
                    binding_key(session.credential_key), mail_lease, session.pool_existing_email
                )
                credentials["HMRC_EMAIL"] = mail_allocation["email"]
                # 仅 VAT 个人联系邮箱复用本次 GG 邮箱；公司及 EORI 通知邮箱不变。
                if self.customer_scope and self.customer_scope[2] == "vat":
                    extracted_values["email"] = mail_allocation["email"]
                    bag["email"] = mail_allocation["email"]
                    if self.customer_store:
                        self.customer_store.save_draft(*self.customer_scope, extracted_values)
                # 邮箱密码不放进 HMRC 凭据、不传给 Chrome；只保存实际开户地址。
                self.credential_store.save(session.credential_key, {"HMRC_EMAIL": mail_allocation["email"]})
                with self._lock:
                    session.last_credentials = dict(credentials)
                    session.saved_credentials = dict(credentials)
                    session.saved_credentials_key = session.credential_key
                    session.state["pool_email"] = mail_allocation["email"]
                    if self.customer_scope and self.customer_scope[2] == "vat":
                        session.state["pool_application_email"] = mail_allocation["email"]
                        session.run_record["application_email"] = mail_allocation["email"]
                        self._save_run_record(session)
                    session.state["message"] = "已分配开户邮箱，正在启动浏览器。"

            async def execute(
                verifier: MailVerifier | None = None, vat_verifier: MailVerifier | None = None,
            ) -> None:
                self._check_stop_requested(session)
                async def pause_checkpoint(url: str, heading: str) -> None:
                    updates = await self._pause_checkpoint(session, url, heading)
                    if updates:
                        bag.clear()
                        bag.update(prepare_document_values(updates))
                        if mail_allocation is not None and self.customer_scope and self.customer_scope[2] == "vat":
                            bag["email"] = mail_allocation["email"]

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
                    error_hold_provider=partial(self._error_hold, session),
                    event_handler=partial(self._event, session),
                    audit_context={"user": session.username, **({"customer_id": self.customer_scope[1], "run_id": session.run_record["id"]} if self.customer_scope else {})},
                    stop_requested=session.stop_requested.is_set,
                    enable_recording=bool(session.recording.get("enabled")),
                    browser_ready_provider=partial(self._browser_ready, session),
                    email_verification_prepare=verifier.prepare if verifier else None,
                    email_verification_provider=partial(self._mail_code, session, verifier) if verifier else None,
                    vat_email_verification_prepare=vat_verifier.prepare if vat_verifier else None,
                    vat_email_verification_provider=partial(self._mail_code, session, vat_verifier) if vat_verifier else None,
                    authenticator=AuthenticatorFlow(
                        self.authenticator_store, session.credential_key,
                        allow_setup=credentials.get("HMRC_SIGN_IN_METHOD", self.default_sign_in_method) == "Create new sign in details",
                        qr_ready=partial(self._authenticator_qr_ready, session, session.run_record.get("id", "")),
                    ) if session.use_authenticator else None,
                )
                task = asyncio.create_task(runner.run(resume=resume))

                async def watch_stop() -> None:
                    while not session.stop_requested.is_set():
                        await asyncio.sleep(0.1)
                    # 只取消一次，让 runner 的 finally 完整关闭浏览器。
                    if not task.done() and not task.cancelling() and not runner.stopping:
                        task.cancel()

                watcher = asyncio.create_task(watch_stop())
                try:
                    await task
                finally:
                    watcher.cancel()
                    with suppress(asyncio.CancelledError):
                        await watcher

            async def execute_with_mail() -> None:
                if mail_allocation is None:
                    await execute()
                    return
                async with SkyMailClient(mail_allocation["base_url"], mail_allocation["email"],
                                         mail_allocation["password"]) as client:
                    # 同一邮箱和客户端，不同用途各自持有游标、发码时间及尝试状态。
                    vat_verifier = (
                        MailVerifier(self.mail_pool, mail_allocation, client, purpose="vat_personal_email")
                        if self.customer_scope and self.customer_scope[2] == "vat" else None
                    )
                    await execute(MailVerifier(self.mail_pool, mail_allocation, client), vat_verifier)

            try:
                if fresh_session:
                    with tempfile.TemporaryDirectory(
                        prefix=f"uk-vat-web-profile-{session.username}-"
                    ) as directory:
                        settings.profile_dir = Path(directory)
                        asyncio.run(execute_with_mail())
                else:
                    asyncio.run(execute_with_mail())
            finally:
                # 跑到建号成功就自动把 GG 账号存到该客户名下（失败不影响状态）。
                self._persist_credentials(session)
        except asyncio.CancelledError:
            terminal = ("stopped", "本次自动化已停止。")
        except MailError as exc:
            terminal = ("stopped", str(exc))
        except AutomationStopped as exc:
            self._record_failure(session, "run-stopped", str(exc))
            terminal = ("stopped", str(exc))
        except Exception as exc:
            self._record_failure(session, "run-failed", str(exc), exc)
            terminal = ("failed", str(exc))
        else:
            terminal = ("completed", "流程已完成")
        finally:
            if mail_allocation is not None:
                try:
                    self.mail_pool.release(mail_lease)
                except (MailError, OSError, sqlite3.Error):
                    # 保留占用而非冒险重复分配；管理员在确认任务已停后可本地解除。
                    terminal = (terminal[0], terminal[1] + "（邮箱占用释放失败，请管理员检查。）")
            self._set_terminal_state(session, *terminal)


@dataclass
class ServerContext:
    config_path: Path = Path("vat-config.flow.json")
    env_path: Path = field(default_factory=default_env_path)
    users: UserStore = field(default_factory=lambda: UserStore(Path("users.json")))
    signer: SessionSigner = field(default_factory=SessionSigner)
    throttle: LoginThrottle = field(default_factory=LoginThrottle)
    secure_cookies: bool = True
    manager: JobManager | None = None
    default_flow: str = "vat"
    flow_configs: dict[str, Path] = field(default_factory=dict)
    flow_managers: dict[str, JobManager] = field(default_factory=dict)
    customer_managers: dict[tuple[str, str, str], JobManager] = field(default_factory=dict)
    customer_store: CustomerStore | None = None
    # 账号库为空时生成，打印在启动终端；凭它才能在网页上创建首个管理员，
    # 防止局域网内任何人抢先注册。
    setup_token: str | None = None

    def customers(self) -> CustomerStore:
        if self.customer_store is None:
            self.customer_store = CustomerStore(self.config_path.resolve().parent / "vat-web-data")
        return self.customer_store

    def owner_id(self, username: str) -> str:
        name = normalize_username(username)
        account = next((r for r in self.users.records() if r["username"] == name), None)
        if account is None:
            raise HTTPException(status_code=401, detail="请重新登录")
        # 删除后重建同名账号不自动继承旧客户。
        return hashlib.sha256(f"{name}:{account['created']}".encode()).hexdigest()[:32]

    def customer_jobs(self, username: str, flow: str, customer_id: str) -> JobManager:
        if not customer_id:
            raise HTTPException(status_code=409, detail="请先上传并确认资料，系统会自动关联公司或建立独立申请。")
        base = self.jobs(flow)
        owner = self.owner_id(username)
        try:
            self.customers().get(owner, customer_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="客户不存在或不属于当前用户。") from exc
        scope = (owner, customer_id, flow or self.default_flow)
        if scope not in self.customer_managers:
            self.customer_managers[scope] = JobManager(
                base.config_path, self.env_path, base.credential_store, self.customers(), scope
            )
        return self.customer_managers[scope]

    def all_managers(self) -> list[JobManager]:
        return [m for m in [self.manager, *self.flow_managers.values(), *self.customer_managers.values()] if m is not None]

    def jobs(self, flow: str = "") -> JobManager:
        flow = flow or self.default_flow
        if flow != self.default_flow and flow not in self.flow_configs:
            raise HTTPException(status_code=404, detail="未配置该注册流程。")
        if self.manager is None:
            self.manager = JobManager(self.config_path, self.env_path)
        if flow == self.default_flow:
            return self.manager
        if flow not in self.flow_managers:
            self.flow_managers[flow] = JobManager(
                self.flow_configs[flow], self.env_path, self.manager.credential_store
            )
        return self.flow_managers[flow]

    def configure_flows(self, config: Path, eori_config: Path | None = None) -> None:
        """保留 --config 的默认入口，同时挂载另一业务；客户端只能选流程 ID。"""
        self.config_path = config.expanduser().resolve()
        settings = load_settings(self.config_path)
        self.default_flow = "eori" if "eori" in settings.start_url else "vat"
        self.manager = None
        self.flow_managers = {}
        self.customer_managers = {}
        self.customer_store = None
        self.flow_configs = {}
        if self.default_flow == "vat":
            companion = eori_config or self.config_path.with_name(
                "vat-config.eori.flow.json"
            )
            companion_id = "eori"
        else:
            companion = self.config_path.with_name("vat-config.flow.json")
            companion_id = "vat"
        companion = companion.expanduser().resolve()
        if eori_config is not None and self.default_flow == "vat" and not companion.is_file():
            raise ValueError(f"EORI 配置文件不存在：{companion}")
        if companion.is_file() and companion != self.config_path:
            load_settings(companion)
            self.flow_configs[companion_id] = companion

    def available_flows(self) -> list[dict[str, str]]:
        return [
            {"id": flow, "name": self.jobs(flow).flow_name or f"英国 {flow.upper()} 注册"}
            for flow in (self.default_flow, *self.flow_configs)
        ]

    def busy_flow(self, excluding: str = "") -> dict[str, str]:
        """跨流程仍只运行一个任务，避免同时占用 Chrome 或混淆人工操作。"""
        for flow, manager in [(self.default_flow, self.manager), *self.flow_managers.items()]:
            if manager is None or flow == excluding:
                continue
            with manager._lock:
                if manager._busy_with:
                    return {"id": flow, "name": manager.flow_name or flow.upper()}
        return {}

    def busy_customer(self, username: str, excluding: JobManager | None = None) -> dict[str, str]:
        owner = self.owner_id(username)
        for scope, manager in self.customer_managers.items():
            if manager is excluding or not manager._busy_with:
                continue
            if scope[0] == owner:
                return {"id": scope[2], "name": manager.flow_name, "customer_id": scope[1]}
            return {"id": scope[2], "name": "其他用户的注册任务"}
        return {}

    def credential_managers(self, manager: JobManager) -> list[JobManager]:
        assert manager.customer_scope is not None
        matches = [m for scope, m in self.customer_managers.items() if scope[:2] == manager.customer_scope[:2]]
        if any(m._busy_with for m in matches):
            raise RuntimeError("该客户有任务运行中，请结束后再修改或清除登录信息。")
        return matches

    def needs_setup(self) -> bool:
        return self.users.is_empty()

    def ensure_setup_token(self) -> str:
        if self.setup_token is None:
            self.setup_token = secrets.token_urlsafe(9)
        return self.setup_token


context = ServerContext()
app = FastAPI(title="UK Registration Automation", docs_url=None, redoc_url=None)


@app.middleware("http")
async def private_responses(request: Request, call_next: Any) -> Response:
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


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
    task_actions = {"/api/code", "/api/pause", "/api/stop", "/api/continue", "/api/recording/continue", "/api/error-hold/resume", "/api/error-hold/cancel", "/api/final-review/confirm", "/api/final-review/edit", "/api/final-review/edit/submit"}
    task_actions.update({"/api/authenticator/qr", "/api/authenticator/qr/close"})
    if request.url.path in {"/api/company/confirm", "/api/customer/draft"} and request.query_params.get("customer_id"):
        username = await current_user(request)
        manager = context.customer_jobs(username, request.query_params.get("flow", ""), request.query_params["customer_id"])
        if manager.session(username).state["status"] in ACTIVE_STATES:
            task_actions.add(request.url.path)
    if request.url.path in task_actions:
        username = await current_user(request)
        manager = context.customer_jobs(username, request.query_params.get("flow", ""), request.query_params.get("customer_id", ""))
        run_id = manager.session(username).run_record.get("id", "")
        if not run_id or request.headers.get("x-vat-run-id") != run_id:
            raise HTTPException(status_code=409, detail="页面已过期或任务已变更，请刷新后核对当前办理记录。")


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
    # 两个业务都重置已结束任务的最近操作；运行中任务仍可在重新登录后接续。
    managers = context.all_managers()
    for manager in managers:
        if manager is not None:
            manager.reset_recent_operations(username)
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
async def logout(request: Request, response: Response, _: None = Depends(require_origin)) -> dict[str, str]:
    username = _session_user(request)
    if username:
        for manager in context.all_managers():
            if manager is not None:
                manager.clear_authenticator_qr(username)
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    return {"status": "ok"}


@app.post("/api/authenticator/qr")
async def authenticator_qr(
    request: Request, username: str = Depends(current_user), _: None = Depends(require_csrf),
    flow: str = "", customer_id: str = "",
) -> Response:
    # 局域网明文调试模式仍禁止发送第二因素密钥；本机回环访问可用。
    if request.url.scheme != "https" and request.url.hostname not in LOCAL_HOSTS:
        raise HTTPException(status_code=403, detail="二维码仅可通过 HTTPS 或本机回环地址查看。")
    manager = context.customer_jobs(username, flow, customer_id)
    try:
        png = manager.authenticator_qr_image(username, request.headers.get("x-vat-run-id", ""))
    except ValueError as exc:
        raise HTTPException(status_code=410, detail=str(exc)) from None
    return Response(content=png, media_type="image/png", headers={
        "Cache-Control": "no-store", "Pragma": "no-cache",
        "X-Content-Type-Options": "nosniff", "Cross-Origin-Resource-Policy": "same-origin",
    })


@app.post("/api/authenticator/qr/close")
async def close_authenticator_qr(
    request: Request, username: str = Depends(current_user), _: None = Depends(require_csrf),
    flow: str = "", customer_id: str = "",
) -> dict[str, str]:
    context.customer_jobs(username, flow, customer_id).clear_authenticator_qr(
        username, request.headers.get("x-vat-run-id", ""),
    )
    return {"status": "ok"}


@app.get("/api/status")
async def status(username: str = Depends(current_user), flow: str = "", customer_id: str = "") -> dict[str, Any]:
    manager = context.customer_jobs(username, flow, customer_id) if customer_id else context.jobs(flow)
    payload = manager.snapshot(username)
    admin = context.users.is_admin(username)
    if not admin:
        # 调试连接可直接控制 Chrome，普通用户不能从状态接口取得连接信息。
        payload["recording"] = {"enabled": False, "endpoint": "", "target_id": ""}
    if not customer_id:
        # 没选择客户时只供业务导航；不展示旧按账号/项目编号存储的凭据。
        payload.update(credentials_stored={}, gateway_user_id="", credential_key="", credentials_saved=False)
    return {
        **payload,
        "customer_id": customer_id,
        "customer_name": context.customers().get(context.owner_id(username), customer_id)["name"] if customer_id else "",
        "flow": flow or context.default_flow,
        "flows": context.available_flows(),
        "busy_flow": context.busy_customer(username, manager) or context.busy_flow(excluding=flow or context.default_flow),
        "admin": admin,
    }


@app.get("/api/customers")
async def list_customers(username: str = Depends(current_user)) -> dict[str, Any]:
    owner = context.owner_id(username)
    return {"customers": context.customers().list(owner), "active": context.busy_customer(username)}


@app.get("/api/companies")
async def search_companies(q: str = "", username: str = Depends(current_user)) -> dict[str, Any]:
    return {"companies": context.customers().search_companies(q[:200])}


@app.post("/api/company/preview")
async def preview_company(payload: CustomerDraftRequest, username: str = Depends(current_user), _: None = Depends(require_csrf)) -> dict[str, Any]:
    return context.customers().preview(payload.values)


@app.post("/api/company/confirm")
async def confirm_company(payload: CompanyConfirmRequest, username: str = Depends(current_user), _: None = Depends(require_csrf), flow: str = "", customer_id: str = "") -> dict[str, Any]:
    flow_manager = context.jobs(flow)  # 先校验业务，避免非法路径或未配置流程写入。
    owner = context.owner_id(username)
    try:
        if customer_id:
            manager = context.customer_jobs(username, flow, customer_id)
            session = manager.session(username)
            if session.state["status"] in ACTIVE_STATES:
                if session.state["status"] != "paused":
                    raise HTTPException(status_code=409, detail="请暂停任务后再修改资料。")
                payload.values = manager.effective_application_values(username, payload.values)
                validate_application_values(payload.values, is_eori="EORI" in manager.flow_name.upper())
                manager.validate_active_company(session, payload.values)
                context.customers().save_draft(owner, customer_id, flow or context.default_flow, payload.values)
                return {"customer_id": customer_id, "customer_name": context.customers().get(owner, customer_id)["name"], "values": normalized_identity_values(payload.values), "association": "unchanged", "preview": context.customers().preview(payload.values)}
        validate_application_values(payload.values, is_eori="EORI" in flow_manager.flow_name.upper())
        return context.customers().confirm_company(owner, flow or context.default_flow, payload.values, customer_id, payload.independent)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/customers")
async def create_customer(payload: CustomerCreateRequest, username: str = Depends(current_user), _: None = Depends(require_csrf)) -> dict[str, Any]:
    return context.customers().create(context.owner_id(username), payload.name)


@app.get("/api/customer/draft")
async def customer_draft(username: str = Depends(current_user), flow: str = "", customer_id: str = "") -> dict[str, Any]:
    manager = context.customer_jobs(username, flow, customer_id)
    return context.customers().draft(*manager.customer_scope)


@app.post("/api/customer/draft")
async def save_customer_draft(payload: CustomerDraftRequest, username: str = Depends(current_user), _: None = Depends(require_csrf), flow: str = "", customer_id: str = "") -> dict[str, str]:
    manager = context.customer_jobs(username, flow, customer_id)
    if manager.session(username).state["status"] in ACTIVE_STATES - {"paused"}:
        raise HTTPException(status_code=409, detail="请暂停任务后再修改资料。")
    try:
        payload.values = manager.effective_application_values(username, payload.values)
        manager.validate_active_company(manager.session(username), payload.values)
        context.customers().save_draft(*manager.customer_scope, payload.values)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "saved"}


@app.get("/api/customer/runs")
async def customer_runs(username: str = Depends(current_user), flow: str = "", customer_id: str = "") -> dict[str, Any]:
    manager = context.customer_jobs(username, flow, customer_id)
    records = context.customers().runs(*manager.customer_scope)
    live = manager.session(username)
    for record in records:
        if record["status"] in ACTIVE_STATES:
            if live.run_record.get("id") == record["id"] and live.state["status"] in ACTIVE_STATES:
                record["status"] = live.state["status"]
            else:
                record.update(status="interrupted", message="服务中断，结果待核实；请勿直接重复提交。")
    return {"runs": records}


@app.get("/api/customer/run-document")
async def customer_run_document(run_id: str, kind: str, username: str = Depends(current_user), flow: str = "", customer_id: str = "") -> FileResponse:
    manager = context.customer_jobs(username, flow, customer_id)
    try:
        record = context.customers().run(*manager.customer_scope, run_id)
        name = record.get("documents", {}).get(kind, "") if kind in {"pdf", "png", "receipt_pdf", "receipt_png"} else ""
        if not name or Path(name).name != name:
            raise KeyError("无文件")
        folder = context.customers().folder(*manager.customer_scope) / "runs" / run_id
        path = (folder / name).resolve()
        if not path.is_relative_to(folder.resolve()) or not path.is_file():
            raise KeyError("无文件")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="未找到该客户的办理文件。") from exc
    return FileResponse(path, filename=f"{run_id}-{kind}{path.suffix}")


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
    flow: str = "",
    customer_id: str = "",
) -> dict[str, Any]:
    if request.enable_recording and not context.users.is_admin(username):
        raise HTTPException(status_code=403, detail="只有管理员可以开启浏览器操作录制。")
    try:
        manager = context.customer_jobs(username, flow, customer_id)
        busy = context.busy_customer(username) or context.busy_flow(excluding=flow or context.default_flow)
        if busy:
            raise RuntimeError(f"{busy['name']}已有任务运行，请先处理或结束该任务。")
        manager.start(username, request)
    except (OSError, ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "starting", "run": dict(manager.session(username).run_record)}


@app.post("/api/credentials/clear")
async def clear_credentials(
    username: str = Depends(current_user),
    _: None = Depends(require_csrf),
    flow: str = "",
    customer_id: str = "",
) -> dict[str, str]:
    """清掉本机内存里记住的 HMRC 登录信息（换账号时用）。"""
    try:
        manager = context.customer_jobs(username, flow, customer_id)
        for related in context.credential_managers(manager):
            related.clear_credentials(username)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "cleared"}


@app.post("/api/credentials/save")
async def save_credentials(
    request: CredentialSaveRequest | None = None,
    username: str = Depends(current_user),
    _: None = Depends(require_csrf),
    flow: str = "",
    customer_id: str = "",
) -> dict[str, Any]:
    """把上次运行的登录信息保存到本机私有存储，之后启动不用再手输。"""
    try:
        overrides = dict(request.credentials) if request is not None else {}
        project_code = (request.project_code if request is not None else "").strip()
        manager = context.customer_jobs(username, flow, customer_id)
        context.credential_managers(manager)
        result = manager.save_credentials(username, overrides, project_code)
        return {**result, "key": "当前客户"}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/code")
async def submit_code(
    request: CodeRequest,
    username: str = Depends(current_user),
    _: None = Depends(require_csrf),
    flow: str = "",
    customer_id: str = "",
) -> dict[str, str]:
    try:
        context.customer_jobs(username, flow, customer_id).submit_code(username, request.code)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "accepted"}


@app.post("/api/pause")
async def pause(
    username: str = Depends(current_user), _: None = Depends(require_csrf),
    flow: str = "",
    customer_id: str = "",
) -> dict[str, str]:
    try:
        context.customer_jobs(username, flow, customer_id).pause(username)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "pausing"}


@app.post("/api/stop")
async def stop(
    username: str = Depends(current_user), _: None = Depends(require_csrf),
    flow: str = "",
    customer_id: str = "",
) -> dict[str, str]:
    try:
        context.customer_jobs(username, flow, customer_id).stop(username)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "stopping"}


@app.post("/api/recording/continue")
async def continue_recording(
    username: str = Depends(current_admin), _: None = Depends(require_csrf),
    flow: str = "",
    customer_id: str = "",
) -> dict[str, str]:
    try:
        context.customer_jobs(username, flow, customer_id).continue_recording(username)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "starting"}


@app.post("/api/error-hold/resume")
async def error_hold_resume(
    username: str = Depends(current_user), _: None = Depends(require_csrf),
    flow: str = "",
    customer_id: str = "",
) -> dict[str, str]:
    """出错停留期间：人工确认已处理，程序从当前页继续。"""
    try:
        context.customer_jobs(username, flow, customer_id).resolve_error_hold(username, "resume")
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "resuming"}


@app.post("/api/error-hold/cancel")
async def error_hold_cancel(
    username: str = Depends(current_user), _: None = Depends(require_csrf),
    flow: str = "",
    customer_id: str = "",
) -> dict[str, str]:
    """出错停留期间：人工决定结束这一轮。"""
    try:
        context.customer_jobs(username, flow, customer_id).resolve_error_hold(username, "cancel")
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "cancelling"}


@app.post("/api/continue")
async def continue_automation(
    request: ContinueRequest,
    username: str = Depends(current_user),
    _: None = Depends(require_csrf),
    flow: str = "",
    customer_id: str = "",
) -> dict[str, str]:
    try:
        manager = context.customer_jobs(username, flow, customer_id)
        effective = manager.continue_after_pause(username, request)
        context.customers().save_draft(*manager.customer_scope, effective)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "continuing"}


@app.get("/api/final-review/pdf")
async def final_review_pdf(
    username: str = Depends(current_user), flow: str = "", customer_id: str = ""
) -> FileResponse:
    path = context.customer_jobs(username, flow, customer_id).final_review_document(username, "pdf") if customer_id else None
    if path is None:
        raise HTTPException(status_code=404, detail="当前没有可下载的最终复核 PDF。")
    return FileResponse(
        path, media_type="application/pdf", filename="check-your-answers.pdf"
    )


@app.get("/api/final-review/png")
async def final_review_png(
    username: str = Depends(current_user), flow: str = "", customer_id: str = ""
) -> FileResponse:
    path = context.customer_jobs(username, flow, customer_id).final_review_document(username, "png") if customer_id else None
    if path is None:
        raise HTTPException(status_code=404, detail="当前没有可查看的最终复核截图。")
    return FileResponse(
        path, media_type="image/png", filename="check-your-answers.png"
    )


@app.post("/api/final-review/confirm")
async def final_review_confirm(
    request: FinalSubmitRequest,
    username: str = Depends(current_user), _: None = Depends(require_csrf),
    flow: str = "",
    customer_id: str = "",
) -> dict[str, str]:
    if not request.confirmed:
        raise HTTPException(
            status_code=400, detail="请明确确认资料无误并同意提交到 HMRC。"
        )
    try:
        context.customer_jobs(username, flow, customer_id).confirm_final_review(username)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "confirmed"}


@app.post("/api/final-review/edit")
async def final_review_edit(
    request: FinalEditRequest,
    username: str = Depends(current_user), _: None = Depends(require_csrf),
    flow: str = "",
    customer_id: str = "",
) -> dict[str, str]:
    try:
        context.customer_jobs(username, flow, customer_id).request_final_review_edit(username, request.target)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "editing"}


@app.post("/api/final-review/edit/submit")
async def final_review_edit_submit(
    request: RemoteEditSubmitRequest,
    username: str = Depends(current_user),
    _: None = Depends(require_csrf),
    flow: str = "",
    customer_id: str = "",
) -> dict[str, str]:
    try:
        context.customer_jobs(username, flow, customer_id).submit_remote_edit(username, request)
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
    flow: str = "",
    customer_id: str = "",
) -> dict[str, Any]:
    if customer_id:
        context.customer_jobs(username, flow, customer_id)
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
    flow: str = "",
    customer_id: str = "",
) -> dict[str, Any]:
    manager = context.customer_jobs(username, flow, customer_id)
    documents: list[tuple[str, bytes]] = []
    for file in files:
        content = await file.read(25 * 1024 * 1024 + 1)
        documents.append((file.filename or "document", content))
    try:
        names = manager.store_identity_documents(username, documents, expected=manager.identity_documents_required)
    except (OSError, ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"count": len(names), "names": names}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="启动英国 VAT / EORI 自动化本地 Web UI")
    result.add_argument("--config", type=Path, default=Path("vat-config.flow.json"))
    result.add_argument(
        "--eori-config", type=Path,
        help="EORI 流程配置，默认读取主配置同目录的 vat-config.eori.flow.json",
    )
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
    context.users = UserStore(args.users.expanduser().resolve())
    try:
        context.configure_flows(args.config, args.eori_config)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"无法加载注册流程配置：{exc}") from exc
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
