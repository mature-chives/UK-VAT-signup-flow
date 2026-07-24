from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


UNSET = object()
UK_NEXT_MONTH_FIRST_PREFIX = "date:uk-next-month-first."
UK_NEXT_MONTH_VAT_STAGGER = "vat-return-stagger:uk-next-month-first"
LIVE_APPLICATION_TEST_MARKERS: tuple[tuple[str, str], ...] = (
    ("example.com", "检测到示例邮箱域名 example.com"),
    ("test-", "检测到 TEST- 测试标记"),
    ("synthetic", "检测到 Synthetic 测试标记"),
    ("northstar", "检测到 Northstar 测试标记"),
    ("tester", "检测到 Tester 测试标记"),
)
ADDRESS_MAX_LENGTH = 35
ADDRESS_ABBREVIATIONS: tuple[tuple[str, str], ...] = (
    (r"\bBuildings?\b", "Bldg"),
    (r"\bApartments?\b", "Apt"),
    (r"\bRooms?\b", "Rm"),
    (r"\bFloors?\b", "Fl"),
    (r"\bAvenue\b", "Ave"),
    (r"\bBoulevard\b", "Blvd"),
    (r"\bRoad\b", "Rd"),
    (r"\bStreet\b", "St"),
)
ANSWER_ALIAS_GROUPS: tuple[tuple[str, ...], ...] = (
    ("Business name", "What is the official name of the business?"),
    (
        "What does the business do?",
        "Describe the type of goods or services the business sells.",
    ),
)


def normalize(text: str) -> str:
    """将页面标签归一化，降低标点、空白变化造成的匹配失败。"""
    return re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()


def uk_next_month_first(reference_date: date | None = None) -> date:
    """返回英国当地当前月份的下个月 1 日。"""
    current = reference_date or datetime.now(ZoneInfo("Europe/London")).date()
    if current.month == 12:
        return date(current.year + 1, 1, 1)
    return date(current.year, current.month + 1, 1)


def vat_return_stagger_for_registration_month(month: int) -> str:
    """根据 VAT 注册月份返回 HMRC 季度申报组选项值。"""
    if month not in range(1, 13):
        raise ValueError(f"无效的注册月份：{month}")
    if month in {2, 5, 8, 11}:
        return "jan"
    if month in {3, 6, 9, 12}:
        return "feb"
    return "mar"


def resolve_dynamic_value(value: Any, target_date: date) -> Any:
    if value == UK_NEXT_MONTH_VAT_STAGGER:
        return vat_return_stagger_for_registration_month(target_date.month)
    if not isinstance(value, str) or not value.startswith(
        UK_NEXT_MONTH_FIRST_PREFIX
    ):
        return value
    component = value.removeprefix(UK_NEXT_MONTH_FIRST_PREFIX)
    components = {
        "day": target_date.day,
        "month": target_date.month,
        "year": target_date.year,
    }
    if component not in components:
        raise ValueError(f"不支持的动态日期字段：{value}")
    return str(components[component])


def _clean_address_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip(" ,")


def _address_value(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(
            part for item in value if (part := _clean_address_text(item))
        )
    return _clean_address_text(value)


def _fit_address_field(value: str, field: str, *, locality: bool = False) -> str:
    if len(value) <= ADDRESS_MAX_LENGTH:
        return value

    shortened = value
    if locality:
        shortened = re.sub(
            r"\b(?:District|City)\b", "", shortened, flags=re.IGNORECASE
        )
        shortened = re.sub(r"\s+,", ",", shortened)
        shortened = re.sub(r"\s+", " ", shortened).strip(" ,")
        if len(shortened) <= ADDRESS_MAX_LENGTH:
            return shortened

    for pattern, replacement in ADDRESS_ABBREVIATIONS:
        shortened = re.sub(pattern, replacement, shortened, flags=re.IGNORECASE)
    shortened = re.sub(r"\s+", " ", shortened).strip(" ,")
    if len(shortened) <= ADDRESS_MAX_LENGTH:
        return shortened
    raise ValueError(
        f"{field} 超过 {ADDRESS_MAX_LENGTH} 个字符且无法安全缩写：{value}"
    )


def build_address_answers(address: Mapping[str, Any]) -> dict[str, str]:
    """将结构化国际地址转换为 HMRC 表单字段，不截断关键地址内容。"""
    if not address:
        return {}

    premises = _address_value(address.get("premises", address.get("line1", "")))
    street = _address_value(address.get("street", address.get("line2", "")))
    locality_parts = [
        _clean_address_text(address.get(key, ""))
        for key in ("locality", "city", "region")
    ]
    locality = ", ".join(dict.fromkeys(part for part in locality_parts if part))

    answers: dict[str, str] = {}
    if premises:
        answers["Address line 1"] = _fit_address_field(
            premises, "Address line 1"
        )
    if street:
        answers["Address line 2"] = _fit_address_field(street, "Address line 2")
    if locality:
        answers["Town or city"] = _fit_address_field(
            locality, "Town or city", locality=True
        )
    postcode = _clean_address_text(address.get("postcode", ""))
    country = _clean_address_text(address.get("country", ""))
    if postcode:
        answers["Postcode"] = postcode
    if country:
        answers["Country"] = country
    return answers


@dataclass(slots=True)
class PageRule:
    path_contains: str = ""
    heading_contains: str = ""
    answers: dict[str, Any] = field(default_factory=dict)
    default_answer: Any = field(default_factory=lambda: UNSET)
    action: str = "continue"

    def matches(self, url: str, heading: str) -> bool:
        return (
            (not self.path_contains or self.path_contains in url)
            and (
                not self.heading_contains
                or normalize(self.heading_contains) in normalize(heading)
            )
        )


@dataclass(slots=True)
class Settings:
    start_url: str
    profile_dir: Path
    artifacts_dir: Path
    answers: dict[str, Any]
    pages: list[PageRule]
    browser_channel: str = "chrome"
    headless: bool = False
    allow_live_application: bool = False
    max_steps: int = 250

    def answer_for(
        self,
        label: str,
        url: str,
        heading: str,
        aliases: tuple[str, ...] = (),
    ) -> tuple[bool, Any]:
        candidates: dict[str, Any] = dict(self.answers)
        matching_pages: list[PageRule] = []
        for page in self.pages:
            if page.matches(url, heading):
                matching_pages.append(page)
                candidates.update(page.answers)
        wanted = {normalize(label), *(normalize(alias) for alias in aliases if alias)}
        for group in ANSWER_ALIAS_GROUPS:
            normalized_group = {normalize(item) for item in group}
            if wanted & normalized_group:
                wanted.update(normalized_group)
        for key, value in candidates.items():
            key_normalized = normalize(key)
            if key_normalized in wanted:
                return True, value
        for page in reversed(matching_pages):
            if page.default_answer is not UNSET:
                return True, page.default_answer
        return False, None

    def action_for(self, url: str, heading: str) -> str:
        # 与 answers/default_answer 的覆盖规则保持一致：后置的具体页面规则
        # 应覆盖前面的通用路径规则。
        for page in reversed(self.pages):
            if page.matches(url, heading):
                return page.action
        return "continue"

    def live_application_warnings(
        self, env: Mapping[str, str] | None = None
    ) -> list[str]:
        warnings: list[str] = []
        seen: set[str] = set()
        for _, value in self._iter_live_application_values(env):
            text = str(value).strip()
            if not text:
                continue
            lowered = text.casefold()
            digits = re.sub(r"\D+", "", text)
            for marker, message in LIVE_APPLICATION_TEST_MARKERS:
                if marker in lowered and message not in seen:
                    warnings.append(message)
                    seen.add(message)
            reserved_phone = "检测到英国保留测试手机号 07700 900xxx"
            if digits.startswith("07700900") and len(digits) == 11:
                if reserved_phone not in seen:
                    warnings.append(reserved_phone)
                    seen.add(reserved_phone)
        return warnings

    def _iter_live_application_values(
        self, env: Mapping[str, str] | None = None
    ) -> Iterable[tuple[str, Any]]:
        for key, value in self.answers.items():
            yield key, value
        for page in self.pages:
            for key, value in page.answers.items():
                yield key, value
        if not env:
            return
        for key in (
            "HMRC_EMAIL",
            "HMRC_FULL_NAME",
            "HMRC_MFA_PHONE",
            "HMRC_MFA_PHONE_COUNTRY",
        ):
            if env.get(key):
                yield key, env[key]


def load_settings(path: Path) -> Settings:
    raw = json.loads(path.read_text(encoding="utf-8"))
    base = path.parent.resolve()
    target_date = uk_next_month_first()

    def resolve_answers(values: Mapping[str, Any]) -> dict[str, Any]:
        return {
            key: resolve_dynamic_value(value, target_date)
            for key, value in values.items()
        }

    def local_path(value: str) -> Path:
        candidate = Path(value).expanduser()
        return candidate if candidate.is_absolute() else base / candidate

    pages: list[PageRule] = []
    for item in raw.get("pages", []):
        page_answers = build_address_answers(item.get("address", {}))
        page_answers.update(resolve_answers(item.get("answers", {})))
        pages.append(
            PageRule(
                path_contains=item.get("match", {}).get("path_contains", ""),
                heading_contains=item.get("match", {}).get("heading_contains", ""),
                answers=page_answers,
                default_answer=resolve_dynamic_value(
                    item.get("default_answer", UNSET), target_date
                ),
                action=item.get("action", "continue"),
            )
        )
    global_answers = build_address_answers(raw.get("address", {}))
    global_answers.update(resolve_answers(raw.get("answers", {})))
    return Settings(
        start_url=raw.get(
            "start_url",
            "https://www.gov.uk/log-in-register-hmrc-online-services",
        ),
        profile_dir=local_path(raw.get("profile_dir", ".browser-profile")),
        artifacts_dir=local_path(raw.get("artifacts_dir", "artifacts")),
        answers=global_answers,
        pages=pages,
        browser_channel=str(raw.get("browser_channel", "chrome")),
        headless=bool(raw.get("headless", False)),
        allow_live_application=bool(raw.get("allow_live_application", False)),
        max_steps=int(raw.get("max_steps", 250)),
    )
