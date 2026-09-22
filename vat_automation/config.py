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
DOC_PREFIX = "doc:"
ENV_PREFIX = "env:"
LIVE_APPLICATION_TEST_MARKERS: tuple[tuple[str, str], ...] = (
    ("example.com", "检测到示例邮箱域名 example.com"),
    ("test-", "检测到 TEST- 测试标记"),
    ("synthetic", "检测到 Synthetic 测试标记"),
    ("northstar", "检测到 Northstar 测试标记"),
    ("tester", "检测到 Tester 测试标记"),
)
ADDRESS_MAX_LENGTH = 35
ANSWER_ALIAS_GROUPS: tuple[tuple[str, ...], ...] = (
    ("Business name", "What is the official name of the business?"),
    (
        "What does the business do?",
        "Describe the type of goods or services the business sells.",
    ),
)
FIXED_VAT_PAGE_RULES: tuple[tuple[str, dict[str, str], str], ...] = (
    (
        "/register-for-vat/standard-rate-turnover",
        {"standardRateSupplies": "10000"},
        "continue",
    ),
    (
        "/register-for-vat/reduced-rate-turnover",
        {"reducedRateSupplies": "0"},
        "continue",
    ),
    (
        "/register-for-vat/zero-rated-turnover",
        {"zeroRatedSupplies": "0"},
        "continue",
    ),
    (
        "/search-standard-industry-classification-codes",
        {"sicSearch": "47910"},
        "Search",
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


def is_placeholder_token(value: str) -> bool:
    return value.startswith(DOC_PREFIX) or value.startswith(ENV_PREFIX)


def is_placeholder_chain(value: Any) -> bool:
    """整段都是 doc: / env: 令牌时才按占位符链解析，避免误伤普通文案。"""
    if not isinstance(value, str) or not value:
        return False
    return all(is_placeholder_token(part) for part in value.split("|"))


def expand_document_address(
    source: str, document_values: Mapping[str, str] | None
) -> dict[str, str]:
    """把 doc:home / doc:business 展开为国际地址表单字段。"""
    if not source.startswith(DOC_PREFIX) or not document_values:
        return {}
    kind = source.removeprefix(DOC_PREFIX).strip()
    from .document_parser import extracted_address, extracted_home_address

    if kind == "home":
        raw = extracted_home_address(dict(document_values))
    elif kind == "business":
        raw = extracted_address(dict(document_values))
    else:
        return {}
    return build_international_address_answers(raw)


def _clean_address_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip(" ,")


def _address_value(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(
            part for item in value if (part := _clean_address_text(item))
        )
    return _clean_address_text(value)


def build_address_answers(address: Mapping[str, Any]) -> dict[str, str]:
    """将结构化地址配置转换为 HMRC 国际地址表的 5 行字段。

    与 doc:home / doc:business 共用同一套拆行逻辑，不缩写、不截断。
    """
    if not address:
        return {}
    locality_parts = [
        _clean_address_text(address.get(key, ""))
        for key in ("locality", "city", "region")
    ]
    normalized: dict[str, Any] = {
        "premises": address.get("premises", address.get("line1", "")),
        "street": address.get("street", address.get("line2", "")),
        "locality": ", ".join(
            dict.fromkeys(part for part in locality_parts if part)
        ),
        "postcode": address.get("postcode", ""),
        "country": address.get("country", ""),
    }
    return build_international_address_answers(normalized)


def build_international_address_answers(address: Mapping[str, Any]) -> dict[str, str]:
    """将完整英文地址分配到 HMRC 国际地址的 5 行表单，不做缩写。"""
    if not address:
        return {}
    labels = (
        "Address line 1",
        "Address line 2",
        "Address line 3 (optional)",
        "Address line 4 (optional)",
        "Address line 5 (optional)",
    )
    lines: list[str] = []
    for key in ("premises", "street", "locality", "city", "region"):
        value = _address_value(address.get(key, ""))
        if value:
            lines.extend(_split_full_address_component(value, key))

    # 某个较长组成部分被拆行后可能超过 5 行。优先从地址末尾合并
    # 相邻短行，例如 "Hangzhou City, Zhejiang Province"，保持原始顺序。
    while len(lines) > len(labels):
        merged = False
        for index in range(len(lines) - 2, -1, -1):
            candidate = f"{lines[index]}, {lines[index + 1]}"
            if len(candidate) <= ADDRESS_MAX_LENGTH:
                lines[index:index + 2] = [candidate]
                merged = True
                break
        if not merged:
            raise ValueError(
                "完整国际地址无法在不缩写的情况下放入 5 个地址栏："
                + " | ".join(lines)
            )

    answers: dict[str, str] = {}
    for label, value in zip(labels, lines, strict=False):
        answers[label] = value
    postcode = _clean_address_text(address.get("postcode", ""))
    country = _clean_address_text(address.get("country", ""))
    if postcode:
        answers["Postcode"] = postcode
        answers["Postcode (optional)"] = postcode
    if country:
        answers["Country"] = country
    return answers


def _split_full_address_component(value: str, field: str) -> list[str]:
    """按逗号和单词边界拆行，完整保留内容且每行不超过 35 字符。"""
    if len(value) <= ADDRESS_MAX_LENGTH:
        return [value]

    comma_parts = [
        part.strip() for part in re.split(r",\s*", value) if part.strip()
    ]
    parts: list[str] = []
    for part in comma_parts:
        if len(part) <= ADDRESS_MAX_LENGTH:
            parts.append(part)
            continue
        current = ""
        for word in part.split():
            if len(word) > ADDRESS_MAX_LENGTH:
                raise ValueError(
                    f"{field} 含有超过 {ADDRESS_MAX_LENGTH} 个字符的连续内容：{word}"
                )
            candidate = f"{current} {word}".strip()
            if len(candidate) <= ADDRESS_MAX_LENGTH:
                current = candidate
            else:
                parts.append(current)
                current = word
        if current:
            parts.append(current)

    lines: list[str] = []
    for part in parts:
        candidate = f"{lines[-1]}, {part}" if lines else part
        if lines and len(candidate) <= ADDRESS_MAX_LENGTH:
            lines[-1] = candidate
        else:
            lines.append(part)
    return lines


@dataclass(slots=True)
class PageRule:
    path_contains: str = ""
    heading_contains: str = ""
    answers: dict[str, Any] = field(default_factory=dict)
    default_answer: Any = field(default_factory=lambda: UNSET)
    action: str = "continue"
    address_source: str = ""

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
        document_values: Mapping[str, str] | None = None,
    ) -> tuple[bool, Any]:
        matching_pages: list[PageRule] = []
        for page in self.pages:
            if page.matches(url, heading):
                matching_pages.append(page)
        wanted = {normalize(label), *(normalize(alias) for alias in aliases if alias)}
        for group in ANSWER_ALIAS_GROUPS:
            normalized_group = {normalize(item) for item in group}
            if wanted & normalized_group:
                wanted.update(normalized_group)

        # 同一层内：后写的页面规则优先于先写的，页面整表优先于全局。
        # 申请人资料通过 doc: 占位符或 address_source 提供，不写进全局 Email address。
        answer_maps: list[Mapping[str, Any]] = []
        for page in reversed(matching_pages):
            if page.address_source:
                expanded = expand_document_address(
                    page.address_source, document_values
                )
                if expanded:
                    answer_maps.append(expanded)
            answer_maps.append(page.answers)
        answer_maps.append(self.answers)
        for answers in answer_maps:
            for key, value in answers.items():
                if normalize(key) in wanted:
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
        self,
        env: Mapping[str, str] | None = None,
        document_values: Mapping[str, str] | None = None,
    ) -> list[str]:
        warnings: list[str] = []
        seen: set[str] = set()
        for _, value in self._iter_live_application_values(env, document_values):
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
        self,
        env: Mapping[str, str] | None = None,
        document_values: Mapping[str, str] | None = None,
    ) -> Iterable[tuple[str, Any]]:
        for key, value in self.answers.items():
            if not is_placeholder_chain(value):
                yield key, value
        for page in self.pages:
            for key, value in page.answers.items():
                if not is_placeholder_chain(value):
                    yield key, value
        if document_values:
            for key, value in document_values.items():
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
        address = item.get("address", {})
        address_source = ""
        if isinstance(address, str):
            address_source = address.strip()
            if address_source and not address_source.startswith(DOC_PREFIX):
                raise ValueError(f"页面 address 必须是对象或 doc: 引用：{address}")
            page_answers = {}
        else:
            page_answers = build_address_answers(address)
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
                address_source=address_source,
            )
        )
    # 这些是当前业务模型的固定规则，追加在最后以防止配置文件中的旧值覆盖。
    for path_contains, answers, action in FIXED_VAT_PAGE_RULES:
        pages.append(
            PageRule(
                path_contains=path_contains,
                answers=dict(answers),
                action=action,
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
