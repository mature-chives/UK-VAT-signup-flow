from __future__ import annotations

from .countries import normalize_country

import asyncio
import json
import os
import re
import sqlite3
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from .config import Settings, is_placeholder_chain, normalize
from .authenticator import AuthenticatorFlow
from .authenticator_store import AuthenticatorError


SAFE_ACTIONS = (
    "Save and continue",
    "Continue",
    "Continue to register for VAT",
    "Start now",
    "Sign in",
)
FINAL_MARKERS = (
    "confirm and submit",
    "submit application",
    "send your application",
)
# 最终核对页的提交按钮文案：HMRC 各服务不完全一致，按顺序尝试第一个可见的。
FINAL_SUBMIT_ACTIONS = (
    "Confirm and submit",
    "Accept and submit",
    "Submit",
)


def submission_evidence(url: str, heading: str, text: str) -> tuple[str, str]:
    """只认可明确的 HMRC 回执；跳离复核页不代表注册成功。"""
    host = (urlparse(url).hostname or "").lower()
    if host not in {"www.tax.service.gov.uk", "tax.service.gov.uk"}:
        return "unverified", ""
    title = normalize(heading)
    eori_titles = {"your eori number", "you have been assigned an eori number", "your eori number is"}
    if title in eori_titles and "eori" in url.lower():
        match = re.search(r"\bGB\s?\d{12}(?:\d{3})?\b", text, re.I)
        if match:
            return "registered", re.sub(r"\s", "", match.group()).upper()
    received_titles = {
        "application received", "application submitted", "your application has been submitted",
        "we have received your application", "your application has been received",
        "registration application received", "application complete",
    }
    if title in received_titles:
        # 只在回执页且紧跟编号标签时提取；不能把 GG 号或页面中任意数字当申请号。
        match = re.search(r"(?:application reference(?: number)?|your reference number)\s*[:：]?\s*((?=[A-Z0-9-]*\d)[A-Z0-9][A-Z0-9-]{5,39})\b", text, re.I)
        return "received", match.group(1) if match else ""
    return "unverified", ""


def is_skip_edit_action(action: str) -> bool:
    """HMRC 上“我没有 UTR/NINO”一类跳过链接，不是表单提交。"""
    text = normalize(action)
    return text.startswith("i do not have") or text.startswith("skip")
AUTH_HOSTS = {
    "access.service.gov.uk",
    "www.access.service.gov.uk",
}
VAT_EMAIL_PATH = "/register-for-vat/email-address"
VAT_EMAIL_CODE_PATH = "/register-for-vat/email-address-verification"
# Chrome 自己的错误页：网络抖动、代理断开时会出现，不是 HMRC 的页面。
BROWSER_ERROR_URL_PREFIX = "chrome-error://"
# 中文标题在 normalize() 里会被清空，所以中文单独按原文比对。
BROWSER_ERROR_HEADINGS = ("无法访问此网站",)
BROWSER_ERROR_HEADINGS_NORMALIZED = (
    "cant be reached",
    # 弯撇号经 normalize() 会变成空格：This site can’t → "can t"
    "can t be reached",
    "err connection",
    "your connection is not private",
)
# 新建 Government Gateway 账号后 HMRC 会显示 "Your Government Gateway user ID is:"。
GATEWAY_USER_ID_HEADING = "government gateway user id"
# HMRC 的 Government Gateway User ID 是 12 位数字。页面里还可能出现邮箱
# （例如 1143038963@qq.com）等其它数字，所以先按"独立的 12 位"找，
# 找不到才退回 10–11 位，并且一律排除紧挨 @ 或字母数字的那串。
GATEWAY_USER_ID_PATTERN = re.compile(r"(?<![\dA-Za-z@])(\d{12})(?![\dA-Za-z@])")
GATEWAY_USER_ID_FALLBACK_PATTERN = re.compile(
    r"(?<![\dA-Za-z@])(\d{10,11})(?![\dA-Za-z@])"
)
# 有些页面把 ID 按 4 位分组显示（1234 5678 9012），比对前先去掉数字之间的分隔符。
GROUPED_DIGITS_PATTERN = re.compile(r"(?<=\d)[ \t\u00a0-](?=\d)")
# 网络抖动时退回上一页重试的次数（单次错误），以及整个任务允许的恢复总次数。
BROWSER_ERROR_RETRIES = 2
BROWSER_ERROR_RECOVERY_LIMIT = 3


@dataclass(slots=True)
class Control:
    kind: str
    name: str
    element_id: str
    label: str
    option_label: str
    value: str
    required: bool
    visible: bool
    combobox: bool


class AutomationStopped(RuntimeError):
    """流程按安全规则或因配置不完整而停止。"""


class SafetyStop(AutomationStopped):
    """安全拦截（例如测试资料不许写入真实申请），必须立即停止、不停留等待。"""


class VatAutomation:
    def __init__(
        self,
        settings: Settings,
        *,
        interactive: bool = True,
        credentials: Mapping[str, str] | None = None,
        verification_code_provider: Callable[[str], Awaitable[str]] | None = None,
        file_upload_provider: Callable[[str], Awaitable[str | None]] | None = None,
        file_uploads_remaining_provider: Callable[[], Awaitable[bool]] | None = None,
        pause_checkpoint_provider: Callable[[str, str], Awaitable[None]] | None = None,
        final_review_provider: Callable[
            [dict[str, Any]], Awaitable[dict[str, Any] | str | None]
        ] | None = None,
        remote_edit_provider: Callable[
            [dict[str, Any]], Awaitable[dict[str, Any]]
        ] | None = None,
        gateway_user_id_provider: Callable[[str], Awaitable[None] | None] | None = None,
        error_hold_provider: Callable[[dict[str, Any]], Awaitable[str]] | None = None,
        event_handler: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
        audit_context: Mapping[str, str] | None = None,
        document_values: dict[str, str] | Mapping[str, str] | None = None,
        stop_requested: Callable[[], bool] | None = None,
        enable_recording: bool = False,
        browser_ready_provider: Callable[[str, str], Awaitable[None]] | None = None,
        email_verification_prepare: Callable[[str], Awaitable[None]] | None = None,
        email_verification_provider: Callable[[str], Awaitable[str]] | None = None,
        authenticator: AuthenticatorFlow | None = None,
        vat_email_verification_prepare: Callable[[str], Awaitable[None]] | None = None,
        vat_email_verification_provider: Callable[[str], Awaitable[str]] | None = None,
    ) -> None:
        self.settings = settings
        self.interactive = interactive
        self.stop_requested = stop_requested
        self.stopping = False
        self.enable_recording = enable_recording
        self.browser_ready_provider = browser_ready_provider
        self.email_verification_prepare = email_verification_prepare
        self.email_verification_provider = email_verification_provider
        self.vat_email_verification_prepare = vat_email_verification_prepare
        self.vat_email_verification_provider = vat_email_verification_provider
        if authenticator is not None and enable_recording:
            raise ValueError("自动管理 Authenticator 不能与浏览器录制同时启用。")
        self.authenticator = authenticator
        # 显式传入凭据时不再读取进程环境：多用户 Web 场景下 os.environ 是共享的，
        # 且写进去的密码会被 Playwright 启动的 Chrome 子进程继承。
        self._credentials: Mapping[str, str] = (
            dict(credentials) if credentials is not None else os.environ
        )
        # 与凭据分开：申请人资料可进网页核对，密码不能。传入 dict 时共用同一对象，
        # 便于暂停后替换整袋而不重建 runner。
        self.document_values: dict[str, str] = (
            document_values
            if isinstance(document_values, dict)
            else dict(document_values or {})
        )
        self.verification_code_provider = verification_code_provider
        self.file_upload_provider = file_upload_provider
        self.file_uploads_remaining_provider = file_uploads_remaining_provider
        self.pause_checkpoint_provider = pause_checkpoint_provider
        self.final_review_provider = final_review_provider
        self.remote_edit_provider = remote_edit_provider
        # 新建 Government Gateway 账号后把 User ID 交给外层保存（网页/工作台）。
        self.gateway_user_id_provider = gateway_user_id_provider
        self.gateway_user_id = ""
        # 出错时（非安全拦截）先把现场交给人工：返回 "resume" 就从当前页重试，
        # 返回 "cancel"/"timeout" 就按原样停止。为 None 时保持旧的立即停止行为。
        self.error_hold_provider = error_hold_provider
        self.event_handler = event_handler
        # 多用户场景下用于在审计记录里标注操作人，不含任何凭据。
        self.audit_context = dict(audit_context or {})
        self._pending_file_path: str | None = None
        # 记录最后一个正常页面，网络抖动后从这里重试；并统计恢复次数避免死循环。
        self._last_good_url = ""
        self._browser_error_recoveries = 0
        self.settings.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.settings.profile_dir.mkdir(parents=True, exist_ok=True)
        self.audit_path = self.settings.artifacts_dir / "audit.jsonl"
        self.audit_path.touch(mode=0o600, exist_ok=True)
        self.audit_path.chmod(0o600)

    def _check_stop_requested(self) -> None:
        requested = getattr(self, "stop_requested", None)
        if requested is not None and requested():
            raise asyncio.CancelledError

    async def run(self, *, resume: bool = False) -> None:
        try:
            from playwright.async_api import Error as PlaywrightError
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError(
                "缺少 Playwright。请执行：python -m pip install -e . && "
                "python -m playwright install chromium"
            ) from exc

        try:
            self._check_stop_requested()
            async with async_playwright() as playwright:
                context = await playwright.chromium.launch_persistent_context(
                    str(self.settings.profile_dir),
                    channel=self.settings.browser_channel or None,
                    headless=self.settings.headless,
                    env={key: value for key, value in os.environ.items() if key != "DEEPSEEK_API_KEY"},
                    viewport={"width": 1440, "height": 1000},
                    args=(
                        ["--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0"]
                        if self.enable_recording else []
                    ),
                )
                page = context.pages[0] if context.pages else await context.new_page()
                try:
                    self._check_stop_requested()
                    if self.enable_recording:
                        endpoint, target_id = await self._recording_connection(context, page)
                        if self.browser_ready_provider is not None:
                            await self.browser_ready_provider(endpoint, target_id)
                        self._check_stop_requested()
                    target = self._resume_url() if resume else None
                    await page.goto(
                        target or self.settings.start_url, wait_until="domcontentloaded"
                    )
                    await self._drive(page)
                except asyncio.CancelledError:
                    self.stopping = True
                    # 只记脱敏页面位置，不截取可能带有验证码的登录现场。
                    with suppress(Exception):
                        await self._audit(
                            "run-cancelled", url=page.url, text="用户停止本次注册"
                        )
                    raise
                finally:
                    self.stopping = True
                    try:
                        with suppress(Exception):
                            await self._save_state(page.url)
                    finally:
                        # 页面卡住或保存现场失败，也要关闭本次浏览器。
                        await asyncio.wait_for(context.close(), timeout=5)
        except PlaywrightError as exc:
            raise RuntimeError(f"浏览器自动化失败：{exc}") from exc

    async def _recording_connection(self, context: Any, page: Any) -> tuple[str, str]:
        """Chrome 自动分配空闲端口；只发布本机地址及当前申请标签页 ID。"""
        port_file = self.settings.profile_dir / "DevToolsActivePort"
        deadline = asyncio.get_running_loop().time() + 5
        while asyncio.get_running_loop().time() < deadline:
            self._check_stop_requested()
            try:
                port = int(port_file.read_text(encoding="utf-8").splitlines()[0])
                if 1 <= port <= 65535:
                    break
            except (OSError, ValueError, IndexError):
                pass
            await asyncio.sleep(0.1)
        else:
            raise AutomationStopped("Chrome 未能打开本机录制连接，请重试或关闭录制选项。")
        endpoint = f"http://127.0.0.1:{port}"
        # 不使用系统代理，保证检测仅访问本机 Chrome。
        async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
            response = await client.get(f"{endpoint}/json/version")
            response.raise_for_status()
        session = await context.new_cdp_session(page)
        try:
            info = await session.send("Target.getTargetInfo")
            target_id = str(info["targetInfo"]["targetId"])
        finally:
            await session.detach()
        return endpoint, target_id

    async def _drive(self, page: Any) -> None:
        for step in range(1, self.settings.max_steps + 1):
            self._check_stop_requested()
            await page.wait_for_load_state("domcontentloaded")
            heading = await self._heading(page)
            await self._audit("page", step=step, url=page.url, heading=heading)
            if self.pause_checkpoint_provider is not None:
                await self.pause_checkpoint_provider(page.url, heading)
            try:
                self._check_stop_requested()
                result = await self._step_once(page, heading)
            except SafetyStop:
                # 安全拦截（测试资料不许写入真实申请）保持立即停止，不留存。
                raise
            except AutomationStopped as exc:
                if not await self._hold_and_retry(page, heading, exc):
                    raise
                continue
            if result is True:
                return
            if result is False:
                # 只做了跳转/点击，不需要额外等待（保持原有节奏）。
                continue
            await page.wait_for_timeout(350)
        raise AutomationStopped(f"已达到最大步骤数 {self.settings.max_steps}。")

    async def _hold_and_retry(
        self, page: Any, heading: str, exc: AutomationStopped
    ) -> bool:
        """出错时把现场交给人工；返回 True 表示人工要求从当前页重试。

        - 网页/工作台跑时注册了 error_hold_provider：停留等待，人工可"继续/取消"
        - 命令行或 error_hold_seconds=0：保持旧的立即停止行为
        - 安全拦截（SafetyStop）在调用方就被排除，不会走到这里
        """
        if self.error_hold_provider is None or self.settings.error_hold_seconds <= 0:
            return False
        screenshot = await self._snapshot(page, heading, reason="error-hold")
        await self._audit(
            "error-hold",
            url=page.url,
            heading=heading,
            message=str(exc),
            seconds=self.settings.error_hold_seconds,
        )
        decision = await self.error_hold_provider(
            {
                "url": _safe_url(page.url),
                "heading": heading,
                "message": str(exc),
                "seconds": self.settings.error_hold_seconds,
                "screenshot": str(screenshot),
            }
        )
        await self._audit(
            "error-hold-finished",
            url=page.url,
            heading=heading,
            decision=str(decision),
        )
        return str(decision) == "resume"

    async def _step_once(self, page: Any, heading: str) -> bool | None:
        """处理当前页面一次。

        返回 True：流程已完成；返回 False：已跳转，直接进入下一步；
        返回 None：正常填表并点了继续，调用方稍作等待。
        """
        if self._is_browser_error_page(page.url, heading):
            if await self._recover_from_browser_error(page, heading):
                return False
            await self._snapshot(page, heading, reason="network-error")
            raise AutomationStopped(
                "浏览器打不开 HMRC 页面（Chrome 报连接错误），已自动重试 "
                f"{BROWSER_ERROR_RETRIES} 次仍未成功：{heading or page.url}。"
                "请检查本机网络或代理后重新启动任务。"
            )
        self._last_good_url = page.url

        if self._is_remote_error_heading(heading):
            await self._snapshot(page, heading, reason="remote-service-error")
            raise AutomationStopped(
                f"远端服务返回 {heading}，请稍后重试或改用非 headless 模式。"
            )

        if self.authenticator is not None:
            try:
                if await self.authenticator.handle(
                    page, heading, self.gateway_user_id or self._credentials.get("HMRC_USER_ID", ""),
                    click=self._click_auth_action, audit=self._audit,
                    check_stop=self._check_stop_requested,
                ):
                    return False
            except AuthenticatorError as exc:
                raise AutomationStopped(str(exc)) from None

        if self._is_auth_page(page.url):
            try:
                await self._handle_auth(page, heading)
            except AutomationStopped:
                raise
            except Exception:
                # 浏览器错误可能附带密码或验证码输入值，认证阶段只报告固定文案。
                raise AutomationStopped("认证页面处理失败，请在浏览器中检查后重试。") from None
            return False

        if not self.settings.allow_live_application and self._is_application_page(
            page.url
        ):
            await self._snapshot(page, heading, reason="live-application-disabled")
            raise SafetyStop(
                "测试配置禁止向真实 HMRC VAT 申请写入虚构资料。"
            )

        verification_input = await self._verification_code_input(page, heading)
        if verification_input is not None:
            await self._enter_verification_code(page, verification_input)
            return False
        if self.vat_email_verification_provider is not None and self._is_vat_email_page(page.url, VAT_EMAIL_CODE_PATH):
            raise AutomationStopped("未找到 VAT 个人邮箱验证码输入框，已暂停供人工检查。")

        if self._is_honesty_declaration_page(page.url):
            await self._handle_honesty_declaration(page, heading)
            return False

        if self._is_final_page(page.url, heading):
            await self._handle_final_review(page, heading)
            return True

        if "/register-for-vat/manage-registrations" in page.url:
            if not await self._click_create_vat_application(page):
                await self._snapshot(
                    page, heading, reason="missing-create-vat-application"
                )
                raise AutomationStopped(
                    "申请管理页面未找到 Create a new application。"
                )
            return False

        if "/file-upload/uploading-document" in page.url:
            if not await self._wait_for_upload_processing(page):
                await self._snapshot(
                    page, heading, reason="file-upload-processing-timeout"
                )
                raise AutomationStopped(
                    "HMRC 文件上传处理超过 60 秒，已停止供检查。"
                )
            return False

        if (
            "/file-upload/summary" in page.url
            and self.file_uploads_remaining_provider is not None
        ):
            remaining = await self.file_uploads_remaining_provider()
            # HMRC 汇总页始终使用 Continue；如果还缺文件，
            # 服务端会在提交后自动返回上传页。
            action_name = "Continue"
            if not await self._click_named_action(page, action_name):
                await self._snapshot(
                    page, heading, reason="missing-file-upload-summary-action"
                )
                raise AutomationStopped(
                    f"文件上传汇总页找不到操作：{action_name}"
                )
            await self._audit(
                "file-upload-summary-action",
                action=action_name,
                files_remaining=remaining,
                url=page.url,
            )
            return False

        action = self.settings.action_for(page.url, heading)
        if action == "stop":
            await self._snapshot(page, heading, reason="configured-stop")
            raise AutomationStopped("配置要求在当前页面停止。")
        if action.startswith("skip:"):
            name = action.removeprefix("skip:")
            if not await self._click_named_action(page, name):
                await self._snapshot(page, heading, reason="missing-skip-action")
                raise AutomationStopped(f"找不到跳过操作：{name}")
            return False

        missing = await self._fill_until_stable(page, heading)
        if missing:
            await self._snapshot(
                page, heading, reason="missing-config", missing=missing
            )
            raise AutomationStopped(
                "当前页面缺少必填配置：" + "；".join(sorted(set(missing)))
            )

        if (self.vat_email_verification_prepare is not None
                and self._is_vat_email_page(page.url, VAT_EMAIL_PATH)):
            email_input = page.locator(
                'main form input[type="email"]:visible, main form input[name="email-address"]:visible'
            )
            if await email_input.count() != 1:
                raise AutomationStopped("VAT 个人邮箱输入框发生变化，已暂停供人工检查。")
            # 用实际将提交的值校验分配归属，并在点击发码前建立本环节的新游标。
            await self.vat_email_verification_prepare(await email_input.input_value())
            self._check_stop_requested()

        clicked = (
            await self._click_safe_action(page)
            if action == "continue"
            else await self._click_named_action(page, action)
        )
        if not clicked and "/application-progress" in page.url:
            clicked = await self._click_next_task(page)
        if not clicked:
            await self._snapshot(page, heading, reason="no-safe-action")
            raise AutomationStopped("找不到安全的继续按钮，已停止供人工检查。")
        self._pending_file_path = None
        return None

    @staticmethod
    def _is_browser_error_page(url: str, heading: str) -> bool:
        """Chrome 的网络错误页（连接被重置、代理断开等），不是 HMRC 的页面。"""
        if url.startswith(BROWSER_ERROR_URL_PREFIX):
            return True
        lowered = heading.casefold()
        if any(marker in lowered for marker in BROWSER_ERROR_HEADINGS):
            return True
        text = normalize(heading)
        return any(marker in text for marker in BROWSER_ERROR_HEADINGS_NORMALIZED)

    async def _recover_from_browser_error(self, page: Any, heading: str) -> bool:
        """网络抖动后退回上一个正常页面重试，成功返回 True。

        HMRC 偶发 ERR_CONNECTION_CLOSED，一次抖动不该让整个申请作废。
        """
        if not self._last_good_url:
            return False
        if self._browser_error_recoveries >= BROWSER_ERROR_RECOVERY_LIMIT:
            await self._audit(
                "browser-error-recovery-exhausted",
                url=page.url,
                heading=heading,
                recoveries=self._browser_error_recoveries,
            )
            return False
        for attempt in range(1, BROWSER_ERROR_RETRIES + 1):
            self._browser_error_recoveries += 1
            await self._audit(
                "browser-error-retry",
                attempt=attempt,
                url=self._last_good_url,
                heading=heading,
            )
            await page.wait_for_timeout(2000 * attempt)
            try:
                await page.goto(self._last_good_url, wait_until="domcontentloaded")
            except Exception:
                continue
            if not self._is_browser_error_page(page.url, await self._heading(page)):
                return True
        return False

    async def _capture_gateway_user_id(self, page: Any, heading: str) -> None:
        """新建 Government Gateway 账号后，HMRC 会显示 User ID；抓下来供复用。

        审计日志只记掩码后的值，完整值放内存交给网页/工作台保存。
        """
        if GATEWAY_USER_ID_HEADING not in normalize(heading):
            # 页面标题可能改文案，注册确认页的 URL 也认。
            if "/registration/confirmation/" not in page.url:
                return
        try:
            text = await page.locator("body").inner_text()
        except Exception:
            return
        haystack = GROUPED_DIGITS_PATTERN.sub("", text or "")
        match = GATEWAY_USER_ID_PATTERN.search(haystack)
        matched = "12-digit"
        if match is None:
            match = GATEWAY_USER_ID_FALLBACK_PATTERN.search(haystack)
            matched = "fallback"
        if match is None:
            # 抓不到就留证据：HMRC 有时只在页面上提示"已发邮件"，
            # User ID 只出现在邮箱里，这时需要人工抄一下。
            await self._audit("gateway-user-id-not-found", url=page.url)
            await self._dump_gateway_user_id_page(text or "")
            return
        self.gateway_user_id = match.group(0)
        await self._audit(
            "gateway-user-id-captured",
            user_id=_masked_value(self.gateway_user_id),
            digits=len(self.gateway_user_id),
            matched=matched,
        )
        if self.gateway_user_id_provider is not None:
            result = self.gateway_user_id_provider(self.gateway_user_id)
            if result is not None:
                await result

    async def _dump_gateway_user_id_page(self, text: str) -> None:
        """把建号确认页文本存到 artifacts（0600，本机私有），便于事后适配抓取规则。"""
        if not text.strip():
            return
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        target = self.settings.artifacts_dir / f"{stamp}-gateway-user-id-page.txt"
        try:
            target.write_text(text, encoding="utf-8")
            target.chmod(0o600)
        except OSError:
            return

    async def _handle_auth(self, page: Any, heading: str) -> None:
        """自动处理凭据和安全方式，仅验证码由用户即时输入。"""
        await self._capture_gateway_user_id(page, heading)
        password = page.locator('input[type="password"]:visible')
        if await password.count():
            user_password = self._credentials.get("HMRC_PASSWORD", "")
            is_password_creation = (
                await password.count() > 1
                or "create a password" in normalize(heading)
                or "/registration/password" in page.url
            )
            if is_password_creation:
                if not user_password:
                    raise AutomationStopped("缺少环境变量 HMRC_PASSWORD。")
                for field in await password.all():
                    await field.fill(user_password)
                await self._audit("auth-password-created", url=page.url)
                if not await self._click_auth_action(page, ("Continue",)):
                    raise AutomationStopped("创建密码页面未找到继续按钮。")
                return

            user_id = self._credentials.get("HMRC_USER_ID", "")
            if not user_id or not user_password:
                raise AutomationStopped(
                    "缺少登录环境变量 HMRC_USER_ID 或 HMRC_PASSWORD。"
                )
            user_input = page.locator(
                'input[type="text"]:visible, input[type="email"]:visible'
            ).first
            if not await user_input.count():
                raise AutomationStopped("登录页未找到 Government Gateway User ID 输入框。")
            await user_input.fill(user_id)
            await password.first.fill(user_password)
            await self._audit("auth-credentials-filled", url=page.url)
            if not await self._click_auth_action(page, ("Sign in", "Continue")):
                raise AutomationStopped("登录页未找到安全的登录按钮。")
            return

        email_input = page.locator(
            'main form input[type="email"]:visible, '
            'main form input[autocomplete="email"]:visible, '
            'main form input[name*="email" i]:visible'
        ).first
        if await email_input.count():
            email = self._credentials.get("HMRC_EMAIL", "")
            if not email:
                raise AutomationStopped("创建登录凭据需要环境变量 HMRC_EMAIL。")
            await email_input.fill(email)
            await self._audit("auth-email-filled", url=page.url)
            # 必须在触发发码之前记游标；只接已知 GG 建号邮箱页，不推断其他验证。
            if (urlparse(page.url).path.rstrip("/") == "/registration/email"
                    and self.email_verification_prepare is not None):
                await self.email_verification_prepare(email)
                self._check_stop_requested()
            if not await self._click_auth_action(page, ("Continue",)):
                raise AutomationStopped("邮箱页面未找到继续按钮。")
            return

        if "/registration/name" in page.url or "full name" in normalize(heading):
            found, configured_name = self.settings.answer_for(
                "Full name",
                page.url,
                heading,
                document_values=self.document_values,
            )
            if found:
                try:
                    configured_name = self._resolve_value(configured_name)
                except KeyError:
                    configured_name = ""
            full_name = self._credentials.get(
                "HMRC_FULL_NAME", str(configured_name) if found and configured_name else ""
            )
            if not full_name:
                raise AutomationStopped(
                    "创建登录凭据需要 HMRC_FULL_NAME 或配置中的 Full name。"
                )
            name_input = page.locator('main form input[name="name"]:visible').first
            if not await name_input.count():
                name_input = page.locator('main form input[type="text"]:visible').first
            await name_input.fill(full_name)
            await self._audit("auth-name-filled", url=page.url)
            if not await self._click_auth_action(page, ("Continue",)):
                raise AutomationStopped("姓名页面未找到继续按钮。")
            return

        if (
            "/multi-factor/enter-mobile-country/" in page.url
            or "country for this mobile phone number" in normalize(heading)
        ):
            country = normalize_country(self._credentials.get("HMRC_MFA_PHONE_COUNTRY", ""))
            if not country:
                raise AutomationStopped("非英国手机号需要明确填写 HMRC_MFA_PHONE_COUNTRY。")
            country_input = page.locator(
                'main form input[type="text"]:visible, '
                'main form input:not([type]):visible'
            ).first
            if not await country_input.count():
                raise AutomationStopped("手机号国家页面未找到国家输入框。")
            await country_input.fill(country)
            await self._choose_autocomplete_option(page, country)
            await self._audit("mfa-phone-country-filled", url=page.url)
            if not await self._click_auth_action(page, ("Continue",)):
                raise AutomationStopped("手机号国家页面未找到继续按钮。")
            return

        code_input = page.locator(
            'input[inputmode="numeric"]:visible, '
            'input[autocomplete="one-time-code"]:visible, '
            'input[name*="code" i]:visible, input[id*="code" i]:visible, '
            'input[name="oneTimePassword"]:visible, '
            'input[id="oneTimePassword"]:visible'
        ).first
        heading_normalized = normalize(heading)
        if await code_input.count():
            if (not self.interactive and self.verification_code_provider is None
                    and self.email_verification_provider is None):
                raise AutomationStopped("验证码页面需要用户输入验证码。")
            await self._enter_verification_code(page, code_input)
            return

        telephone = page.locator('main form input[type="tel"]:visible').first
        if await telephone.count():
            phone = self._credentials.get("HMRC_MFA_PHONE", "")
            if not phone:
                raise AutomationStopped("安全设置页面需要环境变量 HMRC_MFA_PHONE。")
            await telephone.fill(phone)
            await self._audit("mfa-phone-filled", url=page.url)
            if not await self._click_auth_action(
                page, ("Send access code", "Continue")
            ):
                raise AutomationStopped("安全设置页面未找到继续按钮。")
            return

        radios = page.locator('main form input[type="radio"]:visible')
        if await radios.count():
            available = await page.locator(
                'main form input[type="radio"] + label'
            ).all_inner_texts()
            is_sign_in_choice = any(
                "government gateway" in normalize(label) for label in available
            )
            if is_sign_in_choice:
                method = self._sign_in_method()
                audit_event = "auth-method-selected"
            elif "tax agent" in heading_normalized:
                method = self._credentials.get("HMRC_IS_TAX_AGENT", "No")
                audit_event = "auth-tax-agent-selected"
            elif "business or organisation" in heading_normalized:
                method = self._credentials.get("HMRC_ACCESS_AS_BUSINESS", "Yes")
                audit_event = "auth-organisation-selected"
            elif (
                "/multi-factor/mobile-number-uk/" in page.url
                or "adding a uk mobile number" in heading_normalized
            ):
                country = normalize_country(self._credentials.get("HMRC_MFA_PHONE_COUNTRY", ""))
                method = "Yes" if country == "United Kingdom" else "No" if country else self._credentials.get("HMRC_MFA_PHONE_IS_UK", "")
                if method not in {"Yes", "No"}:
                    raise AutomationStopped("请明确确认短信验证手机号国家，不能自动判断是否为英国号码。")
                audit_event = "mfa-phone-country-selected"
            else:
                method = self._credentials.get("HMRC_MFA_METHOD", "Text message")
                audit_event = "mfa-method-selected"
            option = page.get_by_label(method, exact=False)
            if not await option.count():
                raise AutomationStopped(
                    f"找不到认证方式 {method!r}，页面选项：{available}"
                )
            await option.first.check()
            await self._audit(audit_event, method=method, url=page.url)
            if not await self._click_auth_action(page, ("Continue",)):
                raise AutomationStopped("认证方式页面未找到继续按钮。")
            return

        validation_error = page.locator(
            '.govuk-error-summary:visible, .govuk-error-message:visible'
        )
        if await validation_error.count():
            await self._snapshot(page, heading, reason="auth-validation-error")
            raise AutomationStopped("认证页面存在校验错误，已停止供检查。")

        if await self._click_auth_action(page, ("Continue",)):
            await self._audit("auth-continue", url=page.url)
            return

        await self._snapshot(page, heading, reason="unknown-auth-page")
        raise AutomationStopped("无法识别当前认证页面，已保存截图供检查。")

    async def _verification_code_input(self, page: Any, heading: str) -> Any | None:
        if "code" not in normalize(heading) and not self._is_vat_email_page(page.url, VAT_EMAIL_CODE_PATH):
            return None
        locator = page.locator(
            'main form input[autocomplete="one-time-code"]:visible, '
            'main form input[name*="code" i]:visible, '
            'main form input[id*="code" i]:visible'
        ).first
        return locator if await locator.count() else None

    async def _enter_verification_code(self, page: Any, code_input: Any) -> None:
        heading = await self._heading(page)
        if (self.email_verification_provider is not None
                and self._is_auth_page(page.url)
                and urlparse(page.url).path.rstrip("/") == "/registration/code"):
            code = await self.email_verification_provider(heading)
        elif (self.vat_email_verification_provider is not None
                and self._is_vat_email_page(page.url, VAT_EMAIL_CODE_PATH)):
            code = await self.vat_email_verification_provider(heading)
        elif self.verification_code_provider is not None:
            code = await self.verification_code_provider(heading)
        elif self.interactive:
            code = await asyncio.to_thread(
                input, "请输入刚收到的验证码（输入 q 退出）："
            )
        else:
            raise AutomationStopped("验证码页面需要用户输入验证码。")
        if code.strip().casefold() == "q":
            raise AutomationStopped("用户在验证码阶段退出。")
        if not code.strip():
            raise AutomationStopped("验证码为空。")
        self._check_stop_requested()
        try:
            await code_input.fill(code.strip())
            await self._audit("verification-code-entered", url=page.url)
            if not await self._click_auth_action(
                page, ("Save and continue", "Continue", "Submit", "Confirm", "Sign in")
            ):
                raise AutomationStopped("验证码页面未找到继续按钮。")
        except AutomationStopped:
            raise
        except Exception:
            # VAT 验证页不在 GG 域名上，同样不能让浏览器错误附带输入值进入日志。
            raise AutomationStopped("验证码填写或提交失败，请在浏览器中检查后重试。") from None

    async def _click_auth_action(self, page: Any, names: tuple[str, ...]) -> bool:
        for name in names:
            button = page.get_by_role("button", name=name, exact=True)
            if await button.count() and await button.first.is_visible():
                self._check_stop_requested()
                await button.first.click()
                return True
        return False

    @staticmethod
    async def _wait_for_upload_processing(page: Any, attempts: int = 120) -> bool:
        """上传中间页没有按钮，应等待 HMRC 完成扫描并自动跳转。"""
        for _ in range(attempts):
            await page.wait_for_timeout(500)
            if "/file-upload/uploading-document" not in page.url:
                return True
        return False

    async def _controls(self, page: Any) -> list[Control]:
        raw = await page.locator("main form input, main form select, main form textarea").evaluate_all(
            r"""
            elements => elements.map(el => {
              const type = (el.type || el.tagName).toLowerCase();
              if (["hidden", "submit", "button", "reset"].includes(type)) return null;
              const ownLabel = el.labels && el.labels.length
                ? el.labels[0].innerText.trim() : "";
              const legend = el.closest("fieldset")?.querySelector("legend")?.innerText.trim() || "";
              return {
                kind: type,
                name: el.name || "",
                element_id: el.id || "",
                label: type === "radio" ? legend : (ownLabel || legend || el.getAttribute("aria-label") || el.name || el.id),
                option_label: type === "radio" ? ownLabel : "",
                value: el.value || "",
                required: !!el.required
                  || el.getAttribute("aria-required") === "true"
                  || type === "radio"
                  || type === "file"
                  || (!["checkbox"].includes(type) && !/optional/i.test(ownLabel || legend)),
                visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
                ,combobox: el.getAttribute("role") === "combobox" || el.hasAttribute("aria-autocomplete")
              };
            }).filter(Boolean)
            """
        )
        return [Control(**item) for item in raw if item.get("visible")]

    async def _fill_until_stable(self, page: Any, heading: str) -> list[str]:
        previous_signature: tuple[tuple[str, str, str], ...] = ()
        missing: list[str] = []
        for _ in range(4):
            controls = await self._controls(page)
            signature = tuple(
                (control.kind, control.name, control.element_id) for control in controls
            )
            missing = await self._fill(page, controls, heading)
            await page.wait_for_timeout(150)
            refreshed = await self._controls(page)
            refreshed_signature = tuple(
                (control.kind, control.name, control.element_id)
                for control in refreshed
            )
            if refreshed_signature == signature or refreshed_signature == previous_signature:
                return missing
            previous_signature = signature
        return missing

    async def _fill(
        self, page: Any, controls: list[Control], heading: str
    ) -> list[str]:
        missing: list[str] = []
        completed_radio_groups: set[str] = set()
        for control in controls:
            key = control.name or control.element_id or control.label
            if control.kind == "radio" and key in completed_radio_groups:
                continue
            if control.kind == "file" and self.file_upload_provider is not None:
                if self._pending_file_path is None:
                    self._pending_file_path = await self.file_upload_provider(
                        control.label
                    )
                found = self._pending_file_path is not None
                value = self._pending_file_path
            else:
                found, value = self.settings.answer_for(
                    control.label,
                    page.url,
                    heading,
                    aliases=(control.name, control.element_id),
                    document_values=self.document_values,
                )
            if not found:
                if control.required:
                    missing.append(control.label or key)
                continue

            locator = self._locator(page, control)
            try:
                value = self._resolve_value(value)
            except KeyError as exc:
                token = str(exc.args[0])
                if token.startswith("doc:"):
                    missing.append(f"{control.label or key}（缺少资料：{token}）")
                else:
                    missing.append(
                        f"{control.label or key}（缺少环境变量 {token}）"
                    )
                continue
            if control.kind == "radio":
                group = [item for item in controls if item.name == control.name]
                chosen = next(
                    (
                        item
                        for item in group
                        if self._radio_option_matches(item, value)
                    ),
                    None,
                )
                if chosen is None:
                    missing.append(
                        f"{control.label}（选项不存在：{value}）"
                    )
                else:
                    await self._locator(page, chosen).check()
                    completed_radio_groups.add(key)
                continue
            if control.kind == "checkbox":
                wanted = bool(value)
                if wanted:
                    await locator.check()
                else:
                    await locator.uncheck()
            elif control.kind == "select-one":
                try:
                    await locator.select_option(label=str(value))
                except Exception:
                    await locator.select_option(value=str(value))
            elif control.kind == "file":
                file_path = Path(str(value)).expanduser().resolve()
                if not file_path.is_file():
                    missing.append(f"{control.label}（文件不存在：{file_path}）")
                else:
                    await locator.set_input_files(str(file_path))
            else:
                await locator.fill(str(value))
                if control.combobox:
                    await self._choose_autocomplete_option(page, str(value))
        return missing

    def _sign_in_method(self) -> str:
        """登录方式：网页/环境变量优先，其次流程配置的默认值，最后新建账号。"""
        return (
            self._credentials.get("HMRC_SIGN_IN_METHOD", "")
            or self.settings.default_sign_in_method
            or "Create new sign in details"
        )

    def _resolve_value(self, value: Any) -> Any:
        if isinstance(value, str) and is_placeholder_chain(value):
            last_error: KeyError | None = None
            for part in value.split("|"):
                try:
                    resolved = self._resolve_placeholder_token(part)
                except KeyError as exc:
                    last_error = exc
                    continue
                if resolved is not None and str(resolved).strip() != "":
                    return resolved
            raise last_error if last_error is not None else KeyError(value)
        return value

    def _resolve_placeholder_token(self, value: str) -> Any:
        if value.startswith("env:"):
            variable = value.removeprefix("env:")
            resolved = self._credentials.get(variable)
            if not resolved:
                raise KeyError(variable)
            return resolved
        if value.startswith("doc:"):
            key = value.removeprefix("doc:")
            resolved = self.document_values.get(key, "")
            if not str(resolved).strip():
                raise KeyError(value)
            return resolved
        return value

    @staticmethod
    def _radio_option_matches(control: Control, value: Any) -> bool:
        wanted = normalize(str(value))
        return wanted in {
            normalize(control.option_label),
            normalize(control.value),
        }

    @staticmethod
    def _is_remote_error_heading(heading: str) -> bool:
        return re.match(r"^[45]\d\d\b", heading.strip()) is not None

    @staticmethod
    async def _choose_autocomplete_option(page: Any, value: str) -> None:
        await page.wait_for_timeout(250)
        options = page.get_by_role("option")
        if not await options.count():
            raise AutomationStopped("未出现可确认的下拉候选项，请核对页面后再继续。")
        matches = []
        for option in await options.all():
            if await option.is_visible() and normalize_country(await option.inner_text()) == normalize_country(value):
                matches.append(option)
        if len(matches) != 1:
            raise AutomationStopped("下拉候选项无法唯一匹配填写值，请核对国家/地区或页面选项。")
        await matches[0].click()

    @staticmethod
    def _locator(page: Any, control: Control) -> Any:
        if control.element_id:
            # HMRC 部分动态表单使用纯数字 id（例如 "55"）。`#55` 不是合法
            # CSS selector；属性选择器既支持数字 id，也能安全处理特殊字符。
            return page.locator(f"[id={json.dumps(control.element_id)}]")
        if control.name:
            return page.locator(
                f'[name="{control.name.replace(chr(34), chr(92) + chr(34))}"]'
            ).first
        return page.get_by_label(control.option_label or control.label, exact=True)

    async def _click_safe_action(self, page: Any) -> bool:
        for name in SAFE_ACTIONS:
            for role in ("button", "link"):
                locator = page.get_by_role(role, name=name, exact=True)
                if await locator.count() and await locator.first.is_visible():
                    await self._audit("click", text=name, url=page.url)
                    self._check_stop_requested()
                    await locator.first.click()
                    return True
        return False

    async def _click_named_action(self, page: Any, name: str) -> bool:
        for role in ("button", "link"):
            locator = page.get_by_role(role, name=name, exact=True)
            if await locator.count() and await locator.first.is_visible():
                await self._audit("click", text=name, url=page.url)
                self._check_stop_requested()
                await locator.first.click()
                return True
        # HMRC 文案会混用直撇号和弯撇号，例如 company's / company’s。
        # 备用匹配只接受归一化后的完整文本相等，避免模糊匹配误点其他链接。
        wanted = normalize(name)
        for role in ("button", "link"):
            for locator in await page.get_by_role(role).all():
                if not await locator.is_visible():
                    continue
                text = (await locator.inner_text()).strip()
                if normalize(text) == wanted:
                    await self._audit("click", text=text, url=page.url)
                    self._check_stop_requested()
                    await locator.click()
                    return True
        return False

    async def _click_next_task(self, page: Any) -> bool:
        href = await page.locator("main").evaluate(
            """
            main => {
              const rows = [...main.querySelectorAll('li, tr')];
              for (const row of rows) {
                const status = (row.innerText || '').toLowerCase();
                const link = row.querySelector('a[href]');
                if (link && !status.includes('completed') && !status.includes('cannot start yet')) {
                  return link.getAttribute('href');
                }
              }
              return null;
            }
            """
        )
        if not href:
            return False
        await self._audit("next-task", href=_safe_url(str(href)), url=page.url)
        await page.locator(f'main a[href="{href}"]').first.click()
        return True

    async def _click_create_vat_application(self, page: Any) -> bool:
        create = page.get_by_role(
            "link", name="Create a new application", exact=True
        )
        if not await create.count() or not await create.first.is_visible():
            return False
        await self._audit("create-vat-application", url=page.url)
        await create.first.click()
        return True

    async def _handle_honesty_declaration(self, page: Any, heading: str) -> None:
        warnings = self.settings.live_application_warnings(
            self._credentials, document_values=self.document_values
        )
        if warnings:
            await self._snapshot(
                page,
                heading,
                reason="synthetic-live-data",
                missing=warnings,
            )
            raise SafetyStop(
                "当前配置仍包含测试资料，不能自动点击 Accept and continue："
                + "；".join(warnings)
            )
        clicked = await self._click_named_action(page, "Accept and continue")
        if not clicked:
            for role in ("button", "link"):
                locator = page.get_by_role(
                    role,
                    name=re.compile(r"^\s*Accept and continue\s*$", re.IGNORECASE),
                )
                if await locator.count() and await locator.first.is_visible():
                    await self._audit("click", text="Accept and continue", url=page.url)
                    await locator.first.click()
                    clicked = True
                    break
        if not clicked:
            await self._snapshot(
                page,
                heading,
                reason="missing-accept-and-continue",
            )
            raise AutomationStopped(
                "诚信声明页面未找到 Accept and continue 按钮。"
            )
        await self._audit("honesty-declaration-accepted", url=page.url)

    async def _handle_final_review(self, page: Any, heading: str) -> None:
        """存档最终复核页，等待人工确认后提交真实申请。"""
        if self.final_review_provider is None:
            raise AutomationStopped(
                "已到达最终复核/声明页面。安全策略禁止自动提交。"
            )

        while True:
            await self._expand_final_review_sections(page, heading)
            screenshot = await self._snapshot(page, heading, reason="final-review")
            document = await self._save_page_pdf(page, "final-review")
            changes = await self._final_review_change_items(page)
            await self._audit(
                "final-review-reached", url=page.url, pdf_saved=document is not None
            )
            decision = await self.final_review_provider(
                {
                    "url": _safe_url(page.url),
                    "heading": heading,
                    "pdf": str(document) if document else "",
                    "screenshot": str(screenshot) if screenshot else "",
                    "editable": bool(changes),
                    "changes": changes,
                }
            )
            action = decision.get("action", "") if isinstance(decision, dict) else decision
            if action in {None, "submit"}:
                break
            if action != "edit":
                raise AutomationStopped("最终复核返回了无效操作，已停止以避免误提交。")
            if self.remote_edit_provider is None:
                raise AutomationStopped(
                    "当前运行方式不支持网页远程修改 HMRC 信息。"
                )
            target = str(decision.get("target", "")) if isinstance(decision, dict) else ""
            if not target:
                raise AutomationStopped("未指定需要修改的最终核对项目。")
            await self._audit("final-review-edit-requested", url=page.url)
            heading = await self._handle_remote_final_review_edit(
                page, target, changes
            )

        self._check_stop_requested()
        await self._audit("final-review-confirmed", url=page.url)
        action = ""
        for candidate in FINAL_SUBMIT_ACTIONS:
            if await self._click_named_action(page, candidate):
                action = candidate
                break
        if not action:
            await self._snapshot(
                page, heading, reason="missing-confirm-and-submit"
            )
            raise AutomationStopped(
                "人工已确认，但最终复核页找不到提交按钮："
                + "、".join(FINAL_SUBMIT_ACTIONS)
            )
        await self._verify_final_submission(page, heading, action)

    async def _final_review_change_items(self, page: Any) -> list[dict[str, str]]:
        """提取最终核对页的 section/字段/当前值/Change 链接，不向客户端暴露 URL。"""
        raw = await page.locator("main a").evaluate_all(
            r"""
            links => {
              let changeIndex = 0;
              return links.map(link => {
                const visible = !!(link.offsetWidth || link.offsetHeight || link.getClientRects().length);
                const text = (link.innerText || '').replace(/\s+/g, ' ').trim();
                if (!visible || !/^change\b/i.test(text)) return null;
                const row = link.closest('.govuk-summary-list__row, tr, li');
                const key = row?.querySelector('.govuk-summary-list__key, th, dt');
                const valueEl = row?.querySelector(
                  '.govuk-summary-list__value, td, dd'
                );
                const section = row?.closest('.govuk-accordion__section');
                const heading = section?.querySelector(
                  '.govuk-accordion__section-button, .govuk-accordion__section-heading'
                );
                const fieldLabel = (key?.innerText || text.replace(/^change\s*/i, ''))
                  .replace(/\s+/g, ' ').trim();
                // 地址等答案按行保留换行，交给前端逐行展示。
                const value = (valueEl?.innerText || '')
                  .split('\n')
                  .map(part => part.replace(/\s+/g, ' ').trim())
                  .filter(Boolean)
                  .join('\n');
                let sectionLabel = (heading?.innerText || '').replace(/\s+/g, ' ').trim();
                sectionLabel = sectionLabel
                  .replace(/\b(show|hide)(\s+this section)?\b/ig, '')
                  .replace(/\s+,/g, ',')
                  .replace(/\s+/g, ' ')
                  .trim()
                  .replace(/^,|,$/g, '')
                  .trim();
                const label = sectionLabel && !fieldLabel.startsWith(sectionLabel)
                  ? `${sectionLabel} — ${fieldLabel}` : fieldLabel;
                return {
                  id: `change-${changeIndex++}`,
                  label: label || text,
                  section: sectionLabel,
                  field: fieldLabel,
                  value: value,
                };
              }).filter(Boolean);
            }
            """
        )
        return [
            {
                "id": str(item.get("id", "")),
                "label": str(item.get("label", "")),
                "section": str(item.get("section", "")),
                "field": str(item.get("field", "")),
                "value": str(item.get("value", "")),
            }
            for item in raw
            if item.get("id") and item.get("label")
        ]

    async def _final_review_change_link(self, page: Any, index: int) -> Any | None:
        """按 Change 链接自己的序号定位，不用 main 里全部 a 的下标。"""
        seen = 0
        for link in await page.locator("main a").all():
            if not await link.is_visible():
                continue
            if not normalize(await link.inner_text()).startswith("change"):
                continue
            if seen == index:
                return link
            seen += 1
        return None

    async def _handle_remote_final_review_edit(
        self, page: Any, target: str, changes: list[dict[str, str]]
    ) -> str:
        if target not in {item["id"] for item in changes}:
            raise AutomationStopped("选择的最终核对修改项目已经失效，请重新选择。")
        match = re.fullmatch(r"change-(\d+)", target)
        if match is None:
            raise AutomationStopped("最终核对修改项目格式无效。")
        link = await self._final_review_change_link(page, int(match.group(1)))
        if link is None:
            raise AutomationStopped("最终核对页上的 Change 链接已不可用。")
        return_url = page.url
        await link.click()
        await page.wait_for_timeout(350)

        submitted = False
        for _ in range(40):
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=15_000)
            except Exception:
                pass
            heading = await self._heading(page)
            if self._is_final_page(page.url, heading):
                await self._audit(
                    "final-review-edit-completed", url=page.url, heading=heading
                )
                return heading

            verification_input = await self._verification_code_input(page, heading)
            if verification_input is not None:
                await self._enter_verification_code(page, verification_input)
                continue

            errors = await self._validation_errors(page)
            if submitted and not errors:
                if await self._return_to_final_review_from_edit(page, return_url):
                    heading = await self._heading(page)
                    if self._is_final_page(page.url, heading):
                        await self._audit(
                            "final-review-edit-completed",
                            url=page.url,
                            heading=heading,
                        )
                        return heading
                raise AutomationStopped(
                    "修改已保存，但未能回到最终核对页。"
                )

            form = await self._remote_edit_form(page)
            response = await self.remote_edit_provider(
                {
                    "url": _safe_url(page.url),
                    "heading": heading,
                    "fields": form["fields"],
                    "actions": form["actions"],
                    "errors": errors,
                }
            )
            if not isinstance(response, dict):
                raise AutomationStopped("远程修改未返回有效的网页操作。")
            action = str(response.get("action", "")).strip()
            if not action and form["actions"]:
                action = form["actions"][0]
            if action not in form["actions"]:
                raise AutomationStopped("远程修改提交按钮无效，请重新进入修改。")
            if any(normalize(marker) in normalize(action) for marker in FINAL_MARKERS):
                raise AutomationStopped("远程修改阶段禁止触发最终提交按钮。")
            if not is_skip_edit_action(action):
                await self._apply_remote_edit_answers(
                    page, form["fields"], response.get("answers", {})
                )
            if not await self._click_named_action(page, action):
                raise AutomationStopped(f"远程修改页面找不到操作：{action}")
            submitted = True
            await page.wait_for_timeout(350)

        raise AutomationStopped("远程修改经过 40 个页面仍未返回最终核对页。")

    @staticmethod
    def _application_progress_url(current_url: str) -> str:
        parsed = urlparse(current_url)
        return f"{parsed.scheme}://{parsed.netloc}/register-for-vat/application-progress"

    @staticmethod
    def _is_final_review_task_link(href: str, text: str, row_text: str = "") -> bool:
        if re.search(r"cannot start yet", row_text, re.I):
            return False
        path = href.casefold()
        label = normalize(text)
        return (
            "/register-for-vat/check-your-answers" in path
            or "/register-for-vat/check-confirm-answers" in path
            or label.startswith("check your answers")
            or "check and confirm" in label
        )

    async def _click_final_review_task(self, page: Any) -> bool:
        """只点最终核对任务，不点进度页上其它未完成项。"""
        href = await page.locator("main").evaluate(
            """
            main => {
              const links = [...main.querySelectorAll('a[href]')];
              for (const link of links) {
                const href = link.getAttribute('href') || '';
                const text = (link.innerText || '').replace(/\\s+/g, ' ').trim();
                const row = (link.closest('li, tr')?.innerText || '');
                if (/cannot start yet/i.test(row)) continue;
                const path = href.toLowerCase();
                const label = text.toLowerCase();
                if (
                  path.includes('/register-for-vat/check-your-answers')
                  || path.includes('/register-for-vat/check-confirm-answers')
                  || /^check your answers\\b/.test(label)
                  || label.includes('check and confirm')
                ) {
                  return href;
                }
              }
              return null;
            }
            """
        )
        if not href:
            return False
        await self._audit("final-review-task", href=_safe_url(str(href)), url=page.url)
        await page.locator(f'main a[href="{href}"]').first.click()
        return True

    async def _return_to_final_review_from_edit(
        self, page: Any, return_url: str
    ) -> bool:
        """改完当前 Change 项后经进度页回到最终核对，不继续走整份申请。"""
        heading = await self._heading(page)
        if self._is_final_page(page.url, heading):
            return True
        if "/application-progress" not in urlparse(page.url).path.casefold():
            progress = self._application_progress_url(page.url)
            await self._audit("return-to-progress-after-edit", url=page.url)
            await page.goto(progress, wait_until="domcontentloaded")
            await page.wait_for_timeout(350)
        if await self._click_final_review_task(page):
            await page.wait_for_timeout(350)
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=15_000)
            except Exception:
                pass
            heading = await self._heading(page)
            if self._is_final_page(page.url, heading):
                return True
        if return_url:
            await self._audit("return-to-saved-final-review", url=return_url)
            await page.goto(return_url, wait_until="domcontentloaded")
            await page.wait_for_timeout(350)
            heading = await self._heading(page)
            return self._is_final_page(page.url, heading)
        return False

    async def _remote_edit_form(self, page: Any) -> dict[str, Any]:
        """把当前 HMRC 表单转换为可在 Web UI 中安全渲染的字段描述。"""
        return await page.locator("main").evaluate(
            r"""
            main => {
              const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
              const ownLabel = el => el.labels?.[0]?.innerText?.trim() || '';
              const legend = el => el.closest('fieldset')?.querySelector('legend')?.innerText?.trim() || '';
              const option = (el, index) => ({
                value: el.value || '',
                label: ownLabel(el) || el.value || `Option ${index + 1}`,
                element_id: el.id || '',
                index
              });
              const elements = [...main.querySelectorAll('form input, form select, form textarea')]
                .filter(visible);
              const seen = new Set();
              const fields = [];
              for (const el of elements) {
                const kind = (el.type || el.tagName).toLowerCase();
                if (['hidden','submit','button','reset','file'].includes(kind)) continue;
                const key = el.name || el.id;
                if (!key) continue;
                const group = (kind === 'radio' || kind === 'checkbox') && el.name
                  ? elements.filter(item => item.name === el.name && (item.type || '').toLowerCase() === kind)
                  : [el];
                const signature = `${kind}:${key}`;
                if (seen.has(signature)) continue;
                seen.add(signature);
                const base = {
                  key,
                  name: el.name || '',
                  element_id: el.id || '',
                  label: legend(el) || ownLabel(el) || el.getAttribute('aria-label') || key,
                  required: !!el.required || el.getAttribute('aria-required') === 'true'
                    || kind === 'radio'
                    || (kind !== 'checkbox' && !/optional/i.test(legend(el) || ownLabel(el))),
                  combobox: el.getAttribute('role') === 'combobox' || el.hasAttribute('aria-autocomplete')
                };
                if (kind === 'radio') {
                  fields.push({...base, kind: 'radio', value: group.find(item => item.checked)?.value || '', options: group.map(option)});
                } else if (kind === 'checkbox' && group.length > 1) {
                  fields.push({...base, kind: 'checkbox-group', value: group.filter(item => item.checked).map(item => item.value), options: group.map(option)});
                } else if (kind === 'checkbox') {
                  fields.push({...base, kind: 'checkbox', value: !!el.checked, options: []});
                } else if (kind === 'select-one') {
                  fields.push({...base, kind, value: el.value || '', options: [...el.options].filter(item => !item.disabled).map((item, index) => ({value:item.value, label:item.textContent.trim(), element_id:'', index}))});
                } else {
                  fields.push({...base, kind, value: el.value || '', options: []});
                }
              }
              const buttonActions = [...main.querySelectorAll('button, input[type="submit"], a.govuk-button')]
                .filter(visible)
                .map(el => (el.innerText || el.value || '').replace(/\s+/g, ' ').trim());
              const skipActions = [...main.querySelectorAll('a')]
                .filter(visible)
                .map(el => (el.innerText || '').replace(/\s+/g, ' ').trim())
                .filter(text => /^i do not have\b/i.test(text) || /^skip\b/i.test(text));
              const actions = [...skipActions, ...buttonActions]
                .filter((value, index, all) => value && all.indexOf(value) === index);
              return {fields, actions};
            }
            """
        )

    async def _validation_errors(self, page: Any) -> list[str]:
        errors = await page.locator(
            ".govuk-error-summary a, .govuk-error-message"
        ).all_inner_texts()
        return list(dict.fromkeys(text.strip() for text in errors if text.strip()))

    async def _apply_remote_edit_answers(
        self,
        page: Any,
        fields: list[dict[str, Any]],
        answers: Any,
    ) -> None:
        if not isinstance(answers, dict):
            raise AutomationStopped("远程修改答案格式无效。")
        field_map = {str(field.get("key", "")): field for field in fields}
        unknown = set(map(str, answers)) - set(field_map)
        if unknown:
            raise AutomationStopped("远程修改包含当前页面不存在的字段。")

        for key, raw_value in answers.items():
            field = field_map[str(key)]
            kind = str(field.get("kind", ""))
            options = list(field.get("options", []))
            if kind == "radio":
                wanted = normalize(str(raw_value))
                selected = next(
                    (
                        item for item in options
                        if wanted in {normalize(str(item.get("value", ""))), normalize(str(item.get("label", "")))}
                    ),
                    None,
                )
                if selected is None:
                    raise AutomationStopped(f"远程修改选项不存在：{key}")
                await self._remote_option_locator(page, field, selected).check()
                continue
            if kind == "checkbox-group":
                wanted_values = {
                    str(item) for item in (raw_value if isinstance(raw_value, list) else [])
                }
                for option in options:
                    locator = self._remote_option_locator(page, field, option)
                    if str(option.get("value", "")) in wanted_values:
                        await locator.check()
                    else:
                        await locator.uncheck()
                continue

            locator = self._remote_field_locator(page, field)
            if kind == "checkbox":
                if bool(raw_value):
                    await locator.check()
                else:
                    await locator.uncheck()
            elif kind == "select-one":
                try:
                    await locator.select_option(value=str(raw_value))
                except Exception:
                    await locator.select_option(label=str(raw_value))
            else:
                await locator.fill(str(raw_value))
                if field.get("combobox"):
                    await self._choose_autocomplete_option(page, str(raw_value))

    @staticmethod
    def _remote_field_locator(page: Any, field: dict[str, Any]) -> Any:
        if field.get("element_id"):
            return page.locator(f"[id={json.dumps(str(field['element_id']))}]")
        return page.locator(f"[name={json.dumps(str(field.get('name', '')))}]").first

    @staticmethod
    def _remote_option_locator(
        page: Any, field: dict[str, Any], option: dict[str, Any]
    ) -> Any:
        if option.get("element_id"):
            return page.locator(f"[id={json.dumps(str(option['element_id']))}]")
        group = page.locator(f"[name={json.dumps(str(field.get('name', '')))}]")
        return group.nth(int(option.get("index", 0)))

    async def _expand_final_review_sections(self, page: Any, heading: str) -> None:
        """展开最终复核页的全部 accordion，确保截图和 PDF 包含答案。"""
        hide_all = page.get_by_role("button", name="Hide all sections", exact=True)
        if await hide_all.count() and await hide_all.first.is_visible():
            return

        show_all = page.get_by_role("button", name="Show all sections", exact=True)
        if not await show_all.count() or not await show_all.first.is_visible():
            # 页面没有 accordion 时不阻断；内容本身已经全部可见。
            return

        await self._audit("expand-final-review", text="Show all sections", url=page.url)
        await show_all.first.click()
        for _ in range(30):
            hide_all = page.get_by_role(
                "button", name="Hide all sections", exact=True
            )
            if await hide_all.count() and await hide_all.first.is_visible():
                await page.wait_for_timeout(300)
                return
            await page.wait_for_timeout(100)

        await self._snapshot(
            page, heading, reason="final-review-expand-failed"
        )
        raise AutomationStopped(
            "已点击 Show all sections，但复核页面未完成展开，已停止避免生成不完整 PDF。"
        )

    async def _verify_final_submission(
        self, page: Any, previous_heading: str, action: str
    ) -> None:
        """等待提交跳转，避免按钮刚点击就关闭浏览器并误报完成。"""
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=30_000)
        except Exception:
            # 某些 HMRC 页面使用客户端跳转；下面仍会检查 URL 和标题。
            pass
        await page.wait_for_timeout(500)
        heading = await self._heading(page)
        if self._is_final_page(page.url, heading):
            await self._snapshot(
                page,
                heading or previous_heading,
                reason="final-submit-not-completed",
            )
            raise AutomationStopped(
                f"已点击 {action}，但页面仍停留在最终复核页，"
                "申请可能尚未提交，请人工检查。"
            )
        await self._audit(
            "application-submitted", action=action, url=page.url, heading=heading,
            **await self._submission_receipt(page, heading),
        )

    async def _submission_receipt(self, page: Any, heading: str) -> dict[str, str]:
        result = {"outcome": "unverified", "reference": ""}
        try:
            text = await page.locator("body").inner_text()
            result["outcome"], result["reference"] = submission_evidence(page.url, heading, text)
        except Exception:
            pass
        try:
            result["receipt_png"] = str(await self._snapshot(page, heading, reason="submission-receipt"))
            pdf = await self._save_page_pdf(page, "submission-receipt")
            if pdf:
                result["receipt_pdf"] = str(pdf)
        except Exception:
            # 回执存档失败不能触发再次提交；结果仍可显示待核实。
            pass
        return result

    async def _save_page_pdf(self, page: Any, reason: str) -> Path | None:
        """整页存成 PDF 供核对和打印；认证页不存档。"""
        if self._is_auth_page(page.url) or self._is_vat_email_page(page.url, VAT_EMAIL_CODE_PATH):
            return None
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        target = self.settings.artifacts_dir / f"{stamp}-{reason}.pdf"
        try:
            await page.pdf(path=str(target), print_background=True, format="A4")
        except Exception:
            # headed Chromium 上 page.pdf 未必可用，且这是尽力而为的附加产物，
            # 任何失败都不应中断已经走到最终页的流程。
            return None
        if not target.is_file():
            return None
        target.chmod(0o600)
        return target

    @staticmethod
    async def _heading(page: Any) -> str:
        heading = page.locator("h1").first
        return (await heading.inner_text()).strip() if await heading.count() else ""

    @staticmethod
    def _is_vat_email_page(url: str, path: str) -> bool:
        parsed = urlparse(url)
        return (parsed.scheme == "https"
                and parsed.hostname in {"tax.service.gov.uk", "www.tax.service.gov.uk"}
                and parsed.path.rstrip("/") == path)

    @staticmethod
    def _is_auth_page(url: str) -> bool:
        parsed = urlparse(url)
        if parsed.hostname in AUTH_HOSTS:
            return True
        path = parsed.path.casefold()
        if parsed.hostname in {"tax.service.gov.uk", "www.tax.service.gov.uk"}:
            return any(
                marker in path
                for marker in (
                    "/sign-in-to-hmrc-online-services/identity/",
                    "/sign-in/identity/",
                )
            )
        return any(
            marker in parsed.path
            for marker in ("/sign-in", "/multi-factor/", "/credentials/")
        )

    @staticmethod
    def _is_application_page(url: str) -> bool:
        """真实申请数据区：这些路径会写入客户真实资料，测试配置必须挡在前面。"""
        path = urlparse(url).path.casefold()
        return any(
            marker in path
            for marker in (
                "/register-for-vat/",
                "/check-if-you-can-register-for-vat/",
                "/identify-your-overseas-business/",
                "/identify-your-sole-trader-business/",
                "/sic-search/",
                "/customs-registration-services/eori-only/",
            )
        )

    @staticmethod
    def _is_honesty_declaration_page(url: str) -> bool:
        return "/register-for-vat/honesty-declaration" in urlparse(url).path.casefold()

    def _is_final_page(self, url: str, heading: str) -> bool:
        haystack = normalize(url + " " + heading)
        path = urlparse(url).path.casefold()
        if any(
            marker in path
            for marker in (
                "/register-for-vat/check-your-answers",
                "/register-for-vat/check-confirm-answers",
                "/customs-registration-services/eori-only/register/review-details",
            )
        ):
            return True
        return any(normalize(marker) in haystack for marker in FINAL_MARKERS)

    async def _snapshot(
        self,
        page: Any,
        heading: str,
        *,
        reason: str,
        missing: list[str] | None = None,
    ) -> Path:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        png = self.settings.artifacts_dir / f"{stamp}-{reason}.png"
        metadata = self.settings.artifacts_dir / "current-page.json"
        if self._is_auth_page(page.url) or self._is_vat_email_page(page.url, VAT_EMAIL_CODE_PATH):
            # 整页遮盖：设置密钥可能同时出现在二维码、文本和输入框中。
            from PIL import Image, ImageDraw
            placeholder = Image.new("RGB", (960, 160), "#f1f5f9")
            ImageDraw.Draw(placeholder).text(
                (24, 60), "Authentication page hidden. Inspect the local browser to continue.",
                fill="#334155",
            )
            png.touch(mode=0o600, exist_ok=True)
            placeholder.save(png)
        else:
            await page.screenshot(path=str(png), full_page=True)
        png.chmod(0o600)
        metadata.write_text(
            json.dumps(
                {
                    "reason": reason,
                    "url": _safe_url(page.url),
                    "heading": heading,
                    "missing": missing or [],
                    "screenshot": str(png),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        metadata.chmod(0o600)
        return png

    async def _audit(self, event: str, **details: Any) -> None:
        if "url" in details:
            details["url"] = _safe_url(str(details["url"]))
        record = {
            "time": datetime.now(UTC).isoformat(),
            "event": event,
            **self.audit_context,
            **details,
        }
        with self.audit_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        if self.event_handler is not None:
            result = self.event_handler(record)
            if result is not None:
                await result

    async def _save_state(self, url: str) -> None:
        if self._is_auth_page(url):
            # 身份认证 URL 常带一次性会话标识，不保存为可恢复断点。
            url = self.settings.start_url
        elif self._is_vat_email_page(url, VAT_EMAIL_CODE_PATH):
            url = _safe_url(url)
        state = self.settings.artifacts_dir / "state.json"
        state.write_text(
            json.dumps({"url": url}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        state.chmod(0o600)

    def _resume_url(self) -> str | None:
        state = self.settings.artifacts_dir / "state.json"
        if not state.exists():
            return None
        saved = json.loads(state.read_text(encoding="utf-8")).get("url")
        if saved != self.settings.start_url:
            return saved

        history = self.settings.profile_dir / "Default" / "History"
        if not history.exists():
            return saved
        try:
            connection = sqlite3.connect(
                f"file:{history}?mode=ro", uri=True, timeout=1
            )
            try:
                row = connection.execute(
                    """
                    SELECT url FROM urls
                    WHERE url LIKE 'https://www.access.service.gov.uk/%'
                      AND (url LIKE '%/multi-factor/%'
                           OR url LIKE '%/registration/%')
                    ORDER BY last_visit_time DESC
                    LIMIT 1
                    """
                ).fetchone()
            finally:
                connection.close()
        except sqlite3.Error:
            return saved
        return str(row[0]) if row else saved


def _masked_value(value: str, *, head: int = 2, tail: int = 2) -> str:
    """审计日志里只留掩码，避免完整凭据/标识落盘。"""
    text = str(value)
    if len(text) <= head + tail:
        return "*" * len(text)
    return f"{text[:head]}{'*' * (len(text) - head - tail)}{text[-tail:]}"


def _safe_url(value: str) -> str:
    """移除查询参数及登录/MFA 路径中的一次性标识。"""
    parsed = urlparse(value)
    path = re.sub(
        r"(/(?:identity/(?:sign-in|tax-agent|organisation)|multi-factor/[^/]+|credentials/[^/]+))(?:/.*)?$",
        r"\1/[redacted]",
        parsed.path,
    )
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{host}{port}{path}"
