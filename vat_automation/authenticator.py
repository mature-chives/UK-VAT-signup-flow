"""按已确认的 GG 页面处理 Authenticator；二维码只在本机内存中解码。"""

from __future__ import annotations

import asyncio
import io
import re
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlparse

from .authenticator_store import AuthenticatorError, AuthenticatorStore, TotpParameters
from .config import normalize


class AuthenticatorFlow:
    def __init__(
        self, store: AuthenticatorStore, scope: str, *, allow_setup: bool,
        qr_ready: Callable[[str, bytes], Awaitable[None]] | None = None,
    ) -> None:
        self.store = store
        self.scope = scope
        self.allow_setup = allow_setup
        self.qr_ready = qr_ready
        self.binding_gateway = ""
        self.code_attempted = False
        self.app_named = False
        self.fallback_reported = False

    @staticmethod
    def is_mfa_page(url: str) -> bool:
        parsed = urlparse(url)
        return (parsed.scheme == "https"
                and parsed.hostname in {"access.service.gov.uk", "www.access.service.gov.uk"}
                and parsed.path.startswith("/multi-factor/"))

    async def handle(
        self, page: Any, heading: str, gateway: str, *,
        click: Callable[[Any, tuple[str, ...]], Awaitable[bool]],
        audit: Callable[..., Awaitable[None]],
        check_stop: Callable[[], None],
    ) -> bool:
        if not self.is_mfa_page(page.url):
            if self.binding_gateway:
                raise AuthenticatorError("未看到 Authenticator 绑定成功页，密钥仍为待确认；请人工核实后再继续。")
            return False
        try:
            return await self._handle(page, heading, gateway, click, audit, check_stop)
        except AuthenticatorError:
            raise
        except Exception:
            # Playwright 异常可能包含输入值，不能交给通用错误日志。
            raise AuthenticatorError("Authenticator 页面处理失败，已暂停；请人工检查浏览器。") from None

    async def _handle(
        self, page: Any, heading: str, gateway: str,
        click: Callable[[Any, tuple[str, ...]], Awaitable[bool]],
        audit: Callable[..., Awaitable[None]], check_stop: Callable[[], None],
    ) -> bool:
        title = normalize(heading)
        if not title:
            legend = page.locator("main legend").first
            if await legend.count():
                title = normalize(await legend.inner_text())
        body = normalize(await page.locator("main").inner_text())

        async def advance(event: str, message: str) -> bool:
            check_stop()
            if not await click(page, ("Continue",)):
                raise AuthenticatorError("Authenticator 页面未找到 Continue，已暂停。")
            await audit(event, message=message)
            return True

        if title == normalize("Add a way to get access codes"):
            if not re.fullmatch(r"[0-9]{10,12}", gateway):
                raise AuthenticatorError("尚未取得本次 GG 账号，不能自动开始绑定 Authenticator。")
            if not self.allow_setup or self.store.status(self.scope, gateway) == "active":
                raise AuthenticatorError("已有 GG 不会自动新增或替换验证器，请人工确认账号的验证方式。")
            option = page.get_by_role("radio", name="Authenticator app for smartphone or tablet", exact=True)
            if await option.count() != 1:
                raise AuthenticatorError("未找到已确认的 Authenticator 选项，已暂停。")
            check_stop()
            await option.check()
            return await advance("authenticator-selected", "已选择 Authenticator，开户无需短信验证手机号。")

        if title == normalize("You need an authenticator app on your device"):
            if not self.allow_setup:
                raise AuthenticatorError("当前仅允许使用已有验证器，不能自动新增绑定。")
            return await advance("authenticator-introduction", "正在准备本机托管的 Authenticator。")

        if title == normalize("Set up your authenticator app"):
            if not self.allow_setup:
                raise AuthenticatorError("当前仅允许使用已有验证器，不能自动读取新绑定密钥。")
            if self.store.status(self.scope, gateway) == "active":
                raise AuthenticatorError("该 GG 已有托管密钥，不会自动重新绑定。")
            params, qr_png = await self._parameters(page)
            check_stop()
            self.store.stage(self.scope, gateway, params)
            self.binding_gateway = gateway
            if self.qr_ready is not None:
                try:
                    await self.qr_ready(gateway, qr_png)
                except Exception:
                    # 可选展示失败不影响绑定，不记录异常中的认证材料。
                    await audit("authenticator-qr-unavailable", message="手机扫码图片暂不可用，自动注册继续。")
            del qr_png
            return await advance("authenticator-staged", "Authenticator 密钥已加密保存，等待验证绑定。")

        if title == normalize("Enter the access code") and normalize(
            "6 digit access code shown on your authenticator app"
        ) in body:
            status = self.store.status(self.scope, gateway)
            binding = bool(self.binding_gateway and self.binding_gateway == gateway)
            if self.code_attempted or (status != "active" and not binding):
                if not self.fallback_reported:
                    await audit("authenticator-manual", message="无法安全自动填入动态码，请使用原 Authenticator 人工输入；不会自动重绑。")
                    self.fallback_reported = True
                return False
            code_input = page.get_by_role("textbox", name="Access code", exact=True)
            if await code_input.count() != 1:
                raise AuthenticatorError("Authenticator 动态码输入框发生变化，请人工处理。")
            # 每轮任务只自动尝试一次，失败后人工接管，避免反复提交导致锁号。
            original_url = page.url
            for _ in range(3):
                check_stop()
                code, wait = self.store.issue(self.scope, gateway, pending=binding)
                if code is not None:
                    break
                await audit("authenticator-waiting", message="正在等待下一个动态码时间窗口，可随时停止任务。")
                await asyncio.sleep(wait)
                check_stop()
                if page.url != original_url:
                    raise AuthenticatorError("等待动态码时页面已改变，请人工检查。")
            else:
                raise AuthenticatorError("动态码时间窗口持续被占用，请稍后人工重试。")
            self.code_attempted = True
            check_stop()
            await code_input.fill(code)
            return await advance("authenticator-code-entered", "已自动填入 Authenticator 动态码。")

        if title == normalize("Create a name for your authenticator app"):
            if not self.binding_gateway or gateway != self.binding_gateway:
                raise AuthenticatorError("没有本次绑定的密钥记录，不能自动确认验证器命名。")
            check_stop()
            await page.get_by_role("textbox", name="App name", exact=True).fill("Registration assistant")
            result = await advance("authenticator-named", "已为验证器命名，等待官方确认。")
            self.app_named = True
            return result

        if title == normalize("You’ve successfully added a new way to get access codes"):
            if (not self.binding_gateway or gateway != self.binding_gateway or not self.app_named
                    or normalize("successfully set up access codes sent to the authenticator app") not in body):
                raise AuthenticatorError("无法将成功页面与本次 Authenticator 绑定对应，请人工核实。")
            self.store.activate(self.scope, gateway)
            self.binding_gateway = ""
            return await advance("authenticator-active", "HMRC 已确认 Authenticator 绑定成功，后续登录可自动生成动态码。")

        # 未确认的 MFA 页面不能落入通用的“有 Continue 就点”分支。
        if self.binding_gateway or "authenticator" in title:
            raise AuthenticatorError("遇到未识别的 Authenticator 页面，已暂停供人工处理。")
        return False

    @staticmethod
    async def _parameters(page: Any) -> tuple[TotpParameters, bytes]:
        import zxingcpp
        from PIL import Image

        qr = page.get_by_role("img", name="Scan this QR code with your authenticator app", exact=True)
        if await qr.count() != 1:
            raise AuthenticatorError("设置页二维码发生变化，不能安全提取 TOTP 参数。")
        # 不指定 path；原图、解码 URI、明文密钥均不写入运行产物。
        data = await qr.screenshot()
        with Image.open(io.BytesIO(data)) as image:
            decoded = zxingcpp.read_barcodes(image.convert("RGB"))
        if len(decoded) != 1:
            raise AuthenticatorError("二维码无法唯一解码，已停止自动绑定。")
        params = TotpParameters.from_uri(decoded[0].text)
        if params.digits != 6:
            raise AuthenticatorError("二维码位数与已确认的 HMRC 6 位验证页面不一致。")
        # 截图证实页面同时给出带 Secret key 标签的 b#secret，可交叉核对。
        secret = page.locator("b#secret")
        if await secret.count() != 1 or not re.match(
            r"^Secret key\s*:", await secret.locator("..").inner_text(), re.I
        ):
            raise AuthenticatorError("未找到可交叉核对的设置密钥，已停止自动绑定。")
        displayed = TotpParameters(await secret.inner_text())
        if displayed.secret != params.secret:
            raise AuthenticatorError("二维码与显示的设置密钥不一致，已停止自动绑定。")
        return params, data
