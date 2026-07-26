from __future__ import annotations

import argparse
import asyncio
import atexit
import os
import queue
import shutil
import tempfile
import threading
import uuid
import webbrowser
import zipfile
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from .config import (
    PageRule,
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
ACTIVE_STATES = {"starting", "running", "waiting_code", "pausing", "paused"}


class StartRequest(BaseModel):
    config_path: str = "vat-config.test.json"
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


class JobManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._code_queue: queue.Queue[str] = queue.Queue(maxsize=1)
        self._thread: threading.Thread | None = None
        self._pause_requested = threading.Event()
        self._resume_event = threading.Event()
        self._pending_extracted_values: dict[str, str] = {}
        self._upload_dir = Path(tempfile.mkdtemp(prefix="uk-vat-private-uploads-"))
        self._upload_dir.chmod(0o700)
        self._identity_documents: list[Path] = []
        self._active_identity_documents: list[Path] = []
        atexit.register(shutil.rmtree, self._upload_dir, True)
        self._state: dict[str, Any] = {
            "status": "idle",
            "message": "尚未启动",
            "heading": "",
            "url": "",
            "events": [],
            "identity_documents": 0,
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                **self._state,
                "events": list(self._state["events"]),
                "identity_documents": len(self._identity_documents),
            }

    def start(self, request: StartRequest) -> None:
        config_path = Path(request.config_path).expanduser().resolve()
        if not config_path.is_file():
            raise ValueError(f"配置文件不存在：{config_path}")
        if request.fresh_session and request.resume:
            raise ValueError("全新会话不能同时启用断点恢复。")
        if not request.extracted_confirmed:
            raise ValueError("请先在网页中检查并确认文档提取结果。")

        credentials = {
            key: value.strip()
            for key, value in request.credentials.items()
            if key in ALLOWED_ENV_KEYS and value.strip()
        }
        with self._lock:
            if self._state["status"] in ACTIVE_STATES:
                raise RuntimeError("已有自动化任务正在运行。")
            self._state = {
                "status": "starting",
                "message": "正在加载配置并启动浏览器",
                "heading": "",
                "url": "",
                "events": [],
                "identity_documents": len(self._identity_documents),
            }
            self._active_identity_documents = list(self._identity_documents)
            self._pause_requested.clear()
            self._resume_event.clear()
            self._pending_extracted_values = {}
            while not self._code_queue.empty():
                try:
                    self._code_queue.get_nowait()
                except queue.Empty:
                    break
            self._thread = threading.Thread(
                target=self._run,
                args=(
                    config_path,
                    credentials,
                    request.extracted_values,
                    request.fresh_session,
                    request.resume,
                ),
                daemon=True,
                name="vat-automation-job",
            )
            self._thread.start()

    def submit_code(self, code: str) -> None:
        clean = code.strip()
        with self._lock:
            if self._state["status"] != "waiting_code":
                raise RuntimeError("当前任务没有等待验证码。")
            self._state["status"] = "running"
            self._state["message"] = "验证码已收到，正在继续"
        try:
            self._code_queue.put_nowait(clean)
        except queue.Full as exc:
            raise RuntimeError("验证码已经提交，请等待页面处理。") from exc

    def pause(self) -> None:
        with self._lock:
            if self._state["status"] != "running":
                raise RuntimeError("只能在自动化正在运行时暂停。")
            self._pause_requested.set()
            self._state["status"] = "pausing"
            self._state["message"] = "正在等待当前页面操作到达安全暂停点"

    def continue_after_pause(self, request: ContinueRequest) -> None:
        if not request.extracted_confirmed:
            raise ValueError("修改资料后请先点击“确认资料无误”。")
        with self._lock:
            if self._state["status"] != "paused":
                raise RuntimeError("当前任务未处于已暂停状态。")
            self._pending_extracted_values = dict(request.extracted_values)
            self._state["message"] = "正在应用修改后的资料并继续"
            self._resume_event.set()

    def store_identity_documents(
        self, documents: list[tuple[str, bytes]]
    ) -> list[str]:
        allowed = {
            ".jpg", ".jpeg", ".bmp", ".png", ".pdf", ".doc", ".docx",
            ".xls", ".xlsx", ".gif", ".txt",
        }
        if len(documents) != 3:
            raise ValueError("请一次选择三份身份证明文件。")
        saved: list[Path] = []
        with self._lock:
            if self._state["status"] in ACTIVE_STATES:
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
                    path = self._upload_dir / f"{uuid.uuid4().hex}{suffix}"
                    path.write_bytes(content)
                    path.chmod(0o600)
                    saved.append(path)
            except Exception:
                for path in saved:
                    path.unlink(missing_ok=True)
                raise
            for old in self._identity_documents:
                old.unlink(missing_ok=True)
            self._identity_documents = saved
            self._state["identity_documents"] = len(saved)
        return [name for name, _ in documents]

    async def _verification_code(self, heading: str) -> str:
        with self._lock:
            self._state["status"] = "waiting_code"
            self._state["heading"] = heading
            self._state["message"] = "请在网页中输入刚收到的验证码"
        return await asyncio.to_thread(self._code_queue.get)

    async def _event(self, record: dict[str, Any]) -> None:
        safe = {
            key: value
            for key, value in record.items()
            if key in {"time", "event", "step", "url", "heading", "text", "reason"}
        }
        with self._lock:
            if record.get("event") == "page":
                self._state["heading"] = str(record.get("heading", ""))
                self._state["url"] = str(record.get("url", ""))
                if self._state["status"] not in {
                    "waiting_code", "pausing", "paused"
                }:
                    self._state["status"] = "running"
                    self._state["message"] = "浏览器自动化正在运行"
            events = self._state["events"]
            events.append(safe)
            del events[:-40]

    async def _identity_file(self, _label: str) -> str | None:
        with self._lock:
            if not self._active_identity_documents:
                return None
            return str(self._active_identity_documents.pop(0))

    async def _identity_files_remaining(self) -> bool:
        with self._lock:
            return bool(self._active_identity_documents)

    async def _pause_checkpoint(self, url: str, heading: str) -> dict[str, str]:
        if not self._pause_requested.is_set():
            return {}
        with self._lock:
            self._state["status"] = "paused"
            self._state["message"] = "已暂停，可修改资料并重新确认后继续"
            self._state["url"] = url
            self._state["heading"] = heading
        await asyncio.to_thread(self._resume_event.wait)
        with self._lock:
            updates = dict(self._pending_extracted_values)
            self._pending_extracted_values = {}
            self._resume_event.clear()
            self._pause_requested.clear()
            self._state["status"] = "running"
            self._state["message"] = "已应用修改后的资料，正在继续"
        return updates

    def _set_terminal_state(self, status: str, message: str) -> None:
        with self._lock:
            self._state["status"] = status
            self._state["message"] = message

    def _apply_extracted_values(self, settings: Any, values: dict[str, str]) -> None:
        settings.answers.update(extracted_answers(values))
        settings.answers.update(build_address_answers(extracted_address(values)))
        home_address_answers = build_international_address_answers(
            extracted_home_address(values)
        )
        business_address_answers = build_international_address_answers(
            extracted_address(values)
        )
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
        application_reference = values.get("application_reference", "")
        if application_reference:
            settings.pages.append(
                PageRule(
                    path_contains="/register-for-vat/application-reference",
                    answers={"value": application_reference},
                )
            )
        for page in settings.pages:
            for path, (keys, _) in contact_paths.items():
                if path in page.path_contains:
                    for key in keys:
                        page.answers.pop(key, None)
        for path, (keys, value) in contact_paths.items():
            if value:
                settings.pages.append(
                    PageRule(
                        path_contains=path,
                        answers={key: value for key in keys},
                    )
                )
        if home_address_answers:
            settings.pages.append(
                PageRule(
                    path_contains="/register-for-vat/home-address/international",
                    answers=home_address_answers,
                )
            )
        if business_address_answers:
            settings.pages.append(
                PageRule(
                    path_contains="/register-for-vat/principal-place-business/international",
                    answers=business_address_answers,
                )
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
                if self._active_identity_documents:
                    page.action = "continue"

    def _run(
        self,
        config_path: Path,
        credentials: dict[str, str],
        extracted_values: dict[str, str],
        fresh_session: bool,
        resume: bool,
    ) -> None:
        previous = {key: os.environ.get(key) for key in credentials}
        os.environ.update(credentials)
        try:
            settings = load_settings(config_path)
            self._apply_extracted_values(settings, extracted_values)

            async def execute() -> None:
                async def pause_checkpoint(url: str, heading: str) -> None:
                    updates = await self._pause_checkpoint(url, heading)
                    if updates:
                        self._apply_extracted_values(settings, updates)

                runner = VatAutomation(
                    settings,
                    interactive=False,
                    verification_code_provider=self._verification_code,
                    file_upload_provider=self._identity_file,
                    file_uploads_remaining_provider=self._identity_files_remaining,
                    pause_checkpoint_provider=pause_checkpoint,
                    event_handler=self._event,
                )
                await runner.run(resume=resume)

            if fresh_session:
                with tempfile.TemporaryDirectory(
                    prefix="uk-vat-web-profile-"
                ) as directory:
                    settings.profile_dir = Path(directory)
                    asyncio.run(execute())
            else:
                asyncio.run(execute())
        except AutomationStopped as exc:
            self._set_terminal_state("stopped", str(exc))
        except Exception as exc:
            self._set_terminal_state("failed", str(exc))
        else:
            self._set_terminal_state("completed", "流程已完成")
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


manager = JobManager()
app = FastAPI(title="UK VAT Automation", docs_url=None, redoc_url=None)


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return INDEX_HTML


@app.get("/api/status")
async def status() -> dict[str, Any]:
    return manager.snapshot()


@app.post("/api/start")
async def start(request: StartRequest) -> dict[str, str]:
    try:
        manager.start(request)
    except (OSError, ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "starting"}


@app.post("/api/code")
async def submit_code(request: CodeRequest) -> dict[str, str]:
    try:
        manager.submit_code(request.code)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "accepted"}


@app.post("/api/pause")
async def pause() -> dict[str, str]:
    try:
        manager.pause()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "pausing"}


@app.post("/api/continue")
async def continue_automation(request: ContinueRequest) -> dict[str, str]:
    try:
        manager.continue_after_pause(request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "continuing"}


@app.post("/api/parse-document")
async def parse_document(file: UploadFile = File(...)) -> dict[str, Any]:
    content = await file.read(12 * 1024 * 1024 + 1)
    if len(content) > 12 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="资料文档不能超过 12MB。")
    try:
        return extract_document(file.filename or "document.txt", content)
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/identity-documents")
async def identity_documents(files: list[UploadFile] = File(...)) -> dict[str, Any]:
    documents: list[tuple[str, bytes]] = []
    for file in files:
        content = await file.read(25 * 1024 * 1024 + 1)
        documents.append((file.filename or "document", content))
    try:
        names = manager.store_identity_documents(documents)
    except (OSError, ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"count": len(names), "names": names}


def main() -> None:
    parser = argparse.ArgumentParser(description="启动英国 VAT 自动化本地 Web UI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()

    if args.host not in {"127.0.0.1", "localhost"}:
        raise SystemExit("为保护 HMRC 凭据，Web UI 只允许绑定到本机地址。")
    if not args.no_open:
        threading.Timer(
            1.0, lambda: webbrowser.open(f"http://127.0.0.1:{args.port}")
        ).start()

    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>UK VAT 注册助手</title>
  <style>
    :root { color-scheme:light; --ink:#18212b; --muted:#66717d; --blue:#1d70b8; --green:#00703c; --red:#d4351c; --bg:#f3f5f7; --line:#d6dbe0; }
    * { box-sizing:border-box; }
    body { margin:0; background:var(--bg); color:var(--ink); font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
    header { background:#0b0c0c; color:white; padding:18px 0; }
    header div,main { width:min(1080px,calc(100% - 32px)); margin:auto; }
    header strong { font-size:22px; }
    main { display:grid; grid-template-columns:minmax(0,1.35fr) minmax(300px,.65fr); gap:24px; padding:32px 0 56px; }
    .card { background:white; border:1px solid var(--line); border-radius:10px; padding:24px; box-shadow:0 2px 10px #0000000d; }
    h1,h2,h3 { margin:0 0 16px; line-height:1.25; } h1 { font-size:30px; } h2 { font-size:21px; } h3 { font-size:18px; margin-top:26px; }
    label { display:block; font-weight:650; margin:14px 0 6px; }
    input,select,textarea { width:100%; padding:10px 12px; border:2px solid #505a5f; border-radius:3px; font:inherit; background:white; }
    textarea { min-height:82px; resize:vertical; }
    input:focus,select:focus,textarea:focus { outline:3px solid #ffdd00; border-color:#0b0c0c; }
    input[type=file] { border:1px dashed #7a858e; background:#fafafa; }
    .row { display:grid; grid-template-columns:1fr 1fr; gap:14px; }
    button { margin-top:14px; padding:10px 18px; border:0; border-radius:3px; background:var(--green); color:white; font:700 16px inherit; cursor:pointer; box-shadow:0 2px 0 #003d20; }
    button.secondary { background:var(--blue); box-shadow:0 2px 0 #003b67; }
    button:disabled { opacity:.5; cursor:not-allowed; }
    .section { border-top:1px solid var(--line); margin-top:24px; padding-top:4px; }
    .hint,.meta { overflow-wrap:anywhere; color:var(--muted); font-size:14px; margin-top:5px; }
    .notice { padding:12px 14px; margin-bottom:20px; font-size:14px; background:#fff7bf; border-left:5px solid #ffdd00; }
    .local { color:#004f2d; background:#e8f4ee; border-left-color:var(--green); }
    .message { min-height:22px; margin-top:8px; font-size:14px; }
    .message.error { color:var(--red); }
    .message.ok { color:var(--green); }
    .warnings { color:#7a3b00; padding-left:22px; }
    .fields { display:grid; grid-template-columns:1fr 1fr; gap:0 14px; margin-top:10px; }
    .fields .wide { grid-column:1/-1; }
    details { margin-top:20px; border:1px solid var(--line); padding:12px 14px; }
    summary { cursor:pointer; font-weight:700; }
    .status { border-left:6px solid var(--blue); padding:13px 15px; background:#eaf4fb; margin-bottom:18px; }
    .status.waiting_code { border-color:#f47738; background:#fff3e8; }
    .status.pausing,.status.paused { border-color:#ffdd00; background:#fff7bf; }
    .status.failed,.status.stopped { border-color:var(--red); background:#fbeaea; }
    .status.completed { border-color:var(--green); background:#e8f4ee; }
    #otp { display:none; padding:16px; border:2px solid #f47738; margin:18px 0; background:#fff9f3; }
    .events { max-height:360px; overflow:auto; padding:0; list-style:none; border-top:1px solid var(--line); }
    .events li { padding:9px 0; border-bottom:1px solid var(--line); font-size:13px; }
    @media (max-width:800px) { main { grid-template-columns:1fr; } .row,.fields { grid-template-columns:1fr; } .fields .wide { grid-column:auto; } }
  </style>
</head>
<body>
<header><div><strong>GOV.UK VAT 注册助手 · 本地私有版</strong></div></header>
<main>
  <section class="card">
    <h1>准备注册资料</h1>
    <div class="notice local">资料文档只在本机进程中解析，不调用外部 AI 或云端 API。身份证明保存在权限为 0700 的本地临时目录，程序退出时删除。</div>

    <div>
      <h2>1. 解析 VAT 资料文档</h2>
      <input id="source-document" type="file" accept=".pdf,.docx,.xlsx,.txt,.json,.csv,.tsv">
      <div class="hint">支持 PDF、DOCX、XLSX、TXT、JSON、CSV、TSV，最大 12MB。建议使用“字段名: 值”格式。</div>
      <button id="parse-document" class="secondary" type="button">本地解析</button>
      <div id="parse-message" class="message"></div>
      <ul id="parse-warnings" class="warnings"></ul>
      <div id="extracted-fields" class="fields"></div>
      <button id="confirm-extraction" type="button" disabled>确认资料无误</button>
      <div id="confirm-message" class="message"></div>
      <div class="notice" style="margin-top:18px">固定自动化规则：标准税率营业额 £10,000；降低税率营业额 £0；零税率营业额 £0；SIC 行业代码 47910。这些值不从文档解析，也不在页面中修改。</div>
    </div>

    <form id="start-form">
      <div class="section">
        <h2>2. 验证信息</h2>
        <div class="row">
          <div><label for="email">接收邮箱验证码的邮箱</label><input id="email" type="email" autocomplete="email" required></div>
          <div><label for="phone">接收短信验证码的手机号</label><input id="phone" type="tel" autocomplete="tel" required></div>
        </div>
        <label for="password">开户密码</label>
        <input id="password" type="password" autocomplete="new-password" required>
        <div class="hint">手机号国家默认 China，“是否英国手机号”默认 No。</div>
      </div>

      <div class="section">
        <h2>3. 三份身份证明</h2>
        <input id="identity-files" type="file" multiple accept=".jpg,.jpeg,.bmp,.png,.pdf,.doc,.docx,.xls,.xlsx,.gif,.txt">
        <div class="hint">必须一次选择正好 3 份，每份最大 25MB。文件仅供后续 HMRC 页面自动上传。</div>
        <button id="save-identity" class="secondary" type="button">保存到本地临时区</button>
        <div id="identity-message" class="message"></div>
      </div>

      <details>
        <summary>高级设置</summary>
        <label for="config">配置文件</label><input id="config" value="vat-config.test.json" required>
        <label for="method">登录方式</label>
        <select id="method"><option>Create new sign in details</option><option>Government Gateway</option></select>
        <label for="user-id">Gateway User ID（仅已有账号）</label><input id="user-id" autocomplete="off">
        <label><input id="fresh" type="checkbox" checked style="width:auto"> 每次使用全新浏览器会话</label>
      </details>

      <button id="start" type="submit">开始 VAT 注册</button>
      <div id="form-error" class="message error"></div>
    </form>
  </section>

  <aside class="card">
    <h2>运行状态</h2>
    <div id="status" class="status">尚未启动</div>
    <div id="otp">
      <strong>请输入刚收到的验证码</strong>
      <input id="code" autocomplete="one-time-code" placeholder="邮箱或短信验证码">
      <button id="send-code" type="button">提交验证码</button>
      <div id="code-error" class="message error"></div>
    </div>
    <div class="row">
      <button id="pause" class="secondary" type="button" disabled>暂停自动化</button>
      <button id="continue" type="button" disabled>应用修改并继续</button>
    </div>
    <div id="pause-error" class="message error"></div>
    <div id="heading" class="meta"></div><div id="url" class="meta"></div>
    <h2 style="margin-top:24px">最近操作</h2><ul id="events" class="events"></ul>
  </aside>
</main>
<script>
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let extractedValues = {};
let extractedLabels = {};
let identityCount = 0;
let extractionConfirmed = false;

function setMessage(id, text, kind='') {
  const element = $(id); element.textContent = text; element.className = 'message ' + kind;
}

function syncExtractedValues() {
  document.querySelectorAll('[data-extracted-key]').forEach(input => {
    const value = input.value.trim();
    if (value) extractedValues[input.dataset.extractedKey] = value;
    else delete extractedValues[input.dataset.extractedKey];
  });
}

function invalidateConfirmation(message='') {
  extractionConfirmed = false;
  if (message) setMessage('confirm-message', message, 'error');
}

function renderExtractedFields() {
  const order = Object.keys(extractedLabels);
  $('extracted-fields').innerHTML = order.map(key => {
    const label = extractedLabels[key] || key;
    const value = esc(extractedValues[key] || '');
    const wide = ['business_description','premises','street'].includes(key) ? ' wide' : '';
    const control = key === 'business_description'
      ? `<textarea data-extracted-key="${key}">${value}</textarea>`
      : `<input data-extracted-key="${key}" value="${value}">`;
    return `<div class="${wide}"><label>${esc(label)}</label>${control}</div>`;
  }).join('');
  document.querySelectorAll('[data-extracted-key]').forEach(input => {
    input.addEventListener('input', () => invalidateConfirmation('资料已修改，请重新确认。'));
  });
}

$('parse-document').addEventListener('click', async () => {
  const file = $('source-document').files[0];
  if (!file) { setMessage('parse-message', '请先选择资料文档。', 'error'); return; }
  setMessage('parse-message', '正在本地解析…');
  const data = new FormData(); data.append('file', file);
  try {
    const response = await fetch('/api/parse-document', {method:'POST', body:data});
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || '解析失败');
    extractedValues = body.values || {}; extractedLabels = body.labels || {};
    invalidateConfirmation();
    renderExtractedFields();
    $('confirm-extraction').disabled = false;
    $('parse-warnings').innerHTML = (body.warnings || []).map(item => `<li>${esc(item)}</li>`).join('');
    setMessage('parse-message', `解析完成，识别到 ${Object.keys(extractedValues).length} 个字段。请检查并修正下方内容。`, 'ok');
  } catch (error) { setMessage('parse-message', error.message, 'error'); }
});

$('confirm-extraction').addEventListener('click', () => {
  syncExtractedValues();
  const required = ['project_code','application_reference','business_name','full_name','birth_date','email','phone','business_description','home_premises','home_street','home_city','home_country','premises','street','city','postcode','country'];
  const missing = required.filter(key => !extractedValues[key]);
  if (missing.length) {
    const names = missing.map(key => extractedLabels[key] || key);
    invalidateConfirmation('以下必要资料仍为空：' + names.join('、'));
    const first = document.querySelector(`[data-extracted-key="${missing[0]}"]`);
    if (first) first.focus();
    return;
  }
  extractionConfirmed = true;
  setMessage('confirm-message', '已确认当前提取资料无误。如再次修改任何字段，需要重新确认。', 'ok');
});

$('save-identity').addEventListener('click', async () => {
  const files = [...$('identity-files').files];
  if (files.length !== 3) { setMessage('identity-message', '请一次选择正好 3 份文件。', 'error'); return; }
  setMessage('identity-message', '正在保存到本地私有临时区…');
  const data = new FormData(); files.forEach(file => data.append('files', file));
  try {
    const response = await fetch('/api/identity-documents', {method:'POST', body:data});
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || '保存失败');
    identityCount = body.count;
    setMessage('identity-message', `已在本机安全保存 ${body.count} 份：${body.names.join('、')}`, 'ok');
  } catch (error) { identityCount = 0; setMessage('identity-message', error.message, 'error'); }
});

$('start-form').addEventListener('submit', async event => {
  event.preventDefault(); setMessage('form-error', ''); syncExtractedValues();
  if (!extractionConfirmed) { setMessage('form-error', '请先检查提取结果并点击“确认资料无误”。', 'error'); return; }
  if (!extractedValues.full_name) { setMessage('form-error', '资料中缺少负责人姓名，请先解析文档或在提取结果中补充。', 'error'); return; }
  if (identityCount !== 3) { setMessage('form-error', '请先保存正好三份身份证明文件。', 'error'); return; }
  const credentials = {
    HMRC_SIGN_IN_METHOD:$('method').value, HMRC_EMAIL:$('email').value,
    HMRC_USER_ID:$('user-id').value, HMRC_PASSWORD:$('password').value,
    HMRC_FULL_NAME:extractedValues.full_name, HMRC_MFA_METHOD:'Text message',
    HMRC_MFA_PHONE:$('phone').value, HMRC_MFA_PHONE_IS_UK:'No',
    HMRC_MFA_PHONE_COUNTRY:'China', HMRC_IS_TAX_AGENT:'No', HMRC_ACCESS_AS_BUSINESS:'Yes'
  };
  try {
    const response = await fetch('/api/start', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({config_path:$('config').value, fresh_session:$('fresh').checked, resume:false, credentials, extracted_values:extractedValues, extracted_confirmed:extractionConfirmed})});
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || '启动失败');
    $('password').value = '';
  } catch (error) { setMessage('form-error', error.message, 'error'); }
});

$('send-code').addEventListener('click', async () => {
  setMessage('code-error', '');
  const response = await fetch('/api/code', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({code:$('code').value})});
  const body = await response.json();
  if (!response.ok) setMessage('code-error', body.detail || '提交失败', 'error'); else $('code').value = '';
});

$('pause').addEventListener('click', async () => {
  setMessage('pause-error', '');
  const response = await fetch('/api/pause', {method:'POST'});
  const body = await response.json();
  if (!response.ok) setMessage('pause-error', body.detail || '暂停失败', 'error');
});

$('continue').addEventListener('click', async () => {
  setMessage('pause-error', ''); syncExtractedValues();
  if (!extractionConfirmed) {
    setMessage('pause-error', '修改资料后请先点击“确认资料无误”。', 'error');
    return;
  }
  const response = await fetch('/api/continue', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({extracted_values:extractedValues, extracted_confirmed:extractionConfirmed})
  });
  const body = await response.json();
  if (!response.ok) setMessage('pause-error', body.detail || '继续失败', 'error');
});

async function refresh() {
  try {
    const state = await (await fetch('/api/status')).json();
    $('status').className = 'status ' + state.status; $('status').textContent = state.message;
    $('heading').textContent = state.heading ? '页面：' + state.heading : '';
    $('url').textContent = state.url ? 'URL：' + state.url : '';
    $('otp').style.display = state.status === 'waiting_code' ? 'block' : 'none';
    $('start').disabled = ['starting','running','waiting_code','pausing','paused'].includes(state.status);
    $('pause').disabled = state.status !== 'running';
    $('continue').disabled = state.status !== 'paused';
    identityCount = state.identity_documents || identityCount;
    $('events').innerHTML = [...state.events].reverse().map(item => `<li><b>${esc(item.event)}</b>${item.heading ? ' · '+esc(item.heading) : ''}${item.text ? ' · '+esc(item.text) : ''}</li>`).join('');
  } catch (_) {}
}
setInterval(refresh,1000); refresh();
</script>
</body></html>"""


if __name__ == "__main__":
    main()
