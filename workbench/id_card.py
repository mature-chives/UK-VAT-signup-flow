from __future__ import annotations

import re
from typing import Any

ID_CARD_FIELD_LABELS = {
    "full_name": "姓名",
    "sex": "性别",
    "ethnicity": "民族",
    "birth_date": "出生",
    "address": "住址",
    "identity_document_number": "公民身份号码",
    "issuing_authority": "签发机关",
    "valid_period": "有效期限",
}

_SPACE = re.compile(r"[\s:：_·]+")
_ID_NUMBER = re.compile(r"\d{17}[\dXx]")
_BIRTH = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")


def _block_text(block: Any) -> str:
    if isinstance(block, dict):
        label = str(block.get("block_label") or block.get("label") or "text")
        if label and label != "text":
            return ""
        return str(block.get("block_content") or block.get("content") or "").strip()
    label = str(getattr(block, "label", None) or getattr(block, "block_label", "text") or "text")
    if label and label != "text":
        return ""
    return str(getattr(block, "content", None) or getattr(block, "block_content", "") or "").strip()


def _collect_text(source: dict[str, Any] | list[Any] | str) -> str:
    if isinstance(source, str):
        return source
    if isinstance(source, list):
        return "\n".join(str(item).strip() for item in source if str(item).strip())
    blocks = source.get("parsing_res_list") or []
    texts: list[str] = []
    ordered = sorted(
        enumerate(blocks),
        key=lambda pair: (
            pair[1].get("block_order") is None if isinstance(pair[1], dict) else False,
            pair[1].get("block_order") if isinstance(pair[1], dict) else pair[0],
            pair[0],
        ),
    )
    for _, block in ordered:
        text = _block_text(block)
        if text:
            texts.append(text)
    return "\n".join(texts)


def _compact(text: str) -> str:
    return _SPACE.sub("", text)


def _between(compact: str, start: str, *ends: str) -> str:
    pattern = start + r"(.+?)"
    if ends:
        pattern += "(?=" + "|".join(re.escape(item) for item in ends) + "|$)"
    match = re.search(pattern, compact)
    return match.group(1).strip() if match else ""


def extract_id_card_fields(source: dict[str, Any] | list[Any] | str) -> dict[str, str]:
    """从 PaddleOCRVL 的 parsing_res_list / 纯文本里抽出身份证字段。"""
    compact = _compact(_collect_text(source))
    birth_match = _BIRTH.search(compact)
    number_match = _ID_NUMBER.search(compact)
    birth = ""
    if birth_match:
        birth = f"{birth_match.group(1)}年{int(birth_match.group(2))}月{int(birth_match.group(3))}日"
    fields = {
        "full_name": _between(compact, "姓名", "性别", "民族", "出生"),
        "sex": _between(compact, "性别", "民族", "出生")[:1],
        "ethnicity": _between(compact, "民族", "出生", "住址"),
        "birth_date": birth,
        "address": _between(compact, "住址", "公民身份号码", "签发机关", "有效期限"),
        "identity_document_number": (number_match.group(0).upper() if number_match else ""),
        "issuing_authority": _between(compact, "签发机关", "有效期限", "公民身份号码"),
        "valid_period": _between(compact, "有效期限", "签发机关", "公民身份号码"),
    }
    if fields["sex"] not in {"男", "女"}:
        fields["sex"] = ""
    return {key: value for key, value in fields.items() if value}


def merge_id_card_fields(*batches: dict[str, str]) -> dict[str, str]:
    merged: dict[str, str] = {}
    for batch in batches:
        for key, value in batch.items():
            current = merged.get(key, "")
            if value and (not current or len(value) > len(current)):
                merged[key] = value
    return merged
