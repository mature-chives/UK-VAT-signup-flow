from __future__ import annotations

import argparse
import hmac
import math
import secrets
import threading
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from vat_automation import tls
from vat_automation.auth import (
    LoginThrottle,
    SessionSigner,
    UserStore,
    normalize_username,
)
from vat_automation.document_parser import extract_document
from vat_automation.web import (
    ALLOWED_ENV_KEYS,
    CSRF_COOKIE,
    CSRF_HEADER,
    LOCAL_HOSTS,
    SESSION_COOKIE,
    JobManager,
    StartRequest,
)

from .catalog import get_plugin, list_plugins, load_builtin_plugins
from .envfile import load_env_file
from .id_card_ocr import recognize_id_card_pack
from .id_card_render import build_task_id_card_pdf
from .id_card_translate import (
    RETRYABLE_VENDOR_CODES,
    VendorConfigError,
    VendorHttpError,
    VendorReject,
    async_translate_id_card_fields,
)
from .store import WorkbenchStore

ID_CARD_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}


STATIC_DIR = Path(__file__).resolve().parent / "static"
load_builtin_plugins()


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class SetupRequest(BaseModel):
    token: str = Field(min_length=1, max_length=128)
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class CustomerRequest(BaseModel):
    id: str = ""
    name: str = Field(min_length=1, max_length=200)
    project_code: str = ""
    notes: str = ""
    fields: dict[str, str] = Field(default_factory=dict)


class TaskCreateRequest(BaseModel):
    plugin_id: str
    customer_id: str
    title: str = ""
    field_keys: list[str] = Field(default_factory=list)
    file_ids: list[str] = Field(default_factory=list)


class UkVatStartRequest(BaseModel):
    credentials: dict[str, str] = Field(default_factory=dict)
    extracted_values: dict[str, str] = Field(default_factory=dict)
    extracted_confirmed: bool = False
    fresh_session: bool = True


class CodeRequest(BaseModel):
    code: str = Field(min_length=1, max_length=32)


class IdCardConfirmRequest(BaseModel):
    fields: dict[str, str] = Field(default_factory=dict)


class FinalSubmitRequest(BaseModel):
    confirmed: bool = False


class FinalEditRequest(BaseModel):
    target: str = Field(min_length=1, max_length=64)


class RemoteEditSubmitRequest(BaseModel):
    answers: dict[str, str | bool | list[str]] = Field(default_factory=dict)
    action: str = Field(default="", max_length=160)


@dataclass
class ServerContext:
    users: UserStore = field(default_factory=lambda: UserStore(Path("users.json")))
    signer: SessionSigner = field(default_factory=SessionSigner)
    throttle: LoginThrottle = field(default_factory=LoginThrottle)
    store: WorkbenchStore = field(
        default_factory=lambda: WorkbenchStore(Path("workbench-data"))
    )
    jobs: JobManager | None = None
    uk_vat_config: Path = Path("vat-config.flow.json")
    secure_cookies: bool = True
    setup_token: str | None = None
    task_owners: dict[str, str] = field(default_factory=dict)

    def job_manager(self) -> JobManager:
        if self.jobs is None:
            self.jobs = JobManager(self.uk_vat_config)
        return self.jobs

    def needs_setup(self) -> bool:
        return self.users.is_empty()

    def ensure_setup_token(self) -> str:
        if self.setup_token is None:
            self.setup_token = secrets.token_urlsafe(12)
        return self.setup_token


context = ServerContext()
app = FastAPI(title="销售交付工作台", docs_url=None, redoc_url=None)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin") or request.headers.get("referer") or ""
    parsed = urlparse(origin)
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
    if username is None or not context.users.exists(username):
        return None
    return username


async def current_user(request: Request) -> str:
    username = _session_user(request)
    if username is None:
        raise HTTPException(status_code=401, detail="请先登录。")
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


def _page(name: str) -> str:
    return (STATIC_DIR / name).read_text(encoding="utf-8")


@app.get("/assets/{name}")
async def asset(name: str) -> FileResponse:
    path = (STATIC_DIR / name).resolve()
    if path.parent != STATIC_DIR.resolve() or path.suffix != ".css" or not path.is_file():
        raise HTTPException(status_code=404, detail="资源不存在。")
    return FileResponse(path, media_type="text/css")


def _public_customer(record: dict[str, Any]) -> dict[str, Any]:
    files = [
        {"id": item.get("id"), "name": item.get("name"), "category": item.get("category")}
        for item in record.get("files", [])
    ]
    return {**record, "files": files}


def _public_task(record: dict[str, Any]) -> dict[str, Any]:
    safe_files = [
        {"id": item.get("id"), "name": item.get("name"), "kind": item.get("kind")}
        for item in record.get("result_files", [])
    ]
    selected = [
        {"id": item.get("id"), "name": item.get("name"), "category": item.get("category")}
        for item in record.get("selected_files", [])
    ]
    return {**record, "result_files": safe_files, "selected_files": selected}


def _sync_uk_vat_task(task: dict[str, Any]) -> dict[str, Any]:
    owner = context.task_owners.get(task["id"])
    if not owner:
        return task
    snap = context.job_manager().snapshot(owner)
    mapping = {
        "idle": task.get("status"),
        "starting": "in_progress",
        "running": "in_progress",
        "waiting_code": "waiting",
        "pausing": "in_progress",
        "paused": "waiting",
        "reviewing": "reviewing",
        "editing": "reviewing",
        "stopped": "blocked",
        "failed": "blocked",
        "completed": "delivered",
    }
    status = mapping.get(str(snap.get("status", "")), task.get("status"))
    try:
        return context.store.update_task(
            task["id"],
            status=status,
            message=snap.get("message", task.get("message")),
            uk_vat=snap,
        )
    except KeyError:
        return task


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> Response:
    if context.needs_setup():
        return RedirectResponse("/setup", status_code=303)
    if _session_user(request) is None:
        return RedirectResponse("/login", status_code=303)
    return HTMLResponse(_page("index.html"))


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> Response:
    if context.needs_setup():
        return RedirectResponse("/setup", status_code=303)
    if _session_user(request) is not None:
        return RedirectResponse("/", status_code=303)
    return HTMLResponse(_page("login.html"))


@app.get("/setup", response_class=HTMLResponse)
async def setup_page() -> Response:
    if not context.needs_setup():
        return RedirectResponse("/login", status_code=303)
    return HTMLResponse(_page("setup.html"))


@app.post("/api/setup")
async def setup(
    request: Request, payload: SetupRequest, response: Response, _: None = Depends(require_origin)
) -> dict[str, str]:
    if not context.needs_setup():
        raise HTTPException(status_code=409, detail="管理员已存在，请直接登录。")
    keys = (f"ip:{_client_ip(request)}", "setup")
    waiting = context.throttle.retry_after(*keys)
    if waiting > 0:
        raise HTTPException(status_code=429, detail=f"请 {math.ceil(waiting)} 秒后再试。")
    if not hmac.compare_digest(payload.token.strip(), context.ensure_setup_token()):
        context.throttle.record_failure(*keys)
        raise HTTPException(status_code=403, detail="初始化码不正确。")
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
    request: Request, payload: LoginRequest, response: Response, _: None = Depends(require_origin)
) -> dict[str, str]:
    name = normalize_username(payload.username)
    keys = (f"ip:{_client_ip(request)}", f"user:{name}")
    waiting = context.throttle.retry_after(*keys)
    if waiting > 0:
        raise HTTPException(status_code=429, detail=f"请 {math.ceil(waiting)} 秒后再试。")
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


@app.get("/api/me")
async def me(username: str = Depends(current_user)) -> dict[str, Any]:
    return {"user": username, "admin": context.users.is_admin(username)}


@app.get("/api/plugins")
async def plugins(_: str = Depends(current_user)) -> dict[str, Any]:
    return {"plugins": [item.public() for item in list_plugins()]}


@app.get("/api/customers")
async def customers(_: str = Depends(current_user)) -> dict[str, Any]:
    return {"customers": [_public_customer(item) for item in context.store.list_customers()]}


@app.post("/api/customers")
async def save_customer(
    payload: CustomerRequest, _: str = Depends(current_user), __: None = Depends(require_csrf)
) -> dict[str, Any]:
    try:
        record = context.store.upsert_customer(
            customer_id=payload.id or None,
            name=payload.name,
            project_code=payload.project_code,
            fields=payload.fields,
            notes=payload.notes,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="客户不存在。") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _public_customer(record)


@app.post("/api/customers/{customer_id}/files")
async def upload_customer_file(
    customer_id: str,
    category: str = "other",
    file: UploadFile = File(...),
    _: str = Depends(current_user),
    __: None = Depends(require_csrf),
) -> dict[str, Any]:
    content = await file.read(25 * 1024 * 1024 + 1)
    try:
        record = context.store.add_customer_file(
            customer_id,
            filename=file.filename or "file.bin",
            content=content,
            category=category,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="客户不存在。") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"id": record["id"], "name": record["name"], "category": record["category"]}


@app.post("/api/customers/{customer_id}/parse/{file_id}")
async def parse_customer_file(
    customer_id: str,
    file_id: str,
    _: str = Depends(current_user),
    __: None = Depends(require_csrf),
) -> dict[str, Any]:
    item = context.store.customer_file(customer_id, file_id)
    if item is None:
        raise HTTPException(status_code=404, detail="客户文件不存在。")
    path = Path(str(item["path"]))
    if not path.is_file():
        raise HTTPException(status_code=404, detail="文件已失效。")
    try:
        parsed = extract_document(str(item["name"]), path.read_bytes())
    except (OSError, ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    customer = context.store.get_customer(customer_id) or {}
    merged = {**customer.get("fields", {}), **parsed.get("values", {})}
    context.store.upsert_customer(
        customer_id=customer_id,
        name=str(customer.get("name") or parsed.get("values", {}).get("business_name") or "客户"),
        project_code=str(
            parsed.get("values", {}).get("project_code") or customer.get("project_code") or ""
        ),
        fields=merged,
        notes=str(customer.get("notes", "")),
    )
    return parsed


@app.get("/api/tasks")
async def tasks(_: str = Depends(current_user)) -> dict[str, Any]:
    records = []
    for item in context.store.list_tasks():
        if item.get("task_kind") == "uk_vat":
            item = _sync_uk_vat_task(item)
        records.append(_public_task(item))
    return {"tasks": records}


@app.post("/api/tasks")
async def create_task(
    payload: TaskCreateRequest,
    username: str = Depends(current_user),
    _: None = Depends(require_csrf),
) -> dict[str, Any]:
    plugin = get_plugin(payload.plugin_id)
    if plugin is None:
        raise HTTPException(status_code=404, detail="未登记的业务插件。")
    try:
        record = context.store.create_task(
            plugin_id=plugin.id,
            plugin_name=plugin.name,
            task_kind=plugin.task_kind,
            customer_id=payload.customer_id,
            title=payload.title,
            created_by=username,
            field_keys=payload.field_keys,
            file_ids=payload.file_ids,
            placeholder=plugin.placeholder,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="客户不存在。") from None
    return _public_task(record)


@app.post("/api/tasks/{task_id}/uk-vat/start")
async def start_uk_vat(
    task_id: str,
    payload: UkVatStartRequest,
    username: str = Depends(current_user),
    _: None = Depends(require_csrf),
) -> dict[str, str]:
    task = context.store.get_task(task_id)
    if task is None or task.get("task_kind") != "uk_vat":
        raise HTTPException(status_code=404, detail="英国 VAT 任务不存在。")
    if not payload.extracted_confirmed:
        raise HTTPException(status_code=400, detail="请先确认客户资料。")
    identity = [
        item
        for item in task.get("selected_files", [])
        if item.get("category") == "identity"
    ]
    if len(identity) != 3:
        raise HTTPException(status_code=400, detail="请选择正好三份身份证明。")
    manager = context.job_manager()
    documents: list[tuple[str, bytes]] = []
    for item in identity:
        path = Path(str(item["path"]))
        if not path.is_file():
            raise HTTPException(status_code=400, detail=f"身份证明已失效：{item.get('name')}")
        documents.append((str(item["name"]), path.read_bytes()))
    manager.store_identity_documents(username, documents)
    values = payload.extracted_values or dict(task.get("selected_fields") or {})
    request = StartRequest(
        fresh_session=payload.fresh_session,
        resume=False,
        credentials={
            key: value
            for key, value in payload.credentials.items()
            if key in ALLOWED_ENV_KEYS
        },
        extracted_values=values,
        extracted_confirmed=True,
    )
    try:
        manager.start(username, request)
    except (OSError, ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    context.task_owners[task_id] = username
    context.store.update_task(task_id, status="in_progress", message="已启动服务器本机 Chrome")
    return {"status": "starting"}


@app.post("/api/tasks/{task_id}/uk-vat/code")
async def uk_vat_code(
    task_id: str,
    payload: CodeRequest,
    username: str = Depends(current_user),
    _: None = Depends(require_csrf),
) -> dict[str, str]:
    if context.task_owners.get(task_id) != username:
        raise HTTPException(status_code=409, detail="当前没有你的英国 VAT 运行任务。")
    try:
        context.job_manager().submit_code(username, payload.code)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "accepted"}


@app.post("/api/tasks/{task_id}/uk-vat/final-review/confirm")
async def uk_vat_confirm(
    task_id: str,
    payload: FinalSubmitRequest,
    username: str = Depends(current_user),
    _: None = Depends(require_csrf),
) -> dict[str, str]:
    if not payload.confirmed:
        raise HTTPException(status_code=400, detail="请明确确认后再提交。")
    if context.task_owners.get(task_id) != username:
        raise HTTPException(status_code=409, detail="当前没有你的英国 VAT 运行任务。")
    try:
        context.job_manager().confirm_final_review(username)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "confirmed"}


@app.post("/api/tasks/{task_id}/uk-vat/final-review/edit")
async def uk_vat_edit(
    task_id: str,
    payload: FinalEditRequest,
    username: str = Depends(current_user),
    _: None = Depends(require_csrf),
) -> dict[str, str]:
    if context.task_owners.get(task_id) != username:
        raise HTTPException(status_code=409, detail="当前没有你的英国 VAT 运行任务。")
    try:
        context.job_manager().request_final_review_edit(username, payload.target)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "editing"}


@app.post("/api/tasks/{task_id}/uk-vat/final-review/edit/submit")
async def uk_vat_edit_submit(
    task_id: str,
    payload: RemoteEditSubmitRequest,
    username: str = Depends(current_user),
    _: None = Depends(require_csrf),
) -> dict[str, str]:
    if context.task_owners.get(task_id) != username:
        raise HTTPException(status_code=409, detail="当前没有你的英国 VAT 运行任务。")
    from vat_automation.web import RemoteEditSubmitRequest as WebRemote

    try:
        context.job_manager().submit_remote_edit(
            username, WebRemote(answers=payload.answers, action=payload.action)
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "applying"}


@app.get("/api/tasks/{task_id}/uk-vat/final-review/pdf")
async def uk_vat_pdf(
    task_id: str, username: str = Depends(current_user)
) -> FileResponse:
    if context.task_owners.get(task_id) != username:
        raise HTTPException(status_code=404, detail="没有可下载的复核文件。")
    path = context.job_manager().final_review_document(username, "pdf")
    if path is None:
        raise HTTPException(status_code=404, detail="当前没有最终复核 PDF。")
    return FileResponse(path, media_type="application/pdf", filename="check-your-answers.pdf")


@app.post("/api/tasks/{task_id}/id-card/ocr")
async def ocr_id_card_task(
    task_id: str,
    _: str = Depends(current_user),
    __: None = Depends(require_csrf),
) -> dict[str, Any]:
    task = context.store.get_task(task_id)
    if task is None or task.get("plugin_id") != "translate-id":
        raise HTTPException(status_code=404, detail="证件照翻译任务不存在。")
    paths: list[Path] = []
    for item in task.get("selected_files") or []:
        path = Path(str(item.get("path") or ""))
        if path.is_file() and path.suffix.casefold() in ID_CARD_IMAGE_SUFFIXES:
            paths.append(path)
    if not paths:
        raise HTTPException(status_code=400, detail="请先在新建任务时勾选证件照片。")
    try:
        pack = recognize_id_card_pack(paths)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (OSError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"识别失败：{exc}") from exc
    fields = pack.get("fields") or {}
    if not fields:
        raise HTTPException(
            status_code=400,
            detail="未能从图片中抽出身份证字段，请换一张更清晰的正面照。",
        )
    path_index = {str(Path(str(item.get("path") or "")).resolve()): item for item in task.get("selected_files") or []}
    sides = []
    for item in pack.get("sides") or []:
        source = path_index.get(str(Path(str(item.get("path") or "")).resolve()), {})
        sides.append(
            {
                "file_id": source.get("id") or "",
                "name": source.get("name") or Path(str(item.get("path") or "")).name,
                "side": item.get("side") or "unknown",
                "portrait_bbox": item.get("portrait_bbox"),
            }
        )
    updated = context.store.update_task(
        task_id,
        extracted_id_card={"fields": fields, "sides": sides},
        status="in_progress",
        message="已识别证件字段，待核对",
    )
    return {"fields": fields, "task": _public_task(updated)}


@app.post("/api/tasks/{task_id}/id-card/confirm")
async def confirm_id_card_task(
    task_id: str,
    payload: IdCardConfirmRequest,
    _: str = Depends(current_user),
    __: None = Depends(require_csrf),
) -> dict[str, Any]:
    task = context.store.get_task(task_id)
    if task is None or task.get("plugin_id") != "translate-id":
        raise HTTPException(status_code=404, detail="证件照翻译任务不存在。")
    extracted = dict((task.get("extracted_id_card") or {}).get("fields") or {})
    edited = {key: str(value).strip() for key, value in payload.fields.items() if str(value).strip()}
    fields = {**extracted, **edited}
    if not fields:
        raise HTTPException(status_code=400, detail="请先识别并核对证件字段。")
    try:
        translated = await async_translate_id_card_fields(fields)
    except VendorReject as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except VendorConfigError as exc:
        raise HTTPException(status_code=400, detail="未配置翻译服务") from exc
    except VendorHttpError as exc:
        detail = str(exc) or "没法生成翻译件，请稍后再试。若连续失败，请联系管理员。"
        retryable = exc.code in RETRYABLE_VENDOR_CODES
        context.store.update_task(
            task_id,
            extracted_id_card={
                **(task.get("extracted_id_card") or {}),
                "fields": fields,
                "translate_error": detail,
            },
            status="in_progress" if retryable else "blocked",
            message=detail,
        )
        raise HTTPException(status_code=502, detail=detail) from exc
    try:
        pdf = build_task_id_card_pdf(task, translated)
    except (OSError, ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=f"生成翻译件失败：{exc}") from exc
    try:
        item = context.store.add_task_result_file(
            task_id,
            filename="id-card-translation.pdf",
            content=pdf,
            kind="translation",
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    extracted_card = dict(task.get("extracted_id_card") or {})
    extracted_card.pop("translate_error", None)
    context.store.update_task(
        task_id,
        extracted_id_card={
            **extracted_card,
            "fields": fields,
            "translated": translated,
        },
        status="delivered",
        message="已生成身份证翻译件",
    )
    latest = context.store.get_task(task_id) or {}
    return {"file": item, "translated": translated, "task": _public_task(latest)}


@app.post("/api/tasks/{task_id}/translate/{kind}")
async def upload_translation(
    task_id: str,
    kind: str,
    file: UploadFile = File(...),
    _: str = Depends(current_user),
    __: None = Depends(require_csrf),
) -> dict[str, Any]:
    if kind not in {"source", "translation"}:
        raise HTTPException(status_code=404, detail="不支持的上传类型。")
    task = context.store.get_task(task_id)
    if task is None or task.get("task_kind") != "translate":
        raise HTTPException(status_code=404, detail="翻译任务不存在。")
    content = await file.read(25 * 1024 * 1024 + 1)
    try:
        record = context.store.add_task_result_file(
            task_id,
            filename=file.filename or "file.bin",
            content=content,
            kind=kind,
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"id": record["id"], "name": record["name"], "kind": record["kind"]}


@app.get("/api/tasks/{task_id}/files/{file_id}")
async def download_task_file(
    task_id: str, file_id: str, _: str = Depends(current_user)
) -> FileResponse:
    task = context.store.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在。")
    for item in task.get("result_files", []):
        if item.get("id") == file_id:
            path = Path(str(item["path"]))
            if not path.is_file():
                break
            return FileResponse(path, filename=str(item.get("name") or path.name))
    raise HTTPException(status_code=404, detail="文件不存在。")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="启动销售交付工作台")
    result.add_argument("--users", type=Path, default=Path("users.json"))
    result.add_argument("--data", type=Path, default=Path("workbench-data"))
    result.add_argument("--uk-vat-config", type=Path, default=Path("vat-config.flow.json"))
    result.add_argument("--cert-dir", type=Path, default=Path("certs"))
    result.add_argument("--host", default="127.0.0.1")
    result.add_argument("--port", type=int, default=8770)
    result.add_argument("--no-open", action="store_true")
    result.add_argument("--ssl-certfile", type=Path)
    result.add_argument("--ssl-keyfile", type=Path)
    result.add_argument("--insecure-http", action="store_true")
    return result


def main() -> None:
    env_path = load_env_file()
    args = parser().parse_args()
    context.users = UserStore(args.users.expanduser().resolve())
    context.store = WorkbenchStore(args.data.expanduser().resolve())
    context.uk_vat_config = args.uk_vat_config.expanduser().resolve()
    context.jobs = None
    context.task_owners = {}

    def banner(text: str) -> None:
        print(text, flush=True)

    if env_path is not None:
        banner(f"已从 {env_path} 读取环境变量；已在 shell 里设置过的同名变量不会被覆盖。")

    if context.needs_setup():
        token = context.ensure_setup_token()
        banner("=" * 56)
        banner("还没有任何账号。打开网页创建管理员，初始化码：\n")
        banner(f"    {token}\n")
        banner("=" * 56)

    is_local = args.host in LOCAL_HOSTS
    certificate = args.ssl_certfile
    key = args.ssl_keyfile
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
    if not args.no_open:
        threading.Timer(
            1.0, lambda: webbrowser.open(f"{scheme}://{display_host}:{args.port}")
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
