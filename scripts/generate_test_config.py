from __future__ import annotations

import argparse
import json
import secrets
import string
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vat_automation.screenshot_flow import screenshot_page_rules


def token(length: int = 6) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def build_config(*, headless: bool) -> dict[str, object]:
    suffix = token()
    birth_date = date.today() - timedelta(days=365 * 32 + 37)
    return {
        "start_url": "https://www.gov.uk/log-in-register-hmrc-online-services",
        "profile_dir": ".browser-profile-test",
        "artifacts_dir": "artifacts-test",
        "browser_channel": "chrome",
        "headless": headless,
        "allow_live_application": False,
        "max_steps": 250,
        "address": {
            "premises": f"Test Room {secrets.randbelow(800) + 100}",
            "street": "88 Synthetic Data Road",
            "city": "Shanghai",
            "postcode": "200000",
            "country": "China",
        },
        "answers": {
            "Why do you want to register the business for VAT?": "It’s selling goods or services and needs or wants to charge VAT to customers",
            "Business name": f"Northstar Test Trading {suffix}",
            "Trading name": f"Northstar Test {suffix}",
            "First name": "Alex",
            "Last name": f"Tester{suffix}",
            "Full name": f"Alex Tester {suffix}",
            "Email address": f"vat-test-{suffix.lower()}@example.com",
            "Telephone number": "07700900123",
            "What does the business do?": "Software testing and quality assurance services",
            "Estimated taxable turnover": "85000",
        },
        "pages": screenshot_page_rules(suffix=suffix, birth_date=birth_date),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="生成不含真实个人信息的 VAT 测试配置")
    parser.add_argument("--output", type=Path, default=Path("vat-config.test.json"))
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()
    args.output.write_text(
        json.dumps(build_config(headless=not args.headed), ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )
    print(f"已生成：{args.output}")


if __name__ == "__main__":
    main()
