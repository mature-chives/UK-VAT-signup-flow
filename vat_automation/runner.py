from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .config import Settings, normalize


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
AUTH_HOSTS = {
    "access.service.gov.uk",
    "www.access.service.gov.uk",
}


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


class VatAutomation:
    def __init__(self, settings: Settings, *, interactive: bool = True) -> None:
        self.settings = settings
        self.interactive = interactive
        self.settings.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.settings.profile_dir.mkdir(parents=True, exist_ok=True)
        self.audit_path = self.settings.artifacts_dir / "audit.jsonl"

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
            async with async_playwright() as playwright:
                context = await playwright.chromium.launch_persistent_context(
                    str(self.settings.profile_dir),
                    channel=self.settings.browser_channel or None,
                    headless=self.settings.headless,
                    viewport={"width": 1440, "height": 1000},
                )
                page = context.pages[0] if context.pages else await context.new_page()
                target = self._resume_url() if resume else None
                await page.goto(
                    target or self.settings.start_url, wait_until="domcontentloaded"
                )
                try:
                    await self._drive(page)
                finally:
                    await self._save_state(page.url)
                    await context.close()
        except PlaywrightError as exc:
            raise RuntimeError(f"浏览器自动化失败：{exc}") from exc

    async def _drive(self, page: Any) -> None:
        for step in range(1, self.settings.max_steps + 1):
            await page.wait_for_load_state("domcontentloaded")
            heading = await self._heading(page)
            await self._audit("page", step=step, url=page.url, heading=heading)

            if self._is_remote_error_heading(heading):
                await self._snapshot(page, heading, reason="remote-service-error")
                raise AutomationStopped(
                    f"远端服务返回 {heading}，请稍后重试或改用非 headless 模式。"
                )

            if self._is_auth_page(page.url):
                await self._handle_auth(page, heading)
                continue

            if not self.settings.allow_live_application and self._is_application_page(
                page.url
            ):
                await self._snapshot(page, heading, reason="live-application-disabled")
                raise AutomationStopped(
                    "测试配置禁止向真实 HMRC VAT 申请写入虚构资料。"
                )

            verification_input = await self._verification_code_input(page, heading)
            if verification_input is not None:
                await self._enter_verification_code(page, verification_input)
                continue

            if self._is_honesty_declaration_page(page.url):
                await self._handle_honesty_declaration(page, heading)
                continue

            if self._is_final_page(page.url, heading):
                await self._snapshot(page, heading, reason="final-review")
                raise AutomationStopped(
                    "已到达最终复核/声明页面。安全策略禁止自动提交。"
                )

            if "/register-for-vat/manage-registrations" in page.url:
                if not await self._click_create_vat_application(page):
                    await self._snapshot(
                        page, heading, reason="missing-create-vat-application"
                    )
                    raise AutomationStopped(
                        "申请管理页面未找到 Create a new application。"
                    )
                continue

            action = self.settings.action_for(page.url, heading)
            if action == "stop":
                await self._snapshot(page, heading, reason="configured-stop")
                raise AutomationStopped("配置要求在当前页面停止。")
            if action.startswith("skip:"):
                name = action.removeprefix("skip:")
                if not await self._click_named_action(page, name):
                    await self._snapshot(page, heading, reason="missing-skip-action")
                    raise AutomationStopped(f"找不到跳过操作：{name}")
                continue

            missing = await self._fill_until_stable(page, heading)
            if missing:
                await self._snapshot(
                    page, heading, reason="missing-config", missing=missing
                )
                raise AutomationStopped(
                    "当前页面缺少必填配置：" + "；".join(sorted(set(missing)))
                )

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
            await page.wait_for_timeout(350)

        raise AutomationStopped(f"已达到最大步骤数 {self.settings.max_steps}。")

    async def _handle_auth(self, page: Any, heading: str) -> None:
        """自动处理凭据和安全方式，仅验证码由用户即时输入。"""
        password = page.locator('input[type="password"]:visible')
        if await password.count():
            user_password = os.environ.get("HMRC_PASSWORD", "")
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

            user_id = os.environ.get("HMRC_USER_ID", "")
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
            email = os.environ.get("HMRC_EMAIL", "")
            if not email:
                raise AutomationStopped("创建登录凭据需要环境变量 HMRC_EMAIL。")
            await email_input.fill(email)
            await self._audit("auth-email-filled", url=page.url)
            if not await self._click_auth_action(page, ("Continue",)):
                raise AutomationStopped("邮箱页面未找到继续按钮。")
            return

        if "/registration/name" in page.url or "full name" in normalize(heading):
            found, configured_name = self.settings.answer_for(
                "Full name", page.url, heading
            )
            full_name = os.environ.get(
                "HMRC_FULL_NAME", str(configured_name) if found else ""
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
            found, configured_country = self.settings.answer_for(
                "Country", page.url, heading
            )
            country = os.environ.get(
                "HMRC_MFA_PHONE_COUNTRY",
                str(configured_country) if found else "",
            )
            if not country:
                raise AutomationStopped(
                    "非英国手机号需要 HMRC_MFA_PHONE_COUNTRY 或配置中的 Country。"
                )
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
            if not self.interactive:
                raise AutomationStopped("验证码页面需要用户输入验证码。")
            await self._enter_verification_code(page, code_input)
            return

        telephone = page.locator('main form input[type="tel"]:visible').first
        if await telephone.count():
            phone = os.environ.get("HMRC_MFA_PHONE", "")
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
                method = os.environ.get(
                    "HMRC_SIGN_IN_METHOD", "Create new sign in details"
                )
                audit_event = "auth-method-selected"
            elif "tax agent" in heading_normalized:
                method = os.environ.get("HMRC_IS_TAX_AGENT", "No")
                audit_event = "auth-tax-agent-selected"
            elif "business or organisation" in heading_normalized:
                method = os.environ.get("HMRC_ACCESS_AS_BUSINESS", "Yes")
                audit_event = "auth-organisation-selected"
            elif (
                "/multi-factor/mobile-number-uk/" in page.url
                or "adding a uk mobile number" in heading_normalized
            ):
                method = os.environ.get("HMRC_MFA_PHONE_IS_UK", "Yes")
                audit_event = "mfa-phone-country-selected"
            else:
                method = os.environ.get("HMRC_MFA_METHOD", "Text message")
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
        if "code" not in normalize(heading):
            return None
        locator = page.locator(
            'main form input[autocomplete="one-time-code"]:visible, '
            'main form input[name*="code" i]:visible, '
            'main form input[id*="code" i]:visible'
        ).first
        return locator if await locator.count() else None

    async def _enter_verification_code(self, page: Any, code_input: Any) -> None:
        if not self.interactive:
            raise AutomationStopped("验证码页面需要用户输入验证码。")
        code = await asyncio.to_thread(input, "请输入刚收到的验证码（输入 q 退出）：")
        if code.strip().casefold() == "q":
            raise AutomationStopped("用户在验证码阶段退出。")
        if not code.strip():
            raise AutomationStopped("验证码为空。")
        await code_input.fill(code.strip())
        await self._audit("verification-code-entered", url=page.url)
        if not await self._click_auth_action(
            page, ("Save and continue", "Continue", "Submit", "Confirm", "Sign in")
        ):
            raise AutomationStopped("验证码页面未找到继续按钮。")

    async def _click_auth_action(self, page: Any, names: tuple[str, ...]) -> bool:
        for name in names:
            button = page.get_by_role("button", name=name, exact=True)
            if await button.count() and await button.first.is_visible():
                await button.first.click()
                return True
        return False

    async def _controls(self, page: Any) -> list[Control]:
        raw = await page.locator("main form input, main form select, main form textarea").evaluate_all(
            """
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
            found, value = self.settings.answer_for(
                control.label,
                page.url,
                heading,
                aliases=(control.name, control.element_id),
            )
            if not found:
                if control.required:
                    missing.append(control.label or key)
                continue

            locator = self._locator(page, control)
            try:
                value = self._resolve_value(value)
            except KeyError as exc:
                missing.append(f"{control.label or key}（缺少环境变量 {exc.args[0]}）")
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

    @staticmethod
    def _resolve_value(value: Any) -> Any:
        if isinstance(value, str) and value.startswith("env:"):
            variable = value.removeprefix("env:")
            if not os.environ.get(variable):
                raise KeyError(variable)
            return os.environ[variable]
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
            return
        exact = page.get_by_role("option", name=value, exact=True)
        if await exact.count() and await exact.first.is_visible():
            await exact.first.click()
            return
        for option in await options.all():
            if await option.is_visible():
                await option.click()
                return

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
                    await locator.first.click()
                    return True
        return False

    async def _click_named_action(self, page: Any, name: str) -> bool:
        for role in ("button", "link"):
            locator = page.get_by_role(role, name=name, exact=True)
            if await locator.count() and await locator.first.is_visible():
                await self._audit("click", text=name, url=page.url)
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
        warnings = self.settings.live_application_warnings(os.environ)
        if warnings:
            await self._snapshot(
                page,
                heading,
                reason="synthetic-live-data",
                missing=warnings,
            )
            raise AutomationStopped(
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


    @staticmethod
    async def _heading(page: Any) -> str:
        heading = page.locator("h1").first
        return (await heading.inner_text()).strip() if await heading.count() else ""

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
        path = urlparse(url).path.casefold()
        return any(
            marker in path
            for marker in (
                "/register-for-vat/",
                "/check-if-you-can-register-for-vat/",
                "/identify-your-overseas-business/",
                "/identify-your-sole-trader-business/",
                "/sic-search/",
            )
        )

    @staticmethod
    def _is_honesty_declaration_page(url: str) -> bool:
        return "/register-for-vat/honesty-declaration" in urlparse(url).path.casefold()

    def _is_final_page(self, url: str, heading: str) -> bool:
        haystack = normalize(url + " " + heading)
        path = urlparse(url).path.casefold()
        if "/register-for-vat/check-your-answers" in path:
            return True
        return any(normalize(marker) in haystack for marker in FINAL_MARKERS)

    async def _snapshot(
        self,
        page: Any,
        heading: str,
        *,
        reason: str,
        missing: list[str] | None = None,
    ) -> None:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        png = self.settings.artifacts_dir / f"{stamp}-{reason}.png"
        metadata = self.settings.artifacts_dir / "current-page.json"
        await page.screenshot(path=str(png), full_page=True)
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

    async def _audit(self, event: str, **details: Any) -> None:
        if "url" in details:
            details["url"] = _safe_url(str(details["url"]))
        record = {
            "time": datetime.now(UTC).isoformat(),
            "event": event,
            **details,
        }
        with self.audit_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")

    async def _save_state(self, url: str) -> None:
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
