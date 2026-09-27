"""SkyMail 普通用户只读客户端；不提供创建、发送或删除接口。"""

from __future__ import annotations

import json
from typing import Any

import httpx


class MailError(RuntimeError):
    """仅使用本地固定文案，避免接口响应携带密码、令牌或邮件正文。"""


class SkyMailClient:
    def __init__(self, base_url: str, email: str, password: str, *, transport: Any = None) -> None:
        self.email = email
        self._password = password
        self._token = ""
        self._client = httpx.AsyncClient(
            base_url=base_url, timeout=10, follow_redirects=False,
            trust_env=False, transport=transport,
        )

    async def __aenter__(self) -> SkyMailClient:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        self._token = ""
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            async with self._client.stream(method, path, **kwargs) as response:
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > 4 * 1024 * 1024:
                        raise MailError("邮箱接口响应过大，已停止读取。")
        except httpx.HTTPError:
            raise MailError("邮箱服务连接失败，请检查网络与 HTTPS 配置。") from None
        if response.status_code == 429:
            raise MailError("邮箱服务限流，请稍后重试或人工输入验证码。")
        try:
            payload = json.loads(content)
        except ValueError:
            raise MailError("邮箱接口未返回有效 JSON，请核对实例地址及版本。") from None
        if not isinstance(payload, dict):
            raise MailError("邮箱接口返回结构不符合文档。")
        code = payload.get("code")
        if response.status_code == 401 or code == 401:
            raise PermissionError("邮箱身份令牌无效。")
        if response.status_code >= 300 or code != 200:
            raise MailError("邮箱接口拒绝请求，请检查账号状态、权限及配置。")
        return payload.get("data")

    async def login(self) -> None:
        try:
            data = await self._request("POST", "/api/login", json={
                "email": self.email, "password": self._password,
            })
        except PermissionError:
            raise MailError("邮箱登录失败，请检查邮箱密码。") from None
        if not isinstance(data, dict) or not isinstance(data.get("token"), str) or not data["token"]:
            raise MailError("邮箱登录接口没有返回有效令牌。")
        self._token = data["token"]

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if path not in {"/api/my/loginUserInfo", "/api/email/list"}:
            raise MailError("不允许调用此邮箱接口。")
        if not self._token:
            await self.login()
        for attempt in range(2):
            try:
                return await self._request("GET", path, params=params,
                                           headers={"Authorization": self._token})
            except PermissionError:
                if attempt:
                    raise MailError("邮箱令牌更新后仍无效，已停止自动读取。") from None
                await self.login()
        raise MailError("邮箱鉴权失败。")

    async def identity(self) -> tuple[int, int]:
        data = await self.get("/api/my/loginUserInfo")
        if not isinstance(data, dict) or not isinstance(data.get("account"), dict):
            raise MailError("邮箱用户信息结构不符合文档。")
        if str(data.get("type")) == "0" or "*" in (data.get("permKeys") or []):
            raise MailError("邮箱池不能使用管理员账号，请导入普通独立邮箱账号。")
        account = data["account"]
        if any(str(value).strip().casefold() != self.email.casefold()
               for value in (data.get("email", ""), account.get("email", ""))):
            raise MailError("邮箱登录身份与配置不一致，已停止。")
        account_id, user_id = account.get("accountId"), data.get("userId")
        if type(account_id) is not int or type(user_id) is not int or min(account_id, user_id) <= 0:
            raise MailError("邮箱接口没有返回有效账号 ID。")
        return account_id, user_id

    async def messages(self, account_id: int, *, cursor: int | None = None,
                       size: int = 50, full: bool = True) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "accountId": account_id, "type": 0, "allReceive": 0,
            "size": size, "full": int(full), "timeSort": int(cursor is not None),
        }
        if cursor is not None:
            params["emailId"] = cursor
        data = await self.get("/api/email/list", params)
        if not isinstance(data, dict) or not isinstance(data.get("list"), list):
            raise MailError("邮箱列表结构不符合文档。")
        result = data["list"]
        if len(result) > size:
            raise MailError("邮件分页行为与文档不一致。")
        for item in result:
            if not isinstance(item, dict) or type(item.get("emailId")) is not int or item["emailId"] <= 0:
                raise MailError("邮件缺少有效 ID，不能可靠匹配验证码。")
            # 实际部署的 full=0 摘要不含 accountId/userId；仍检查精确收件地址。
            # 完整邮件必须带匹配的 accountId；摘要如返回该字段，也不能忽略冲突。
            if (((full or "accountId" in item) and item.get("accountId") != account_id)
                    or item.get("type") != 0
                    or str(item.get("toEmail", "")).strip().casefold() != self.email.casefold()):
                raise MailError("邮件收件范围不一致，已停止读取以防串码。")
        ids = [item["emailId"] for item in result]
        if cursor is not None and (ids != sorted(set(ids)) or any(item <= cursor for item in ids)):
            raise MailError("邮件增量排序与文档不一致，请人工处理。")
        return result
