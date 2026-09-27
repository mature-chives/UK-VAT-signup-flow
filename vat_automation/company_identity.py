"""公司身份的保守识别：编号格式校验不等于工商/税务真实性核验。"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from typing import Any

from .countries import normalize_country

USCC_ALPHABET = "0123456789ABCDEFGHJKLMNPQRTUWXY"
USCC_WEIGHTS = (1, 3, 9, 27, 19, 26, 16, 17, 20, 29, 25, 13, 8, 24, 10, 30, 28)


def compact(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value))).upper()


def company_name_key(value: str) -> str:
    # 只忽略大小写、空白和标点；不猜测中英文翻译或 LTD/LIMITED 等别名。
    return "".join(c for c in compact(value) if c.isalnum())


def jurisdiction(value: str) -> str:
    return {"China": "CN", "Hong Kong": "HK"}.get(normalize_country(value), "")


def valid_uscc(value: str) -> bool:
    if len(value) != 18 or any(c not in USCC_ALPHABET for c in value):
        return False
    check = (31 - sum(USCC_ALPHABET.index(c) * w for c, w in zip(value[:17], USCC_WEIGHTS)) % 31) % 31
    return value[-1] == USCC_ALPHABET[check]


def identify_company(values: Mapping[str, str]) -> dict[str, Any]:
    region = jurisdiction(values.get("company_registration_country", ""))
    number = compact(values.get("company_registration_number", ""))
    kind = compact(values.get("company_identifier_type", ""))
    kind = {"USCC": "USCC", "统一社会信用代码": "USCC", "BRN": "BRN", "HK_BRN": "BRN", "商业登记号码": "BRN", "CRN": "CRN", "HK_CRN": "CRN"}.get(kind, "")
    # 有明确编号类型时可确定注册司法辖区；不能从通讯地址国家推断。
    if not values.get("company_registration_country", "").strip():
        region = {"USCC": "CN", "BRN": "HK", "CRN": "HK"}.get(kind, "")
    if region == "CN" and not kind and len(number) == 18:
        kind = "USCC"
    result = {"jurisdiction": region, "type": kind, "number": number, "key": "", "valid": False, "message": ""}
    if not number:
        result["message"] = "未提供统一社会信用代码/BRN，将保留为独立申请，不自动关联公司。"
    elif region == "CN" and kind == "USCC":
        if valid_uscc(number):
            result.update(valid=True, key=f"CN:USCC:{number}")
        else:
            result["message"] = "统一社会信用代码应为 18 位且校验位正确；请核对原件。当前不会自动关联。"
    elif region == "HK" and kind == "BRN":
        # 仅明确选择 BRN 时接受完整商业登记证号的已知格式，不能任意截取前八位。
        certificate = re.fullmatch(r"([0-9]{8})-[0-9]{3}-[0-9]{2}-[0-9]{2}-[0-9]", number)
        if certificate:
            number = certificate[1]
            result["number"] = number
        if re.fullmatch(r"[0-9]{8}", number):
            result.update(valid=True, key=f"HK:BRN:{number}")
        else:
            result["message"] = "香港 BRN 应为 8 位数字；不能把旧公司注册编号或不明证书号码直接截断。当前不会自动关联。"
    else:
        result["message"] = "请核对公司注册地和编号类型；旧香港公司注册编号及其他税号不能直接作为 USCC/BRN 关联。"
    if result["valid"]:
        result["message"] = "编号格式已通过检查；仍需核对原件，这不代表已完成官方真实性核验。"
    return result


def normalized_identity_values(values: Mapping[str, str]) -> dict[str, str]:
    clean = dict(values)
    identity = identify_company(clean)
    if identity["valid"]:
        clean.update(
            company_registration_country={"CN": "China", "HK": "Hong Kong"}[identity["jurisdiction"]],
            company_identifier_type=identity["type"],
            company_registration_number=identity["number"],
        )
    return clean


def identity_signature(values: Mapping[str, str]) -> list[str]:
    identity = identify_company(values)
    return [company_name_key(values.get("business_name", "")), identity["jurisdiction"], identity["type"], identity["number"]]
