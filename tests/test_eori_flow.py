import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from vat_automation.config import (
    DEFAULT_IDENTITY_DOCUMENTS,
    build_address_answers,
    is_placeholder_chain,
    load_settings,
)
from vat_automation.document_parser import prepare_document_values
from vat_automation.eori_flow import (
    EORI_REGISTER_PATH,
    EORI_SCREENSHOT_HEADINGS,
    EORI_SCREENSHOT_PATHS,
    EORI_SPECIAL_HANDLED_PATHS,
    EORI_START_URL,
    build_eori_flow_config,
)
from vat_automation.runner import (
    FINAL_SUBMIT_ACTIONS,
    VatAutomation,
)
from vat_automation.web import JobManager, StartRequest


ROOT = Path(__file__).resolve().parents[1]
FLOW_CONFIG = ROOT / "vat-config.eori.flow.json"


def document_bag() -> dict[str, str]:
    return {
        "business_name": "Test Trading Ltd",
        "full_name": "Test Applicant",
        "phone": "13900000000",
        "premises": "Room 601, Building 3",
        "street": "88 Synthetic Data Road",
        "locality": "Linping District",
        "city": "Hangzhou",
        "region": "",
        "postcode": "311100",
        "country": "China",
        "vat_number": "GB123456789",
        "vat_registration_date": "2026-09-01",
        "company_incorporation_date": "2026-05-13",
    }


class EoriFlowConfigTests(unittest.TestCase):
    def test_committed_flow_config_uses_placeholders_not_applicant_literals(self) -> None:
        raw = json.loads(FLOW_CONFIG.read_text(encoding="utf-8"))
        blob = json.dumps(raw, ensure_ascii=False)
        self.assertIn("doc:business_name", blob)
        self.assertIn("doc:vat_contact_email|env:HMRC_EMAIL", blob)
        self.assertEqual(raw["start_url"], EORI_START_URL)
        self.assertEqual(raw["identity_documents_required"], 0)
        # 流程表只能出现占位符或固定选项，不能混入任何客户真值。
        fixed_options = {"No", "Yes", "Rest of the world", "Organisation", "47910"}
        for value in raw["answers"].values():
            self.assertTrue(is_placeholder_chain(str(value)), f"全局答案：{value}")
        for rule in raw["pages"]:
            values = list(dict(rule.get("answers") or {}).values())
            if "default_answer" in rule:
                values.append(rule["default_answer"])
            for value in values:
                text = str(value)
                self.assertTrue(
                    is_placeholder_chain(text) or text in fixed_options,
                    f"流程表出现非占位符取值：{text}",
                )
        self.assertNotIn("@", blob)

    def test_committed_flow_config_matches_generator(self) -> None:
        raw = json.loads(FLOW_CONFIG.read_text(encoding="utf-8"))
        self.assertEqual(raw, build_eori_flow_config())

    def test_address_page_uses_eori_address_format(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            copy = Path(workspace) / "vat-config.eori.flow.json"
            copy.write_text(FLOW_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
            settings = load_settings(copy)
        address_page = next(
            page
            for page in settings.pages
            if "matching/address/third-country-organisation" in page.path_contains
        )
        self.assertEqual(address_page.address_source, "doc:business")
        self.assertEqual(address_page.address_format, "eori")
        self.assertEqual(settings.identity_documents_required, 0)

    def test_eori_address_page_answers_match_screenshot(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            copy = Path(workspace) / "vat-config.eori.flow.json"
            copy.write_text(FLOW_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
            settings = load_settings(copy)
        url = (
            "https://www.tax.service.gov.uk"
            f"{EORI_REGISTER_PATH}/matching/address/third-country-organisation"
        )
        expected = {
            "Address line 1": "Room 601, Building 3",
            "Address line 2 (optional)": "88 Synthetic Data Road",
            "Town or city": "Linping District, Hangzhou",
            "Postal code (optional)": "311100",
            "Country": "China",
        }
        for label, value in expected.items():
            self.assertEqual(
                settings.answer_for(
                    label,
                    url,
                    "Enter your organisation address",
                    document_values=document_bag(),
                ),
                (True, value),
            )
        # 截图里这一栏是空的，配置不应给出值而不是报缺资料。
        self.assertEqual(
            settings.answer_for(
                "Region or state (optional)",
                url,
                "Enter your organisation address",
                document_values=document_bag(),
            ),
            (False, None),
        )

    def test_date_pages_use_document_components(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            copy = Path(workspace) / "vat-config.eori.flow.json"
            copy.write_text(FLOW_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
            settings = load_settings(copy)
        bag = document_bag() | prepare_document_values(document_bag())
        runner = VatAutomation.__new__(VatAutomation)
        runner.document_values = bag
        found, value = settings.answer_for(
            "Day",
            f"https://www.tax.service.gov.uk{EORI_REGISTER_PATH}/date-established",
            "When was the organisation established?",
            document_values=bag,
        )
        self.assertTrue(found)
        self.assertEqual(runner._resolve_value(value), "13")
        found, value = settings.answer_for(
            "Month",
            "https://www.tax.service.gov.uk/customs-registration-services/eori-only/register/vat-details",
            "When did you become VAT registered?",
            document_values=bag,
        )
        self.assertTrue(found)
        self.assertEqual(runner._resolve_value(value), "9")

    def test_sic_default_matches_vat_registration(self) -> None:
        config = build_eori_flow_config()
        page = next(
            rule
            for rule in config["pages"]
            if str(rule["match"].get("path_contains", "")).endswith("/sic-code")
        )
        self.assertEqual(page["default_answer"], "47910")

    def test_every_screenshot_page_has_a_rule(self) -> None:
        config = build_eori_flow_config()
        paths = [
            str(rule["match"].get("path_contains", ""))
            for rule in config["pages"]
        ]
        headings = [
            str(rule["match"].get("heading_contains", ""))
            for rule in config["pages"]
        ]
        for page in EORI_SCREENSHOT_PATHS:
            covered = any(item and item in page for item in paths)
            covered = covered or any(item in page for item in EORI_SPECIAL_HANDLED_PATHS)
            self.assertTrue(covered, f"缺少流程规则：{page}")
        for heading in EORI_SCREENSHOT_HEADINGS:
            covered = any(item and item in heading for item in headings)
            covered = covered or any(
                item in heading for item in EORI_SPECIAL_HANDLED_PATHS
            )
            self.assertTrue(covered, f"缺少标题规则：{heading}")

    def test_coverage_script_reports_full_coverage(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "check_flow_coverage.py"),
                "--flow",
                "eori",
            ],
            capture_output=True,
            text=True,
            cwd=ROOT,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["covered"], report["screenshot_pages"])
        self.assertEqual(report["uncovered"], [])


class EoriRunnerGuardTests(unittest.TestCase):
    def test_eori_register_is_treated_as_application_page(self) -> None:
        for path in EORI_SCREENSHOT_PATHS:
            url = f"https://www.tax.service.gov.uk{path}"
            self.assertTrue(
                VatAutomation._is_application_page(url), f"未拦住：{url}"
            )
        self.assertFalse(
            VatAutomation._is_application_page("https://www.gov.uk/eori/apply-for-eori")
        )

    def test_eori_review_page_is_final_page(self) -> None:
        runner = VatAutomation.__new__(VatAutomation)
        self.assertTrue(
            runner._is_final_page(
                "https://www.tax.service.gov.uk"
                f"{EORI_REGISTER_PATH}/review-details",
                "Check your answers",
            )
        )

    def test_final_submit_actions_keep_vat_button_first(self) -> None:
        self.assertEqual(FINAL_SUBMIT_ACTIONS[0], "Confirm and submit")
        self.assertIn("Accept and submit", FINAL_SUBMIT_ACTIONS)


class EoriAddressFormatTests(unittest.TestCase):
    def test_eori_address_format_maps_components(self) -> None:
        answers = build_address_answers(
            {
                "premises": "Room 601, Building 3",
                "street": "88 Synthetic Data Road",
                "locality": "Linping District",
                "city": "Hangzhou",
                "region": "Zhejiang",
                "postcode": "311100",
                "country": "China",
            },
            "eori",
        )
        self.assertEqual(answers["Address line 1"], "Room 601, Building 3")
        self.assertEqual(answers["Address line 2 (optional)"], "88 Synthetic Data Road")
        self.assertEqual(answers["Town or city"], "Linping District, Hangzhou")
        self.assertEqual(answers["Region or state (optional)"], "Zhejiang")
        self.assertEqual(answers["Postal code (optional)"], "311100")
        self.assertEqual(answers["Country"], "China")

    def test_eori_address_that_needs_three_lines_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_address_answers(
                {
                    "premises": "Room 601 Building 3 Xingqi Road",
                    "street": "No. 528 Donghu Street Linping District Hangzhou",
                    "city": "Hangzhou",
                    "country": "China",
                },
                "eori",
            )

    def test_unknown_address_format_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            path = Path(workspace) / "vat-config.json"
            path.write_text(
                json.dumps(
                    {
                        "pages": [
                            {
                                "match": {"path_contains": "/x"},
                                "address": {"premises": "A"},
                                "address_format": "hmrc-1996",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "地址格式"):
                load_settings(path)


class IdentityDocumentsRequirementTests(unittest.TestCase):
    def _write(self, workspace: str, payload: dict[str, object]) -> Path:
        path = Path(workspace) / "vat-config.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_default_is_three_documents(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            settings = load_settings(self._write(workspace, {}))
        self.assertEqual(
            settings.identity_documents_required, DEFAULT_IDENTITY_DOCUMENTS
        )

    def test_invalid_requirement_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            with self.assertRaisesRegex(ValueError, "identity_documents_required"):
                load_settings(
                    self._write(workspace, {"identity_documents_required": "三份"})
                )
            with self.assertRaisesRegex(ValueError, "identity_documents_required"):
                load_settings(
                    self._write(workspace, {"identity_documents_required": -1})
                )

    def test_eori_job_starts_without_identity_documents(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            path = self._write(workspace, build_eori_flow_config())
            manager = JobManager(path)
            self.assertEqual(manager.snapshot("alice")["identity_required"], 0)
            with manager._lock:
                manager._busy_with = "bob"
            # 身份证明检查应被跳过，因此这里报的是“别人正在跑”，不是缺身份证明。
            with self.assertRaisesRegex(RuntimeError, "bob"):
                manager.start("alice", StartRequest(extracted_confirmed=True))

    def test_vat_job_still_requires_three_documents(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            path = self._write(workspace, {})
            manager = JobManager(path)
            with self.assertRaisesRegex(ValueError, "身份证明"):
                manager.start("alice", StartRequest(extracted_confirmed=True))


class DocumentDateFieldTests(unittest.TestCase):
    def test_company_and_vat_dates_are_expanded(self) -> None:
        bag = prepare_document_values(
            {
                "company_incorporation_date": "2026-05-13",
                "vat_registration_date": "01/09/2026",
            }
        )
        self.assertEqual(bag["company-incorporation-date.day"], "13")
        self.assertEqual(bag["company-incorporation-date.month"], "5")
        self.assertEqual(bag["company-incorporation-date.year"], "2026")
        self.assertEqual(bag["vat-registration-date.day"], "1")
        self.assertEqual(bag["vat-registration-date.month"], "9")
        self.assertEqual(bag["vat-registration-date.year"], "2026")

    def test_bad_date_is_reported(self) -> None:
        with self.assertRaisesRegex(ValueError, "vat-registration-date"):
            prepare_document_values({"vat_registration_date": "下个月"})


if __name__ == "__main__":
    unittest.main()
