import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from vat_automation.config import (
    PageRule,
    Settings,
    build_address_answers,
    build_international_address_answers,
    load_settings,
    normalize,
    resolve_dynamic_value,
    uk_next_month_first,
    vat_return_stagger_for_registration_month,
)
from vat_automation.runner import (
    SAFE_ACTIONS,
    AutomationStopped,
    Control,
    VatAutomation,
    _safe_url,
)
from vat_automation.screenshot_flow import SCREENSHOT_PATHS, screenshot_page_rules


class ConfigTests(unittest.TestCase):
    def test_normalize_ignores_case_punctuation_and_curly_apostrophe(self) -> None:
        self.assertEqual(
            normalize("It’s selling goods or services"),
            normalize("IT'S SELLING GOODS OR SERVICES"),
        )
        self.assertEqual(
            normalize("I do not have the company’s UTR number"),
            normalize("I do not have the company's UTR number"),
        )

    def test_page_answers_override_global_answers(self) -> None:
        settings = Settings(
            start_url="https://example.test",
            profile_dir=Path(".browser-profile"),
            artifacts_dir=Path("artifacts"),
            answers={"Country": "United Kingdom"},
            pages=[
                PageRule(path_contains="/international", answers={"Country": "China"})
            ],
        )
        found, answer = settings.answer_for(
            "Country", "https://example.test/international", "Address"
        )
        self.assertTrue(found)
        self.assertEqual(answer, "China")

    def test_business_name_alias_matches_official_name_question(self) -> None:
        settings = Settings(
            start_url="https://example.test",
            profile_dir=Path(".browser-profile"),
            artifacts_dir=Path("artifacts"),
            answers={"Business name": "Example Trading Ltd"},
            pages=[],
        )
        self.assertEqual(
            settings.answer_for(
                "What is the official name of the business?",
                "https://example.test/register-for-vat/business-name",
                "What is the official name of the business?",
            ),
            (True, "Example Trading Ltd"),
        )

    def test_business_description_alias_matches_goods_services_prompt(self) -> None:
        settings = Settings(
            start_url="https://example.test",
            profile_dir=Path(".browser-profile"),
            artifacts_dir=Path("artifacts"),
            answers={
                "What does the business do?": "Software development services"
            },
            pages=[],
        )
        self.assertEqual(
            settings.answer_for(
                "Describe the type of goods or services the business sells.",
                "https://example.test/register-for-vat/business-description",
                "What does the business do?",
            ),
            (True, "Software development services"),
        )

    def test_uk_next_month_first_handles_normal_month_and_year_rollover(self) -> None:
        self.assertEqual(
            uk_next_month_first(date(2026, 7, 24)), date(2026, 8, 1)
        )
        self.assertEqual(
            uk_next_month_first(date(2026, 12, 31)), date(2027, 1, 1)
        )

    def test_dynamic_uk_next_month_date_components(self) -> None:
        target = date(2027, 1, 1)
        self.assertEqual(
            resolve_dynamic_value("date:uk-next-month-first.day", target), "1"
        )
        self.assertEqual(
            resolve_dynamic_value("date:uk-next-month-first.month", target), "1"
        )
        self.assertEqual(
            resolve_dynamic_value("date:uk-next-month-first.year", target), "2027"
        )
        self.assertEqual(
            resolve_dynamic_value(
                "vat-return-stagger:uk-next-month-first", target
            ),
            "mar",
        )

    def test_vat_return_stagger_covers_all_registration_months(self) -> None:
        expected = {
            1: "mar",
            2: "jan",
            3: "feb",
            4: "mar",
            5: "jan",
            6: "feb",
            7: "mar",
            8: "jan",
            9: "feb",
            10: "mar",
            11: "jan",
            12: "feb",
        }
        self.assertEqual(
            {
                month: vat_return_stagger_for_registration_month(month)
                for month in range(1, 13)
            },
            expected,
        )

    def test_fixed_turnover_and_sic_rules_override_config_values(self) -> None:
        settings = load_settings(Path("vat-config.test.json"))
        cases = (
            ("/register-for-vat/standard-rate-turnover", "standardRateSupplies", "10000"),
            ("/register-for-vat/reduced-rate-turnover", "reducedRateSupplies", "0"),
            ("/register-for-vat/zero-rated-turnover", "zeroRatedSupplies", "0"),
            ("/search-standard-industry-classification-codes", "sicSearch", "47910"),
        )
        for path, label, expected in cases:
            self.assertEqual(
                settings.answer_for(label, f"https://example.test{path}", "Question"),
                (True, expected),
            )
        self.assertEqual(
            settings.action_for(
                "https://example.test/search-standard-industry-classification-codes",
                "Search",
            ),
            "Search",
        )

    def test_structured_address_is_split_into_hmrc_fields(self) -> None:
        answers = build_address_answers(
            {
                "premises": "Room 601,602,603, Building 3",
                "street": "No. 528 Xingqi Road, Donghu Street",
                "locality": "Linping District",
                "city": "Hangzhou City",
                "region": "Zhejiang",
                "postcode": "311100",
                "country": "China",
            }
        )
        self.assertEqual(
            answers,
            {
                "Address line 1": "Room 601,602,603, Building 3",
                "Address line 2": "No. 528 Xingqi Road, Donghu Street",
                "Town or city": "Linping, Hangzhou, Zhejiang",
                "Postcode": "311100",
                "Country": "China",
            },
        )
        self.assertTrue(
            all(
                len(answers[key]) <= 35
                for key in ("Address line 1", "Address line 2", "Town or city")
            )
        )

    def test_structured_address_rejects_unsafe_overflow(self) -> None:
        with self.assertRaisesRegex(ValueError, "Address line 1 超过 35"):
            build_address_answers({"premises": "X" * 36})

    def test_international_address_maps_each_english_component_to_own_line(self) -> None:
        answers = build_international_address_answers(
            {
                "premises": "Room 8, Building 2",
                "street": "No. 9 Test Road",
                "locality": "Demo District",
                "city": "Sample City",
                "region": "Zhejiang",
                "postcode": "310000",
                "country": "China",
            }
        )
        self.assertEqual(answers["Address line 1"], "Room 8, Building 2")
        self.assertEqual(answers["Address line 3 (optional)"], "Demo District")
        self.assertEqual(answers["Address line 5 (optional)"], "Zhejiang")
        self.assertEqual(answers["Postcode (optional)"], "310000")

    def test_page_rule_requires_all_supplied_matchers(self) -> None:
        rule = PageRule(path_contains="/vat", heading_contains="Address")
        self.assertTrue(rule.matches("https://example.test/vat", "Home address"))
        self.assertFalse(rule.matches("https://example.test/vat", "Business name"))

    def test_specific_action_overrides_earlier_generic_path(self) -> None:
        settings = Settings(
            start_url="https://example.test",
            profile_dir=Path(".browser-profile"),
            artifacts_dir=Path("artifacts"),
            answers={},
            pages=[
                PageRule(
                    path_contains="/identify-your-sole-trader-business/",
                    action="continue",
                ),
                PageRule(
                    path_contains="/national-insurance-number",
                    action="skip:I do not have a National Insurance number",
                ),
            ],
        )
        self.assertEqual(
            settings.action_for(
                "https://example.test/identify-your-sole-trader-business/"
                "person-id/national-insurance-number",
                "Alex’s National Insurance number?",
            ),
            "skip:I do not have a National Insurance number",
        )

    def test_page_default_answer_is_used_when_no_label_matches(self) -> None:
        settings = Settings(
            start_url="https://example.test",
            profile_dir=Path(".browser-profile"),
            artifacts_dir=Path("artifacts"),
            answers={},
            pages=[PageRule(path_contains="/question", default_answer="false")],
        )
        self.assertEqual(
            settings.answer_for(
                "A changed question label", "https://example.test/question", "Question"
            ),
            (True, "false"),
        )

    def test_screenshot_flow_rule_order_keeps_specific_actions(self) -> None:
        raw = screenshot_page_rules(
            suffix="ABC123",
            birth_date=date(1990, 1, 2),
        )
        pages = [
            PageRule(
                path_contains=item["match"].get("path_contains", ""),
                heading_contains=item["match"].get("heading_contains", ""),
                answers=item.get("answers", {}),
                default_answer=item.get("default_answer"),
                action=item.get("action", "continue"),
            )
            for item in raw
        ]
        settings = Settings(
            start_url="https://example.test",
            profile_dir=Path(".browser-profile"),
            artifacts_dir=Path("artifacts"),
            answers={},
            pages=pages,
        )
        self.assertEqual(
            settings.action_for(
                "https://example.test/identify-your-overseas-business/id/overseas-identifier",
                "Your overseas tax identifier",
            ),
            "continue",
        )
        self.assertEqual(
            settings.answer_for(
                "Question",
                "https://example.test/business-account/add-tax/vat/do-you-have-a-vat-number",
                "Do you have a VAT number?",
            ),
            (True, "No"),
        )
        self.assertEqual(
            settings.action_for(
                "https://example.test/register-for-vat/honesty-declaration",
                "Declaration",
            ),
            "continue",
        )
        self.assertGreaterEqual(len(SCREENSHOT_PATHS), 80)

    def test_final_review_is_always_detected(self) -> None:
        settings = Settings(
            start_url="https://example.test",
            profile_dir=Path(".browser-profile"),
            artifacts_dir=Path("artifacts"),
            answers={},
            pages=[],
        )
        runner = VatAutomation.__new__(VatAutomation)
        runner.settings = settings
        self.assertTrue(
            runner._is_final_page(
                "https://example.test/register-for-vat/check-your-answers",
                "Check your answers",
            )
        )
        self.assertNotIn("Submit application", SAFE_ACTIONS)
        self.assertIn("Continue to register for VAT", SAFE_ACTIONS)

    def test_intermediate_identity_review_is_not_final(self) -> None:
        settings = Settings(
            start_url="https://example.test",
            profile_dir=Path(".browser-profile"),
            artifacts_dir=Path("artifacts"),
            answers={},
            pages=[],
        )
        runner = VatAutomation.__new__(VatAutomation)
        runner.settings = settings
        self.assertFalse(
            runner._is_final_page(
                "https://example.test/identify-your-overseas-business/id/check-your-answers-business",
                "Check your answers",
            )
        )
        self.assertFalse(
            runner._is_final_page(
                "https://example.test/register-for-vat/honesty-declaration",
                "Declaration",
            )
        )

    def test_live_application_warnings_detect_placeholder_values(self) -> None:
        settings = Settings(
            start_url="https://example.test",
            profile_dir=Path(".browser-profile"),
            artifacts_dir=Path("artifacts"),
            answers={
                "Email address": "person@example.com",
                "Telephone number": "07700900123",
                "Business name": "Northstar Trading",
            },
            pages=[],
        )
        warnings = settings.live_application_warnings()
        self.assertIn("检测到示例邮箱域名 example.com", warnings)
        self.assertIn("检测到英国保留测试手机号 07700 900xxx", warnings)
        self.assertIn("检测到 Northstar 测试标记", warnings)

    def test_live_application_warnings_include_environment_values(self) -> None:
        settings = Settings(
            start_url="https://example.test",
            profile_dir=Path(".browser-profile"),
            artifacts_dir=Path("artifacts"),
            answers={},
            pages=[],
        )
        warnings = settings.live_application_warnings(
            {"HMRC_EMAIL": "real-person@example.com"}
        )
        self.assertIn("检测到示例邮箱域名 example.com", warnings)

    def test_safe_url_redacts_auth_token_and_query(self) -> None:
        self.assertEqual(
            _safe_url(
                "https://www.tax.service.gov.uk/sign-in/identity/sign-in/secret?code=1"
            ),
            "https://www.tax.service.gov.uk/sign-in/identity/sign-in/[redacted]",
        )

    def test_government_gateway_host_is_auth_page(self) -> None:
        self.assertTrue(
            VatAutomation._is_auth_page(
                "https://www.access.service.gov.uk/login/signin/creds"
            )
        )

    def test_business_account_is_not_auth_page(self) -> None:
        self.assertFalse(
            VatAutomation._is_auth_page(
                "https://www.tax.service.gov.uk/business-account"
            )
        )
        self.assertTrue(
            VatAutomation._is_auth_page(
                "https://www.tax.service.gov.uk/"
                "sign-in-to-hmrc-online-services/identity/sign-in/token"
            )
        )

    def test_live_application_paths_are_detected(self) -> None:
        self.assertTrue(
            VatAutomation._is_application_page(
                "https://www.tax.service.gov.uk/register-for-vat/business-name"
            )
        )
        self.assertFalse(
            VatAutomation._is_application_page(
                "https://www.access.service.gov.uk/registration/email"
            )
        )

    def test_safe_url_redacts_identity_flow_token(self) -> None:
        self.assertEqual(
            _safe_url(
                "https://www.tax.service.gov.uk/service/identity/tax-agent/secret"
            ),
            "https://www.tax.service.gov.uk/service/identity/tax-agent/[redacted]",
        )

    def test_auth_resume_state_keeps_current_url(self) -> None:
        import asyncio
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            artifacts = Path(directory)
            settings = Settings(
                start_url="https://www.gov.uk/start",
                profile_dir=Path(".browser-profile"),
                artifacts_dir=artifacts,
                answers={},
                pages=[],
            )
            runner = VatAutomation(settings, interactive=False)
            current = (
                "https://www.access.service.gov.uk/"
                "multi-factor/enter-mobile-number/session-token"
            )
            asyncio.run(runner._save_state(current))
            self.assertEqual(runner._resume_url(), current)
            self.assertEqual((artifacts / "state.json").stat().st_mode & 0o777, 0o600)

    def test_missing_environment_value_is_rejected(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(KeyError, "HMRC_TEST_MISSING"):
                VatAutomation._resolve_value("env:HMRC_TEST_MISSING")

    def test_radio_can_match_raw_value(self) -> None:
        control = Control(
            kind="radio",
            name="choice",
            element_id="yes",
            label="Choose one",
            option_label="Yes",
            value="true",
            required=True,
            visible=True,
            combobox=False,
        )
        self.assertTrue(VatAutomation._radio_option_matches(control, "true"))
        self.assertTrue(VatAutomation._radio_option_matches(control, "Yes"))
        self.assertFalse(VatAutomation._radio_option_matches(control, "false"))

    def test_numeric_element_id_uses_valid_attribute_selector(self) -> None:
        class FakePage:
            selector = ""

            def locator(self, selector: str) -> str:
                self.selector = selector
                return selector

        page = FakePage()
        control = Control(
            kind="radio",
            name="choice",
            element_id="55",
            label="Choose one",
            option_label="Yes",
            value="true",
            required=True,
            visible=True,
            combobox=False,
        )
        self.assertEqual(VatAutomation._locator(page, control), '[id="55"]')

    def test_honesty_declaration_is_accepted_without_terminal_confirmation(self) -> None:
        import asyncio

        class HonestyRunner(VatAutomation):
            clicked = False

            async def _click_named_action(self, _page: object, name: str) -> bool:
                self.clicked = name == "Accept and continue"
                return self.clicked

            async def _audit(self, *_args: object, **_kwargs: object) -> None:
                return None

        settings = Settings(
            start_url="https://example.test",
            profile_dir=Path(".browser-profile"),
            artifacts_dir=Path("artifacts"),
            answers={},
            pages=[],
        )
        runner = HonestyRunner(settings, interactive=False)
        page = type(
            "FakePage",
            (),
            {"url": "https://example.test/register-for-vat/honesty-declaration"},
        )()
        with patch.dict("os.environ", {}, clear=True):
            asyncio.run(runner._handle_honesty_declaration(page, "Declaration"))
        self.assertTrue(runner.clicked)

    def test_verification_code_can_come_from_web_provider(self) -> None:
        import asyncio

        class CodeInput:
            value = ""

            async def fill(self, value: str) -> None:
                self.value = value

        class WebRunner(VatAutomation):
            clicked = False

            async def _heading(self, _page: object) -> str:
                return "Enter the access code"

            async def _audit(self, *_args: object, **_kwargs: object) -> None:
                return None

            async def _click_auth_action(
                self, _page: object, _names: tuple[str, ...]
            ) -> bool:
                self.clicked = True
                return True

        async def provider(heading: str) -> str:
            self.assertEqual(heading, "Enter the access code")
            return "123456"

        settings = Settings(
            start_url="https://example.test",
            profile_dir=Path(".browser-profile"),
            artifacts_dir=Path("artifacts"),
            answers={},
            pages=[],
        )
        runner = WebRunner(
            settings,
            interactive=False,
            verification_code_provider=provider,
        )
        code_input = CodeInput()
        page = type("FakePage", (), {"url": "https://example.test/code"})()
        asyncio.run(runner._enter_verification_code(page, code_input))
        self.assertEqual(code_input.value, "123456")
        self.assertTrue(runner.clicked)

    def test_auth_code_page_uses_web_provider_when_not_interactive(self) -> None:
        import asyncio

        class EmptyLocator:
            @property
            def first(self) -> "EmptyLocator":
                return self

            async def count(self) -> int:
                return 0

        class CodeLocator:
            value = ""

            @property
            def first(self) -> "CodeLocator":
                return self

            async def count(self) -> int:
                return 1

            async def fill(self, value: str) -> None:
                self.value = value

        code_input = CodeLocator()

        class Page:
            url = "https://www.access.service.gov.uk/registration/code"

            def locator(self, selector: str) -> object:
                if "one-time-code" in selector:
                    return code_input
                return EmptyLocator()

        class WebRunner(VatAutomation):
            async def _heading(self, _page: object) -> str:
                return "Enter code to confirm your email address"

            async def _audit(self, *_args: object, **_kwargs: object) -> None:
                return None

            async def _click_auth_action(
                self, _page: object, _names: tuple[str, ...]
            ) -> bool:
                return True

        async def provider(_heading: str) -> str:
            return "654321"

        settings = Settings(
            start_url="https://example.test",
            profile_dir=Path(".browser-profile"),
            artifacts_dir=Path("artifacts"),
            answers={},
            pages=[],
        )
        runner = WebRunner(
            settings,
            interactive=False,
            verification_code_provider=provider,
        )
        asyncio.run(
            runner._handle_auth(Page(), "Enter code to confirm your email address")
        )
        self.assertEqual(code_input.value, "654321")

    def test_upload_processing_page_waits_for_automatic_redirect(self) -> None:
        import asyncio

        class Page:
            url = "https://example.test/file-upload/uploading-document"
            waits = 0

            async def wait_for_timeout(self, _milliseconds: int) -> None:
                self.waits += 1
                if self.waits == 2:
                    self.url = "https://example.test/file-upload/summary"

        page = Page()
        self.assertTrue(
            asyncio.run(VatAutomation._wait_for_upload_processing(page, attempts=3))
        )
        self.assertEqual(page.waits, 2)

    def test_named_action_matches_curly_apostrophe(self) -> None:
        import asyncio

        class Candidate:
            clicked = False

            async def is_visible(self) -> bool:
                return True

            async def inner_text(self) -> str:
                return "I do not have the company’s UTR number"

            async def click(self) -> None:
                self.clicked = True

        class Locators:
            def __init__(self, items: list[Candidate]) -> None:
                self.items = items
                self.first = self

            async def count(self) -> int:
                return len(self.items)

            async def all(self) -> list[Candidate]:
                return self.items

        candidate = Candidate()

        class FakePage:
            url = "https://example.test/non-uk-company-utr"

            def get_by_role(
                self, role: str, name: str | None = None, exact: bool = False
            ) -> Locators:
                if name is not None or role == "button":
                    return Locators([])
                return Locators([candidate])

        class ActionRunner(VatAutomation):
            async def _audit(self, *_args: object, **_kwargs: object) -> None:
                return None

        settings = Settings(
            start_url="https://example.test",
            profile_dir=Path(".browser-profile"),
            artifacts_dir=Path("artifacts"),
            answers={},
            pages=[],
        )
        runner = ActionRunner(settings, interactive=False)
        clicked = asyncio.run(
            runner._click_named_action(
                FakePage(), "I do not have the company's UTR number"
            )
        )
        self.assertTrue(clicked)
        self.assertTrue(candidate.clicked)

    def test_http_error_heading_is_detected(self) -> None:
        self.assertTrue(VatAutomation._is_remote_error_heading("403 ERROR"))
        self.assertTrue(
            VatAutomation._is_remote_error_heading("500 Internal Server Error")
        )
        self.assertFalse(
            VatAutomation._is_remote_error_heading("Check your answers")
        )

    def test_skip_action_runs_before_required_field_scan(self) -> None:
        class FakePage:
            url = "https://example.test/no-utr"

            async def wait_for_load_state(self, _state: str) -> None:
                return None

        class SkipRunner(VatAutomation):
            clicked = False
            fill_called = False

            async def _heading(self, _page: object) -> str:
                return "Your UTR"

            async def _audit(self, *_args: object, **_kwargs: object) -> None:
                return None

            async def _click_named_action(self, _page: object, name: str) -> bool:
                self.clicked = name == "I do not have the company's UTR number"
                return self.clicked

            async def _fill_until_stable(
                self, _page: object, _heading: str
            ) -> list[str]:
                self.fill_called = True
                return ["UTR"]

        settings = Settings(
            start_url="https://example.test/no-utr",
            profile_dir=Path(".browser-profile"),
            artifacts_dir=Path("artifacts"),
            answers={},
            pages=[
                PageRule(
                    path_contains="/no-utr",
                    action="skip:I do not have the company's UTR number",
                )
            ],
            max_steps=1,
        )
        runner = SkipRunner(settings, interactive=False)
        with self.assertRaisesRegex(AutomationStopped, "最大步骤数"):
            import asyncio

            asyncio.run(runner._drive(FakePage()))
        self.assertTrue(runner.clicked)
        self.assertFalse(runner.fill_called)


if __name__ == "__main__":
    unittest.main()
