"""生成 EORI 流程配置。

默认写出可提交的流程表 `vat-config.eori.flow.json`（只含 doc:/env: 占位符）；
加 `--test` 时写出本地冒烟用的虚构配置 `vat-config.eori.test.json`。
"""

from __future__ import annotations

import argparse
import json
import secrets
import string
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vat_automation.eori_flow import build_eori_flow_config  # noqa: E402


def token(length: int = 6) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def test_values() -> dict[str, str]:
    """测试夹具：doc: 占位符 → 明确带测试标记的虚构值。"""
    suffix = token()
    established = date.today() - timedelta(days=400)
    vat_registered = date.today() - timedelta(days=30)
    return {
        "doc:business_name": f"Northstar Test Trading {suffix}",
        "doc:full_name": f"Alex Tester {suffix}",
        "doc:phone": "07700900123",
        "doc:vat_number": f"GB{secrets.randbelow(900_000_000) + 100_000_000}",
        "doc:postcode|doc:business_postcode": "200000",
        "doc:vat_contact_email|env:HMRC_EMAIL": "env:HMRC_EMAIL",
        "doc:country|env:HMRC_MFA_PHONE_COUNTRY": "env:HMRC_MFA_PHONE_COUNTRY",
        "doc:company-incorporation-date.day": str(established.day),
        "doc:company-incorporation-date.month": str(established.month),
        "doc:company-incorporation-date.year": str(established.year),
        "doc:vat-registration-date.day": str(vat_registered.day),
        "doc:vat-registration-date.month": str(vat_registered.month),
        "doc:vat-registration-date.year": str(vat_registered.year),
    }


def build_test_config(*, headless: bool) -> dict[str, object]:
    config = build_eori_flow_config()
    replacements = test_values()
    config["profile_dir"] = ".browser-profile-eori-test"
    config["artifacts_dir"] = "artifacts-eori-test"
    config["headless"] = headless
    # 虚构资料一律不允许写进真实申请：进入 EORI 数据区前自动停止。
    config["allow_live_application"] = False
    config["answers"] = {
        key: replacements.get(str(value), value)
        for key, value in dict(config["answers"]).items()
    }
    config["address"] = {
        "premises": "Test Room 601, Building 3",
        "street": "88 Synthetic Data Road",
        "city": "Shanghai",
        "postcode": "200000",
        "country": "China",
    }
    pages: list[dict[str, object]] = []
    for rule in config["pages"]:
        item = dict(rule)
        if item.get("address") == "doc:business":
            item.pop("address", None)
        item["answers"] = {
            key: replacements.get(str(value), value)
            for key, value in dict(item.get("answers") or {}).items()
        }
        if "default_answer" in item:
            item["default_answer"] = replacements.get(
                str(item["default_answer"]), item["default_answer"]
            )
        pages.append(item)
    config["pages"] = pages
    return config


def main() -> None:
    parser = argparse.ArgumentParser(description="生成英国 EORI 注册流程配置")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--test", action="store_true", help="生成虚构资料的冒烟配置")
    parser.add_argument("--headed", action="store_true", help="测试配置用有界面浏览器")
    args = parser.parse_args()

    if args.test:
        target = args.output or Path("vat-config.eori.test.json")
        config = build_test_config(headless=not args.headed)
    else:
        target = args.output or Path("vat-config.eori.flow.json")
        config = build_eori_flow_config()

    target.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"已生成：{target}")


if __name__ == "__main__":
    main()
