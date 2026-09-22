"""EORI 流程离线冒烟：用本地模拟页面跑一遍 vat-config.eori.flow.json。

不发任何请求到 HMRC：脚本在本机起一个 HTTP 服务，按 HMRC 的路径和标签渲染
模拟页面，让真正的 runner 依次填写、点 Continue，并把每页提交的值记下来核对。
最后停在 review-details（最终核对），验证程序不会自动提交。
"""

from __future__ import annotations

import asyncio
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.async_api import async_playwright  # noqa: E402

from vat_automation.config import load_settings  # noqa: E402
from vat_automation.document_parser import prepare_document_values  # noqa: E402
from vat_automation.runner import AutomationStopped, VatAutomation  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
FLOW_CONFIG = ROOT / "vat-config.eori.flow.json"
EORI_ROOT = "/customs-registration-services/eori-only/register"
EMAIL = "eori-smoke@example.com"

DOCUMENT_BAG = {
    "business_name": "Northstar Test Trading AB12CD",
    "full_name": "Alex Tester",
    "phone": "07700900123",
    "premises": "Room 601, Building 3",
    "street": "88 Synthetic Data Road",
    "locality": "Linping District",
    "city": "Hangzhou",
    "postcode": "311100",
    "country": "China",
    "vat_number": "GB123456789",
    "vat_registration_date": "2026-09-01",
    "company_incorporation_date": "2026-05-13",
    "vat_contact_email": EMAIL,
}


def text_field(name: str, label: str) -> str:
    return (
        f'<div class="govuk-form-group"><label class="govuk-label" for="{name}">'
        f"{label}</label>"
        f'<input class="govuk-input" id="{name}" name="{name}" type="text"></div>'
    )


def radio_group(name: str, legend: str, options: list[tuple[str, str]]) -> str:
    items = "".join(
        f'<div class="govuk-radios__item">'
        f'<input class="govuk-radios__input" type="radio" name="{name}" '
        f'id="{name}-{index}" value="{value}">'
        f'<label class="govuk-label govuk-radios__label" for="{name}-{index}">'
        f"{label}</label></div>"
        for index, (value, label) in enumerate(options)
    )
    return (
        f'<div class="govuk-form-group"><fieldset class="govuk-fieldset">'
        f'<legend class="govuk-fieldset__legend">{legend}</legend>'
        f'<div class="govuk-radios">{items}</div></fieldset></div>'
    )


def date_fields(prefix: str) -> str:
    return "".join(
        text_field(f"{prefix}-{part}", part.capitalize())
        for part in ("day", "month", "year")
    )


# (路径后缀, 页面标题, 表单内容)
FLOW_PAGES: list[tuple[str, str, str]] = [
    (
        "/matching/vat-group",
        "Is your organisation part of a VAT group in the UK?",
        radio_group("vat-group", "Is your organisation part of a VAT group in the UK?",
                    [("yes", "Yes"), ("no", "No")]),
    ),
    (
        "/matching/email-notifications",
        "What email address can we use for customs notifications?",
        text_field(
            "email-address",
            "What email address can we use for customs notifications?",
        ),
    ),
    (
        "/matching/confirm-email",
        f"Is {EMAIL} the mail address you want to use?",
        radio_group("confirm-email", f"Is {EMAIL} the mail address you want to use?",
                    [("yes", "Yes"),
                     ("no", "No, I need to change this email address")]),
    ),
    (
        "/matching/user-location",
        "Where is your organisation established?",
        radio_group(
            "user-location",
            "Where is your organisation established?",
            [
                ("united-kingdom", "United Kingdom"),
                ("isle-of-man", "The Isle of Man"),
                ("channel-islands", "The Channel Islands"),
                ("rest-of-the-world", "Rest of the world"),
            ],
        ),
    ),
    (
        "/matching/organisation-type",
        "What do you want to apply as?",
        radio_group(
            "organisation-type",
            "What do you want to apply as?",
            [("organisation", "Organisation"), ("sole-trader", "Sole trader"),
             ("individual", "Individual")],
        ),
    ),
    (
        "/matching/name/third-country-organisation",
        "What is your registered company name?",
        text_field("name", "What is your registered company name?"),
    ),
    (
        "/matching/utr/third-country-organisation",
        "Does your organisation have a Corporation Tax UTR issued in the UK?",
        radio_group(
            "utr",
            "Does your organisation have a Corporation Tax Unique Taxpayer "
            "Reference (UTR) issued in the UK?",
            [("yes", "Yes"), ("no", "No")],
        ),
    ),
    (
        "/matching/address/third-country-organisation",
        "Enter your organisation address",
        text_field("address-line-1", "Address line 1")
        + text_field("address-line-2", "Address line 2 (optional)")
        + text_field("town-or-city", "Town or city")
        + text_field("region-or-state", "Region or state (optional)")
        + text_field("postal-code", "Postal code (optional)")
        + text_field("country", "Country"),
    ),
    (
        "/date-established",
        "When was the organisation established?",
        date_fields("established-date"),
    ),
    (
        "/sic-code",
        "Your Standard Industrial Classification (SIC) code",
        text_field("sic", "Enter a SIC code"),
    ),
    (
        "/disclose-personal-details-consent",
        "Do you consent to show the organisation's name and address on the "
        "'Check an EORI number' service?",
        radio_group(
            "disclose-consent",
            "Do you consent to show the organisation's name and address on the "
            "'Check an EORI number' service?",
            [("yes", "Yes"), ("no", "No")],
        ),
    ),
    (
        "/vat-registered-uk",
        "Is your organisation VAT registered in the UK?",
        radio_group("vat-registered", "Is your organisation VAT registered in the UK?",
                    [("yes", "Yes"), ("no", "No")]),
    ),
    (
        "/vat-details",
        "Your UK VAT details",
        text_field("vat-number", "What is your VAT registration number?")
        + text_field(
            "vat-postcode",
            "What is the postcode where your organisation is registered for VAT?",
        ),
    ),
    (
        "/vat-registered-date",
        "When did you become VAT registered?",
        date_fields("vat-registered-date"),
    ),
    (
        "/contact-details",
        "EORI number application contact details",
        text_field("contact-name", "Full name")
        + text_field("contact-telephone", "Telephone"),
    ),
    (
        "/address-for-information",
        "Do you want us to use this address to send you information about your "
        "EORI number application?",
        radio_group(
            "address-for-information",
            "Do you want us to use this address to send you information about "
            "your EORI number application?",
            [("yes", "Yes"),
             ("no", "No, I want to enter the address manually")],
        ),
    ),
    (
        "/review-details",
        "Check your answers",
        "",
    ),
]


def page_paths() -> list[str]:
    return [f"{EORI_ROOT}{suffix}" for suffix, _, _ in FLOW_PAGES]


def page_html(path: str, heading: str, body: str, next_path: str | None) -> str:
    if next_path is None:
        return (
            f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            f"<title>{heading}</title></head><body><main><h1>{heading}</h1>"
            "<dl class='govuk-summary-list'></dl></main></body></html>"
        )
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        f"<title>{heading}</title></head><body><main><form method='get' "
        f"action='/submit'><input type='hidden' name='__page' value='{path}'>"
        f"<input type='hidden' name='__next' value='{next_path}'>"
        f"<h1>{heading}</h1>{body}"
        "<button class='govuk-button' data-module='govuk-button'>Continue</button>"
        "</form></main></body></html>"
    )


class FixtureHandler(BaseHTTPRequestHandler):
    """按 HMRC 路径返回模拟页面，并记录每页提交的值。"""

    pages: dict[str, str] = {}
    records: dict[str, dict[str, str]] = {}

    def log_message(self, *args: object) -> None:  # 静默，保持输出干净
        return

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的约定
        parsed = urlparse(self.path)
        if parsed.path == "/submit":
            query = {
                key: values[0] for key, values in parse_qs(parsed.query).items()
            }
            page = query.pop("__page", "")
            next_path = query.pop("__next", "")
            if page:
                self.records[page] = query
            self.send_response(302)
            self.send_header("Location", next_path or "/")
            self.end_headers()
            return
        html = self.pages.get(parsed.path)
        if html is None:
            self.send_error(404, "unknown fixture page")
            return
        payload = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def build_pages() -> dict[str, str]:
    paths = page_paths()
    pages: dict[str, str] = {}
    for index, (suffix, heading, body) in enumerate(FLOW_PAGES):
        path = f"{EORI_ROOT}{suffix}"
        next_path = paths[index + 1] if index + 1 < len(paths) else None
        pages[path] = page_html(path, heading, body, next_path)
    return pages


def check(records: dict[str, dict[str, str]]) -> list[str]:
    """核对每页提交的值，返回不一致的说明列表。"""
    expected = {
        "/matching/vat-group": {"vat-group": "no"},
        "/matching/email-notifications": {"email-address": EMAIL},
        "/matching/confirm-email": {"confirm-email": "yes"},
        "/matching/user-location": {"user-location": "rest-of-the-world"},
        "/matching/organisation-type": {"organisation-type": "organisation"},
        "/matching/name/third-country-organisation": {
            "name": DOCUMENT_BAG["business_name"]
        },
        "/matching/utr/third-country-organisation": {"utr": "no"},
        "/matching/address/third-country-organisation": {
            "address-line-1": DOCUMENT_BAG["premises"],
            "address-line-2": DOCUMENT_BAG["street"],
            "town-or-city": "Linping District, Hangzhou",
            "postal-code": DOCUMENT_BAG["postcode"],
            "country": "China",
        },
        "/date-established": {
            "established-date-day": "13",
            "established-date-month": "5",
            "established-date-year": "2026",
        },
        "/sic-code": {"sic": "47910"},
        "/disclose-personal-details-consent": {"disclose-consent": "no"},
        "/vat-registered-uk": {"vat-registered": "yes"},
        "/vat-details": {
            "vat-number": DOCUMENT_BAG["vat_number"],
            "vat-postcode": DOCUMENT_BAG["postcode"],
        },
        "/vat-registered-date": {
            "vat-registered-date-day": "1",
            "vat-registered-date-month": "9",
            "vat-registered-date-year": "2026",
        },
        "/contact-details": {
            "contact-name": DOCUMENT_BAG["full_name"],
            "contact-telephone": DOCUMENT_BAG["phone"],
        },
        "/address-for-information": {"address-for-information": "yes"},
    }
    problems: list[str] = []
    for suffix, fields in expected.items():
        path = f"{EORI_ROOT}{suffix}"
        actual = records.get(path)
        if actual is None:
            problems.append(f"{suffix}：没有提交记录")
            continue
        for key, wanted in fields.items():
            if actual.get(key) != wanted:
                problems.append(f"{suffix}：{key} 期望 {wanted!r}，实际 {actual.get(key)!r}")
    return problems


async def run() -> int:
    FixtureHandler.pages = build_pages()
    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    settings = load_settings(FLOW_CONFIG)
    settings.start_url = f"http://127.0.0.1:{port}{page_paths()[0]}"
    settings.profile_dir = Path("/private/tmp/eori-smoke-profile")
    settings.artifacts_dir = Path("/private/tmp/eori-smoke-artifacts")
    settings.headless = True
    # 全部是本地 fixture，没有真实客户数据；仍会停在 review-details 等待人工确认。
    settings.allow_live_application = True
    runner = VatAutomation(
        settings,
        interactive=False,
        credentials={"HMRC_EMAIL": EMAIL},
        document_values=prepare_document_values(DOCUMENT_BAG),
    )

    stopped = ""
    try:
        await runner.run()
    except AutomationStopped as exc:
        stopped = str(exc)
    finally:
        server.shutdown()

    problems = check(FixtureHandler.records)
    print(f"模拟页面：{len(FixtureHandler.pages)} 个，已填写：{len(FixtureHandler.records)} 个")
    print(f"停止原因：{stopped}")
    if problems:
        print("填写结果不一致：")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    if "最终复核" not in stopped:
        print(f"未按预期停在最终核对页，停止原因：{stopped!r}")
        return 1
    print(f"{len(FixtureHandler.records)} 个页面的填写值与配置一致，"
          "并按要求停在最终核对页。")
    return 0


def main() -> int:
    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
