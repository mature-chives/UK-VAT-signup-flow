"""按独立用途处理 GG 建号 / VAT 个人邮箱验证码，不推断 MFA 或其他用途。"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import UTC, datetime
from email.utils import parseaddr
from html.parser import HTMLParser
from typing import Any

from .mail_pool import MailPool, code_rule_format, code_rules_for_purpose
from .skymail import MailError, SkyMailClient


class _PlainText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, _attrs: Any) -> None:
        if tag in {"script", "style"}:
            self.hidden += 1
        elif tag in {"br", "p", "div", "td"}:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def extract_code(message: dict[str, Any], rules: list[dict[str, Any]]) -> str | None:
    sender = parseaddr(str(message.get("sendEmail", "")))[1].casefold()
    subject = str(message.get("subject", "")).casefold()
    text = str(message.get("text") or "")
    if not text:
        parser = _PlainText()
        parser.feed(str(message.get("content") or "")[:100_000])
        text = " ".join(parser.parts)
    text = " ".join(text[:100_000].split())
    candidates: set[str] = set()
    for rule in rules:
        if sender != rule["sender"].strip().casefold() or rule["subject_contains"].casefold() not in subject:
            continue
        prefix = " ".join(rule["code_prefix"].split())
        length, characters = code_rule_format(rule)
        # 仅前缀忽略大小写，验证码字符集严格按已确认模板匹配，值原样返回。
        expression = ("(?i:" + re.escape(prefix) + r")\s*[:：]?\s*("
                      + characters + "{" + str(length) + r"})(?![0-9A-Za-z])")
        candidates.update(re.findall(expression, text))
    if len(candidates) > 1:
        raise MailError("邮件包含多个候选验证码，请人工核对。")
    return next(iter(candidates), None)


class MailVerifier:
    def __init__(
        self, pool: MailPool, allocation: dict[str, Any], client: SkyMailClient,
        *, purpose: str = "gg_signup",
    ) -> None:
        self.pool = pool
        self.allocation = allocation
        self.client = client
        self.purpose = purpose
        self.rules = code_rules_for_purpose(allocation.get("code_rules", []), purpose)
        self.cursor: int | None = None
        self.sent_after = 0.0
        self.attempted = False
        self.prepared_once = False
        self.notice = ""

    async def prepare(self, email: str) -> None:
        self.cursor = None
        if not self.rules:
            self.notice = "未配置已核对的邮件模板，请人工输入验证码。"
            return
        if self.prepared_once:
            self.notice = "本验证环节重复发码或返回邮箱页，请人工核对验证码。"
            return
        self.prepared_once = True
        try:
            if email.casefold() != self.allocation["email"].casefold():
                raise MailError("发码邮箱与任务分配不一致，已关闭自动取码。")
            account_id, user_id = await self.client.identity()
            if (account_id, user_id) != (self.allocation["account_id"], self.allocation["user_id"]):
                raise MailError("邮箱账号身份发生变化，请重新检查邮箱池。")
            messages = await self.client.messages(account_id, size=1, full=False)
            self.cursor = max((item["emailId"] for item in messages), default=0)
            self.sent_after = time.time()
        except MailError as exc:
            self.notice = str(exc)

    async def receive(self, *, timeout: float = 150, interval: float = 4) -> str | None:
        if self.cursor is None or self.attempted:
            return None
        # 每次发码至多自动提交一次；无效码不会反复撞码或自动重发。
        self.attempted = True
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            candidates: list[tuple[int, str]] = []
            for _ in range(10):
                messages = await self.client.messages(self.allocation["account_id"], cursor=self.cursor)
                if any(item["emailId"] <= self.cursor for item in messages):
                    raise MailError("邮件游标行为与文档不一致，请人工输入验证码。")
                for message in messages:
                    if message.get("isDel", 0) != 0:
                        continue
                    try:
                        received = datetime.strptime(message["createTime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC).timestamp()
                    except (KeyError, TypeError, ValueError):
                        raise MailError("邮件没有有效的 UTC 接收时间，不能自动取码。") from None
                    if received < self.sent_after - 2 or received > time.time() + 30:
                        continue
                    code = extract_code(message, self.rules)
                    if code:
                        candidates.append((message["emailId"], code))
                if messages:
                    self.cursor = max(item["emailId"] for item in messages)
                if len(messages) < 50:
                    break
            else:
                raise MailError("新邮件过多，无法可靠确认验证码，请人工处理。")
            if len(candidates) > 1:
                raise MailError("收到多封候选验证码邮件，请人工核对，程序不会逐个试码。")
            if candidates:
                email_id, code = candidates[0]
                if not self.pool.mark_attempted(self.allocation["email"], email_id):
                    raise MailError("该邮件已用于验证码尝试，请人工确认。")
                return code
            await asyncio.sleep(min(interval, max(0, deadline - time.monotonic())))
        raise MailError("自动收码等待超时，请人工查看邮箱并输入验证码。")
