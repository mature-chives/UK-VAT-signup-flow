from __future__ import annotations

import argparse
import asyncio
import atexit
import hmac
import math
import queue
import secrets
import shutil
import tempfile
import threading
import uuid
import webbrowser
import zipfile
from dataclasses import dataclass, field
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
    PageRule,
    Settings,
    build_address_answers,
    build_international_address_answers,
    load_settings,
)
from .document_parser import (
    extract_document,
    extracted_address,
    extracted_answers,
    extracted_birth_date,
    extracted_home_address,
)
from .runner import AutomationStopped, VatAutomation


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
ACTIVE_STATES = {"starting", "running", "waiting_code", "pausing", "paused", "reviewing"}
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


class ContinueRequest(BaseModel):
    extracted_values: dict[str, str] = Field(default_factory=dict)
    extracted_confirmed: bool = False


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
    pending_extracted_values: dict[str, str] = field(default_factory=dict)
    identity_documents: list[Path] = field(default_factory=list)
    active_identity_documents: list[Path] = field(default_factory=list)
    injected_rules: list[PageRule] = field(default_factory=list)
    final_review: dict[str, str] = field(default_factory=dict)
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
    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path
        self._lock = threading.Lock()
        self._sessions: dict[str, UserSession] = {}
        self._busy_with: str | None = None
        self._upload_root = Path(tempfile.mkdtemp(prefix="uk-vat-private-uploads-"))
        self._upload_root.chmod(0o700)
        atexit.register(shutil.rmtree, self._upload_root, True)

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
            return {
                **session.state,
                "events": list(session.state["events"]),
                "identity_documents": len(session.identity_documents),
                "final_review": {
                    "available": bool(session.final_review),
                    "pdf": bool(session.final_review.get("pdf")),
                    "png": bool(session.final_review.get("png")),
                },
                "user": session.username,
                "busy_with": busy if busy and busy != session.username else "",
                "config": self.config_path.name,
            }

    def start(self, username: str, request: StartRequest) -> None:
        session = self.session(username)
        if not self.config_path.is_file():
            raise ValueError(f"服务端配置文件不存在：{self.config_path}")
        if request.fresh_session and request.resume:
            raise ValueError("全新会话不能同时启用断点恢复。")
        if not request.extracted_confirmed:
            raise ValueError("请先在网页中检查并确认文档提取结果。")
        if len(session.identity_documents) != 3:
            raise ValueError("请先保存正好三份身份证明文件。")

        credentials = {
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
            self._busy_with = session.username
            session.state = {
                **_initial_state(),
                "status": "starting",
                "message": "正在加载配置并启动浏览器",
            }
            session.active_identity_documents = list(session.identity_documents)
            session.injected_rules = []
            session.final_review = {}
            session.pause_requested.clear()
            session.resume_event.clear()
            session.review_event.clear()
            session.pending_extracted_values = {}
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
            session.state["message"] = "已确认核对完毕，正在结束自动化"
            session.review_event.set()

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
        self, username: str, documents: list[tuple[str, bytes]]
    ) -> list[str]:
        allowed = {
            ".jpg", ".jpeg", ".bmp", ".png", ".pdf", ".doc", ".docx",
            ".xls", ".xlsx", ".gif", ".txt",
        }
        if len(documents) != 3:
            raise ValueError("请一次选择三份身份证明文件。")
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
                    "waiting_code", "pausing", "paused", "reviewing"
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

    async def _final_review(self, session: UserSession, info: dict[str, str]) -> None:
        with self._lock:
            session.final_review = {
                "pdf": info.get("pdf", ""),
                "png": info.get("screenshot", ""),
            }
            session.state["status"] = "reviewing"
            session.state["url"] = info.get("url", "")
            session.state["heading"] = info.get("heading", "")
            session.state["message"] = (
                "已到达 Check your answers，请逐项核对。"
                "程序不会提交，正式提交需在服务器的浏览器中人工完成。"
            )
        confirmed = await asyncio.to_thread(
            session.review_event.wait, FINAL_REVIEW_TIMEOUT_SECONDS
        )
        with self._lock:
            session.review_event.clear()
        if not confirmed:
            raise AutomationStopped(
                "最终复核等待超过 30 分钟，已自动结束并关闭浏览器。"
            )

    def _set_terminal_state(self, session: UserSession, status: str, message: str) -> None:
        with self._lock:
            session.state["status"] = status
            session.state["message"] = message
            if self._busy_with == session.username:
                self._busy_with = None

    def _apply_extracted_values(
        self, session: UserSession, settings: Settings, values: dict[str, str]
    ) -> None:
        """把提取结果合并进配置。

        每次暂停继续都会重新注入，所以先移除上一轮注入的规则，
        避免 settings.pages 随暂停次数无限增长。
        """
        previous = {id(rule) for rule in session.injected_rules}
        if previous:
            settings.pages = [
                page for page in settings.pages if id(page) not in previous
            ]
            session.injected_rules = []

        settings.answers.update(extracted_answers(values))
        settings.answers.update(build_address_answers(extracted_address(values)))
        home_address_answers = build_international_address_answers(
            extracted_home_address(values)
        )
        business_address_answers = build_international_address_answers(
            extracted_address(values)
        )

        def inject(path_contains: str, answers: dict[str, str]) -> None:
            rule = PageRule(path_contains=path_contains, answers=dict(answers))
            settings.pages.append(rule)
            session.injected_rules.append(rule)

        contact_paths = {
            "/register-for-vat/email-address": (
                {"email-address", "Email address"}, values.get("email", "")
            ),
            "/register-for-vat/telephone-number": (
                {"telephone-number", "Telephone number"}, values.get("phone", "")
            ),
            "/register-for-vat/business-email": (
                {"businessEmailAddress"}, values.get("vat_contact_email", "")
            ),
            "/register-for-vat/business-telephone-number": (
                {"daytimePhone"}, values.get("business_phone", "")
            ),
        }
        for page in settings.pages:
            if "/register-for-vat/application-reference" in page.path_contains:
                page.answers.pop("value", None)
            for path, (keys, _) in contact_paths.items():
                if path in page.path_contains:
                    for key in keys:
                        page.answers.pop(key, None)

        application_reference = values.get("application_reference", "")
        if application_reference:
            inject(
                "/register-for-vat/application-reference",
                {"value": application_reference},
            )
        for path, (keys, value) in contact_paths.items():
            if value:
                inject(path, {key: value for key in keys})
        if home_address_answers:
            inject(
                "/register-for-vat/home-address/international", home_address_answers
            )
        if business_address_answers:
            inject(
                "/register-for-vat/principal-place-business/international",
                business_address_answers,
            )

        birth_date = extracted_birth_date(values)
        for page in settings.pages:
            if "/date-of-birth" in page.path_contains and birth_date:
                page.answers.update(birth_date)
            if "/overseas-identifier" in page.path_contains:
                identifier = values.get("overseas_tax_identifier", "")
                if identifier:
                    page.answers.update(
                        {"tax-identifier-radio": "Yes", "tax-identifier": identifier}
                    )
            if "/file-upload/upload-document" in page.path_contains:
                if session.active_identity_documents:
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
            self._apply_extracted_values(session, settings, extracted_values)

            async def execute() -> None:
                async def pause_checkpoint(url: str, heading: str) -> None:
                    updates = await self._pause_checkpoint(session, url, heading)
                    if updates:
                        self._apply_extracted_values(session, settings, updates)

                runner = VatAutomation(
                    settings,
                    interactive=False,
                    credentials=credentials,
                    verification_code_provider=partial(
                        self._verification_code, session
                    ),
                    file_upload_provider=partial(self._identity_file, session),
                    file_uploads_remaining_provider=partial(
                        self._identity_files_remaining, session
                    ),
                    pause_checkpoint_provider=pause_checkpoint,
                    final_review_provider=partial(self._final_review, session),
                    event_handler=partial(self._event, session),
                    audit_context={"user": session.username},
                )
                await runner.run(resume=resume)

            if fresh_session:
                with tempfile.TemporaryDirectory(
                    prefix=f"uk-vat-web-profile-{session.username}-"
                ) as directory:
                    settings.profile_dir = Path(directory)
                    asyncio.run(execute())
            else:
                asyncio.run(execute())
        except AutomationStopped as exc:
            self._set_terminal_state(session, "stopped", str(exc))
        except Exception as exc:
            self._set_terminal_state(session, "failed", str(exc))
        else:
            self._set_terminal_state(session, "completed", "流程已完成")


@dataclass
class ServerContext:
    config_path: Path = Path("vat-config.json")
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
            self.manager = JobManager(self.config_path)
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
    username: str = Depends(current_user), _: None = Depends(require_csrf)
) -> dict[str, str]:
    try:
        context.jobs().confirm_final_review(username)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "confirmed"}


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
    result.add_argument("--config", type=Path, default=Path("vat-config.json"))
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
    context.config_path = args.config.expanduser().resolve()
    context.users = UserStore(args.users.expanduser().resolve())
    context.manager = None

    if not context.config_path.is_file():
        raise SystemExit(f"配置文件不存在：{context.config_path}")
    # uvicorn.run 不会返回，stdout 重定向到文件时块缓冲会吞掉这些提示，
    # 因此启动横幅必须立即 flush。
    def banner(text: str) -> None:
        print(text, flush=True)

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
