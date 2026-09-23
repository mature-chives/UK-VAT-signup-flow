import asyncio
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from vat_automation.config import (
    PageRule,
    Settings,
    build_address_answers,
    build_international_address_answers,
    is_placeholder_chain,
    load_settings,
    normalize,
    resolve_dynamic_value,
    uk_next_month_first,
    vat_return_stagger_for_registration_month,
)
from vat_automation.document_parser import prepare_document_values
from vat_automation.runner import (
    BROWSER_ERROR_RETRIES,
    SAFE_ACTIONS,
    AutomationStopped,
    Control,
    VatAutomation,
    _masked_value,
    _safe_url,
)
from vat_automation.screenshot_flow import (
    SCREENSHOT_PATHS,
    build_flow_config,
    screenshot_page_rules,
)


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
                "Address line 3 (optional)": "Linping District, Hangzhou City",
                "Address line 4 (optional)": "Zhejiang",
                "Postcode": "311100",
                "Postcode (optional)": "311100",
                "Country": "China",
            },
        )
        self.assertTrue(
            all(
                len(value) <= 35
                for key, value in answers.items()
                if key.startswith("Address line")
            )
        )

    def test_page_specific_alias_wins_over_global_email_label(self) -> None:
        settings = Settings(
            start_url="https://example.test",
            profile_dir=Path(".browser-profile"),
            artifacts_dir=Path("artifacts"),
            answers={"Email address": "personal@example.test"},
            pages=[
                PageRule(
                    path_contains="/register-for-vat/business-email",
                    answers={"businessEmailAddress": "vat@example.test"},
                )
            ],
        )
        self.assertEqual(
            settings.answer_for(
                "Email address",
                "https://example.test/register-for-vat/business-email",
                "What is the business email address?",
                aliases=("businessEmailAddress",),
            ),
            (True, "vat@example.test"),
        )

    def test_placeholder_chain_rejects_ordinary_text(self) -> None:
        self.assertTrue(is_placeholder_chain("doc:first_name"))
        self.assertTrue(is_placeholder_chain("doc:vat_contact_email|env:HMRC_EMAIL"))
        self.assertFalse(is_placeholder_chain("It’s selling goods or services"))
        self.assertFalse(is_placeholder_chain("doc:first_name|please use this"))

    def test_document_bag_fills_name_and_keeps_business_email_separate(self) -> None:
        settings = Settings(
            start_url="https://example.test",
            profile_dir=Path(".browser-profile"),
            artifacts_dir=Path("artifacts"),
            answers={"Email address": "doc:email", "First name": "Alex"},
            pages=[
                PageRule(
                    path_contains="/identify-your-sole-trader-business/",
                    answers={
                        "first-name": "doc:first_name",
                        "last-name": "doc:last_name",
                    },
                ),
                PageRule(
                    path_contains="/register-for-vat/email-address",
                    answers={"email-address": "doc:email"},
                ),
                PageRule(
                    path_contains="/register-for-vat/business-email",
                    answers={
                        "businessEmailAddress": "doc:vat_contact_email|env:HMRC_EMAIL",
                    },
                ),
            ],
        )
        bag = prepare_document_values(
            {
                "first_name": "Ming",
                "last_name": "Li",
                "email": "person@example.test",
                "vat_contact_email": "vat@example.test",
            }
        )
        runner = VatAutomation(
            settings,
            credentials={"HMRC_EMAIL": "login@hmrc.test"},
            document_values=bag,
        )

        def resolved(
            label: str, url: str, heading: str, aliases: tuple[str, ...] = ()
        ) -> object:
            found, value = settings.answer_for(
                label, url, heading, aliases, document_values=bag
            )
            self.assertTrue(found)
            return runner._resolve_value(value)

        name_url = (
            "https://example.test/identify-your-sole-trader-business/abc/full-name"
        )
        self.assertEqual(
            resolved("First name", name_url, "What is your name?", ("first-name",)),
            "Ming",
        )
        self.assertEqual(
            resolved("Last name", name_url, "What is your name?", ("last-name",)),
            "Li",
        )
        self.assertEqual(
            resolved(
                "Email address",
                "https://example.test/register-for-vat/business-email",
                "What is the business email address?",
                ("businessEmailAddress",),
            ),
            "vat@example.test",
        )
        self.assertEqual(
            resolved(
                "Email address",
                "https://example.test/register-for-vat/email-address",
                "What is your email address?",
                ("email-address",),
            ),
            "person@example.test",
        )

    def test_missing_vat_contact_email_falls_back_to_login_email(self) -> None:
        settings = Settings(
            start_url="https://example.test",
            profile_dir=Path(".browser-profile"),
            artifacts_dir=Path("artifacts"),
            answers={},
            pages=[
                PageRule(
                    path_contains="/register-for-vat/business-email",
                    answers={
                        "businessEmailAddress": "doc:vat_contact_email|env:HMRC_EMAIL",
                    },
                )
            ],
        )
        bag = prepare_document_values({"email": "person@example.test"})
        runner = VatAutomation(
            settings,
            credentials={"HMRC_EMAIL": "login@hmrc.test"},
            document_values=bag,
        )
        found, value = settings.answer_for(
            "Email address",
            "https://example.test/register-for-vat/business-email",
            "What is the business email address?",
            ("businessEmailAddress",),
            document_values=bag,
        )
        self.assertTrue(found)
        self.assertEqual(runner._resolve_value(value), "login@hmrc.test")

    def test_home_address_source_expands_from_document_bag(self) -> None:
        settings = Settings(
            start_url="https://example.test",
            profile_dir=Path(".browser-profile"),
            artifacts_dir=Path("artifacts"),
            answers={"Address line 1": "JSON Room"},
            pages=[
                PageRule(
                    path_contains="/register-for-vat/home-address/international",
                    address_source="doc:home",
                )
            ],
        )
        bag = prepare_document_values(
            {
                "home_premises": "201, Building 3",
                "home_street": "Jingxi Community",
                "home_city": "Guangzhou City",
                "home_country": "China",
                "home_postcode": "510080",
            }
        )
        found, value = settings.answer_for(
            "Address line 1",
            "https://example.test/register-for-vat/home-address/international",
            "What is your home address?",
            document_values=bag,
        )
        self.assertEqual((found, value), (True, "201, Building 3"))

    def test_structured_address_rejects_unsafe_overflow(self) -> None:
        with self.assertRaisesRegex(ValueError, "premises 含有超过 35"):
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

    def test_international_address_keeps_full_words_and_splits_long_street(self) -> None:
        answers = build_international_address_answers(
            {
                "premises": "Room 509, 5th Floor, Building 4",
                "street": "No. 532 Xingtian Road, Donghu Street",
                "locality": "Linping District",
                "city": "Hangzhou City",
                "region": "Zhejiang Province",
                "postcode": "310000",
                "country": "China",
            }
        )
        self.assertEqual(
            {
                key: answers[key]
                for key in (
                    "Address line 1",
                    "Address line 2",
                    "Address line 3 (optional)",
                    "Address line 4 (optional)",
                    "Address line 5 (optional)",
                )
            },
            {
                "Address line 1": "Room 509, 5th Floor, Building 4",
                "Address line 2": "No. 532 Xingtian Road",
                "Address line 3 (optional)": "Donghu Street",
                "Address line 4 (optional)": "Linping District",
                "Address line 5 (optional)": "Hangzhou City, Zhejiang Province",
            },
        )
        self.assertTrue(
            all(
                len(value) <= 35
                for key, value in answers.items()
                if key.startswith("Address line")
            )
        )

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

    def test_committed_flow_config_uses_placeholders_not_applicant_literals(self) -> None:
        import json
        import tempfile

        flow_path = Path(__file__).resolve().parents[1] / "vat-config.flow.json"
        raw = json.loads(flow_path.read_text(encoding="utf-8"))
        blob = json.dumps(raw, ensure_ascii=False)
        self.assertIn("doc:first_name", blob)
        self.assertIn("doc:vat_contact_email|env:HMRC_EMAIL", blob)
        self.assertIn("doc:home", blob)
        self.assertNotIn("Alex", blob)
        self.assertNotIn("Tester", blob)
        self.assertNotIn("@example.com", blob)
        with tempfile.TemporaryDirectory() as workspace:
            copy = Path(workspace) / "vat-config.flow.json"
            copy.write_text(flow_path.read_text(encoding="utf-8"), encoding="utf-8")
            settings = load_settings(copy)
        self.assertEqual(
            settings.answer_for(
                "First name",
                "https://example.test/identify-your-sole-trader-business/id/full-name",
                "What is your name?",
            ),
            (True, "doc:first_name"),
        )
        home = next(
            page
            for page in settings.pages
            if "home-address/international" in page.path_contains
        )
        self.assertEqual(home.address_source, "doc:home")
        self.assertEqual(build_flow_config()["answers"]["Business name"], "doc:business_name")

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
        self.assertTrue(
            runner._is_final_page(
                "https://www.tax.service.gov.uk/register-for-vat/check-confirm-answers",
                "Check your answers before sending your application",
            )
        )
        self.assertNotIn("Submit application", SAFE_ACTIONS)
        self.assertIn("Continue to register for VAT", SAFE_ACTIONS)

    def test_final_review_change_items_carry_section_field_and_value(self) -> None:
        import asyncio

        class _Locator:
            async def evaluate_all(self, _script: str) -> list[dict[str, object]]:
                return [
                    {
                        "id": "change-0",
                        "label": "About you — Full Name",
                        "section": "About you",
                        "field": "Full Name",
                        "value": "San Zhang",
                    },
                    {
                        "id": "change-1",
                        "label": "About you — Home address",
                        "section": "About you",
                        "field": "Home address",
                        "value": "Room 509, 5th Floor, Building 4\n310000\nChina",
                    },
                    {"id": "change-2", "label": "Legacy item"},
                ]

        page = type(
            "Page",
            (),
            {"locator": staticmethod(lambda _selector: _Locator())},
        )()
        runner = VatAutomation.__new__(VatAutomation)
        items = asyncio.run(runner._final_review_change_items(page))
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0]["section"], "About you")
        self.assertEqual(items[0]["field"], "Full Name")
        self.assertEqual(items[0]["value"], "San Zhang")
        self.assertIn("\n", items[1]["value"])
        self.assertEqual(items[2]["section"], "")
        self.assertEqual(items[2]["field"], "")
        self.assertEqual(items[2]["value"], "")

    def test_final_review_submits_only_after_provider_confirmation(self) -> None:
        import asyncio
        import tempfile

        sequence: list[str] = []

        class ReviewRunner(VatAutomation):
            async def _final_review_change_items(
                self, _page: object
            ) -> list[dict[str, str]]:
                return []

            async def _expand_final_review_sections(
                self, _page: object, _heading: str
            ) -> None:
                sequence.append("expanded")

            async def _snapshot(
                self, _page: object, _heading: str, **_kwargs: object
            ) -> Path:
                sequence.append("screenshot")
                return self.settings.artifacts_dir / "review.png"

            async def _save_page_pdf(self, _page: object, _reason: str) -> Path:
                sequence.append("pdf")
                return self.settings.artifacts_dir / "review.pdf"

            async def _audit(self, event: str, **_kwargs: object) -> None:
                sequence.append(event)

            async def _click_named_action(self, _page: object, name: str) -> bool:
                sequence.append(f"click:{name}")
                return name == "Confirm and submit"

            async def _verify_final_submission(
                self, _page: object, _heading: str, _action: str
            ) -> None:
                sequence.append("application-submitted")

        async def provider(info: dict[str, object]) -> None:
            self.assertTrue(str(info["pdf"]).endswith("review.pdf"))
            sequence.append("human-confirmed")

        with tempfile.TemporaryDirectory() as workspace:
            base = Path(workspace)
            runner = ReviewRunner(
                Settings(
                    start_url="https://example.test",
                    profile_dir=base / "profile",
                    artifacts_dir=base / "artifacts",
                    answers={},
                    pages=[],
                ),
                interactive=False,
                final_review_provider=provider,
            )
            page = type(
                "Page",
                (),
                {
                    "url": (
                        "https://www.tax.service.gov.uk/register-for-vat/"
                        "check-confirm-answers"
                    )
                },
            )()
            asyncio.run(
                runner._handle_final_review(
                    page, "Check your answers before sending your application"
                )
            )

        self.assertLess(
            sequence.index("human-confirmed"),
            sequence.index("click:Confirm and submit"),
        )
        self.assertLess(sequence.index("expanded"), sequence.index("screenshot"))
        self.assertLess(sequence.index("expanded"), sequence.index("pdf"))
        self.assertIn("application-submitted", sequence)

    def test_final_review_can_return_to_any_change_page_before_submission(self) -> None:
        import asyncio
        import tempfile

        sequence: list[str] = []
        decisions = iter(("edit", "submit"))

        class ReviewRunner(VatAutomation):
            async def _final_review_change_items(
                self, _page: object
            ) -> list[dict[str, str]]:
                return [{"id": "change-0", "label": "Business email address"}]

            async def _expand_final_review_sections(
                self, _page: object, _heading: str
            ) -> None:
                sequence.append("expanded")

            async def _snapshot(
                self, _page: object, _heading: str, **_kwargs: object
            ) -> Path:
                sequence.append("screenshot")
                return self.settings.artifacts_dir / "review.png"

            async def _save_page_pdf(self, _page: object, _reason: str) -> Path:
                sequence.append("pdf")
                return self.settings.artifacts_dir / "review.pdf"

            async def _audit(self, event: str, **_kwargs: object) -> None:
                sequence.append(event)

            async def _handle_remote_final_review_edit(
                self,
                _page: object,
                target: str,
                _changes: list[dict[str, str]],
            ) -> str:
                if target != "change-0":
                    raise AssertionError(target)
                sequence.append("remote-edit")
                return "Check your answers after editing"

            async def _click_named_action(self, _page: object, name: str) -> bool:
                sequence.append(f"click:{name}")
                return True

            async def _verify_final_submission(
                self, _page: object, _heading: str, _action: str
            ) -> None:
                sequence.append("application-submitted")

        async def provider(info: dict[str, object]) -> dict[str, str]:
            self.assertTrue(info["editable"])
            decision = next(decisions)
            sequence.append(f"decision:{decision}")
            return {
                "action": decision,
                "target": "change-0" if decision == "edit" else "",
            }

        async def remote_provider(_info: dict[str, object]) -> dict[str, object]:
            return {"answers": {}, "action": "Save and continue"}

        with tempfile.TemporaryDirectory() as workspace:
            base = Path(workspace)
            runner = ReviewRunner(
                Settings(
                    start_url="https://example.test",
                    profile_dir=base / "profile",
                    artifacts_dir=base / "artifacts",
                    answers={},
                    pages=[],
                ),
                interactive=False,
                final_review_provider=provider,
                remote_edit_provider=remote_provider,
            )
            page = type(
                "Page",
                (),
                {
                    "url": (
                        "https://www.tax.service.gov.uk/register-for-vat/"
                        "check-confirm-answers"
                    )
                },
            )()
            asyncio.run(runner._handle_final_review(page, "Check your answers"))

        self.assertEqual(sequence.count("expanded"), 2)
        self.assertLess(sequence.index("remote-edit"), sequence.index("decision:submit"))
        self.assertIn("application-submitted", sequence)

    def test_skip_edit_action_recognizes_missing_utr_link(self) -> None:
        from vat_automation.runner import is_skip_edit_action

        self.assertTrue(
            is_skip_edit_action("I do not have the company's UTR number")
        )
        self.assertTrue(
            is_skip_edit_action("I do not have the company’s UTR number")
        )
        self.assertFalse(is_skip_edit_action("Continue"))
        self.assertFalse(is_skip_edit_action("Save and continue"))

    def test_final_review_task_link_ignores_locked_and_other_tasks(self) -> None:
        self.assertTrue(
            VatAutomation._is_final_review_task_link(
                "/register-for-vat/check-confirm-answers",
                "Check your answers",
            )
        )
        self.assertFalse(
            VatAutomation._is_final_review_task_link(
                "/register-for-vat/check-confirm-answers",
                "Check your answers",
                "Check your answers Cannot start yet",
            )
        )
        self.assertFalse(
            VatAutomation._is_final_review_task_link(
                "/register-for-vat/business-email",
                "Business email address",
            )
        )
        self.assertEqual(
            VatAutomation._application_progress_url(
                "https://www.tax.service.gov.uk/register-for-vat/business-email"
            ),
            "https://www.tax.service.gov.uk/register-for-vat/application-progress",
        )

    def test_remote_edit_returns_via_progress_after_save(self) -> None:
        import asyncio
        import tempfile

        sequence: list[str] = []

        class Page:
            def __init__(self) -> None:
                self.url = (
                    "https://www.tax.service.gov.uk/register-for-vat/"
                    "business-email"
                )
                self.heading = "What is the business email address?"

            async def wait_for_load_state(self, *_args: object, **_kwargs: object) -> None:
                return None

            async def wait_for_timeout(self, _milliseconds: int) -> None:
                return None

            async def goto(self, url: str, **_kwargs: object) -> None:
                sequence.append(f"goto:{url}")
                self.url = url
                if url.endswith("/application-progress"):
                    self.heading = "Your application progress"
                else:
                    self.heading = "Check your answers"

        class ReviewRunner(VatAutomation):
            async def _heading(self, current: Page) -> str:
                return current.heading

            async def _validation_errors(self, _page: object) -> list[str]:
                return []

            async def _click_final_review_task(self, current: Page) -> bool:
                sequence.append("click-final-review-task")
                current.url = (
                    "https://www.tax.service.gov.uk/register-for-vat/"
                    "check-confirm-answers"
                )
                current.heading = "Check your answers"
                return True

            async def _audit(self, event: str, **_kwargs: object) -> None:
                sequence.append(event)

        with tempfile.TemporaryDirectory() as workspace:
            base = Path(workspace)
            runner = ReviewRunner(
                Settings(
                    start_url="https://example.test",
                    profile_dir=base / "profile",
                    artifacts_dir=base / "artifacts",
                    answers={},
                    pages=[],
                ),
                interactive=False,
            )
            returned = asyncio.run(
                runner._return_to_final_review_from_edit(
                    Page(),
                    "https://www.tax.service.gov.uk/register-for-vat/"
                    "check-confirm-answers",
                )
            )
        self.assertTrue(returned)
        self.assertIn(
            "goto:https://www.tax.service.gov.uk/register-for-vat/application-progress",
            sequence,
        )
        self.assertIn("click-final-review-task", sequence)

    def test_final_review_expands_all_sections_before_archiving(self) -> None:
        import asyncio
        import tempfile

        class Toggle:
            def __init__(self, page: "Page", name: str) -> None:
                self.page = page
                self.name = name

            @property
            def first(self) -> "Toggle":
                return self

            async def count(self) -> int:
                expected = "Hide all sections" if self.page.expanded else "Show all sections"
                return int(self.name == expected)

            async def is_visible(self) -> bool:
                return bool(await self.count())

            async def click(self) -> None:
                self.page.expanded = True

        class Page:
            url = "https://example.test/register-for-vat/check-confirm-answers"
            expanded = False

            def get_by_role(
                self, _role: str, *, name: str, exact: bool = True
            ) -> Toggle:
                return Toggle(self, name)

            async def wait_for_timeout(self, _milliseconds: int) -> None:
                return None

        class ReviewRunner(VatAutomation):
            async def _audit(self, *_args: object, **_kwargs: object) -> None:
                return None

        with tempfile.TemporaryDirectory() as workspace:
            base = Path(workspace)
            runner = ReviewRunner(
                Settings(
                    start_url="https://example.test",
                    profile_dir=base / "profile",
                    artifacts_dir=base / "artifacts",
                    answers={},
                    pages=[],
                ),
                interactive=False,
            )
            page = Page()
            asyncio.run(
                runner._expand_final_review_sections(
                    page, "Check your answers before sending your application"
                )
            )
            self.assertTrue(page.expanded)

    def test_final_submit_is_not_completed_while_review_page_remains(self) -> None:
        import asyncio
        import tempfile

        class Page:
            url = (
                "https://www.tax.service.gov.uk/register-for-vat/"
                "check-confirm-answers"
            )

            async def wait_for_load_state(self, *_args: object, **_kwargs: object) -> None:
                return None

            async def wait_for_timeout(self, _milliseconds: int) -> None:
                return None

        class ReviewRunner(VatAutomation):
            async def _heading(self, _page: object) -> str:
                return "Check your answers before sending your application"

            async def _snapshot(
                self, _page: object, _heading: str, **_kwargs: object
            ) -> Path:
                return self.settings.artifacts_dir / "not-submitted.png"

        with tempfile.TemporaryDirectory() as workspace:
            base = Path(workspace)
            runner = ReviewRunner(
                Settings(
                    start_url="https://example.test",
                    profile_dir=base / "profile",
                    artifacts_dir=base / "artifacts",
                    answers={},
                    pages=[],
                ),
                interactive=False,
            )
            with self.assertRaisesRegex(AutomationStopped, "仍停留"):
                asyncio.run(
                    runner._verify_final_submission(
                        Page(),
                        "Check your answers before sending your application",
                        "Confirm and submit",
                    )
                )

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

    def test_live_application_warnings_scan_document_bag_not_placeholders(self) -> None:
        settings = Settings(
            start_url="https://example.test",
            profile_dir=Path(".browser-profile"),
            artifacts_dir=Path("artifacts"),
            answers={"Business name": "doc:business_name"},
            pages=[],
        )
        self.assertEqual(settings.live_application_warnings(), [])
        warnings = settings.live_application_warnings(
            document_values={"business_name": "Northstar Trading"}
        )
        self.assertIn("检测到 Northstar 测试标记", warnings)

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

    def test_missing_credential_value_is_rejected(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as workspace:
            base = Path(workspace)
            settings = Settings(
                start_url="https://example.test",
                profile_dir=base / "profile",
                artifacts_dir=base / "artifacts",
                answers={},
                pages=[],
            )
            runner = VatAutomation(settings, credentials={"HMRC_PRESENT": "value"})
            self.assertEqual(runner._resolve_value("env:HMRC_PRESENT"), "value")
            with self.assertRaisesRegex(KeyError, "HMRC_TEST_MISSING"):
                runner._resolve_value("env:HMRC_TEST_MISSING")

    def test_explicit_credentials_replace_process_environment(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as workspace:
            base = Path(workspace)
            settings = Settings(
                start_url="https://example.test",
                profile_dir=base / "profile",
                artifacts_dir=base / "artifacts",
                answers={},
                pages=[],
            )
            # 显式传入凭据后，进程环境变量不应再被读取。
            with patch.dict("os.environ", {"HMRC_ONLY_IN_ENV": "leak"}, clear=False):
                runner = VatAutomation(settings, credentials={})
                with self.assertRaises(KeyError):
                    runner._resolve_value("env:HMRC_ONLY_IN_ENV")

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

    def test_remote_edit_applies_text_and_radio_values(self) -> None:
        import asyncio
        import tempfile

        class Locator:
            def __init__(self) -> None:
                self.filled = ""
                self.checked = False

            @property
            def first(self) -> "Locator":
                return self

            async def fill(self, value: str) -> None:
                self.filled = value

            async def check(self) -> None:
                self.checked = True

        class Page:
            def __init__(self) -> None:
                self.locators = {
                    '[id="business-email"]': Locator(),
                    '[id="choice-yes"]': Locator(),
                }

            def locator(self, selector: str) -> Locator:
                return self.locators[selector]

        fields = [
            {
                "key": "businessEmailAddress",
                "kind": "email",
                "name": "businessEmailAddress",
                "element_id": "business-email",
                "combobox": False,
                "options": [],
            },
            {
                "key": "choice",
                "kind": "radio",
                "name": "choice",
                "element_id": "choice-yes",
                "options": [
                    {
                        "value": "true",
                        "label": "Yes",
                        "element_id": "choice-yes",
                        "index": 0,
                    }
                ],
            },
        ]
        with tempfile.TemporaryDirectory() as workspace:
            base = Path(workspace)
            runner = VatAutomation(
                Settings(
                    start_url="https://example.test",
                    profile_dir=base / "profile",
                    artifacts_dir=base / "artifacts",
                    answers={},
                    pages=[],
                ),
                interactive=False,
            )
            page = Page()
            asyncio.run(
                runner._apply_remote_edit_answers(
                    page,
                    fields,
                    {
                        "businessEmailAddress": "vat@example.test",
                        "choice": "Yes",
                    },
                )
            )
        self.assertEqual(
            page.locators['[id="business-email"]'].filled,
            "vat@example.test",
        )
        self.assertTrue(page.locators['[id="choice-yes"]'].checked)

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

class GatewayUserIdCaptureTests(unittest.TestCase):
    """新建 Government Gateway 账号后抓取 User ID，供写回 .env 复用。"""

    def _runner(self) -> VatAutomation:
        class CaptureRunner(VatAutomation):
            events: list[dict] = []
            captured: list[str] = []

            async def _audit(self, event: str, **details: object) -> None:
                self.events.append({"event": event, **details})

        settings = Settings(
            start_url="https://example.test",
            profile_dir=Path(".browser-profile"),
            artifacts_dir=Path("artifacts"),
            answers={},
            pages=[],
        )
        runner = CaptureRunner(settings, interactive=False, credentials={})
        runner.gateway_user_id_provider = runner.captured.append
        return runner

    @staticmethod
    def _page(
        text: str,
        url: str = "https://www.access.service.gov.uk/registration/confirmation/x",
    ) -> object:
        class Locator:
            async def inner_text(self) -> str:
                return text

        class Page:
            url = ""

            def locator(self, _selector: str) -> Locator:
                return Locator()

        page = Page()
        page.url = url
        return page

    def test_user_id_is_captured_and_audited_masked(self) -> None:
        runner = self._runner()
        asyncio.run(
            runner._capture_gateway_user_id(
                self._page(
                    "Your Government Gateway user ID is:\n123456789012\nKeep it safe."
                ),
                "Your Government Gateway user ID is:",
            )
        )
        self.assertEqual(runner.gateway_user_id, "123456789012")
        self.assertEqual(runner.captured, ["123456789012"])
        self.assertEqual(
            runner.events,
            [
                {
                    "event": "gateway-user-id-captured",
                    "user_id": "12********12",
                    "digits": 12,
                    "matched": "12-digit",
                }
            ],
        )

    def test_email_digits_are_not_mistaken_for_the_user_id(self) -> None:
        """真实踩过的坑：页面上的邮箱 1143038963@qq.com 被当成了 User ID。"""
        runner = self._runner()
        asyncio.run(
            runner._capture_gateway_user_id(
                self._page(
                    "We have sent a confirmation email to 1143038963@qq.com.\n"
                    "Your Government Gateway user ID is:\n"
                    "123456789012\n"
                ),
                "Your Government Gateway user ID is:",
            )
        )
        self.assertEqual(runner.gateway_user_id, "123456789012")
        self.assertEqual(runner.events[0]["matched"], "12-digit")

    def test_ten_digit_fallback_is_marked(self) -> None:
        runner = self._runner()
        asyncio.run(
            runner._capture_gateway_user_id(
                self._page("Your Government Gateway user ID is: 1143038963"),
                "Your Government Gateway user ID is:",
            )
        )
        self.assertEqual(runner.gateway_user_id, "1143038963")
        self.assertEqual(runner.events[0]["matched"], "fallback")

    def test_grouped_digits_are_still_captured(self) -> None:
        runner = self._runner()
        asyncio.run(
            runner._capture_gateway_user_id(
                self._page("Your Government Gateway user ID is:\n1234 5678 9012\n"),
                "Your Government Gateway user ID is:",
            )
        )
        self.assertEqual(runner.gateway_user_id, "123456789012")

    def test_page_without_user_id_is_flagged(self) -> None:
        """HMRC 只发邮件、页面上没有 ID 时要留下证据并提示人工填写。"""
        runner = self._runner()
        page = self._page(
            "Your Government Gateway user ID is:\n"
            "We have sent it to 1143038963@qq.com\n"
        )
        asyncio.run(
            runner._capture_gateway_user_id(page, "Your Government Gateway user ID is:")
        )
        self.assertEqual(runner.gateway_user_id, "")
        self.assertEqual(runner.events[0]["event"], "gateway-user-id-not-found")

    def test_other_pages_are_ignored(self) -> None:
        runner = self._runner()
        asyncio.run(
            runner._capture_gateway_user_id(
                self._page(
                    "123456789012",
                    "https://www.access.service.gov.uk/registration/email",
                ),
                "Enter your email address",
            )
        )
        self.assertEqual(runner.gateway_user_id, "")
        self.assertEqual(runner.events, [])

    def test_confirmation_url_still_captures_when_heading_changes(self) -> None:
        """HMRC 改标题文案时，注册确认页的 URL 仍能识别。"""
        runner = self._runner()
        asyncio.run(
            runner._capture_gateway_user_id(
                self._page("你的用户 ID 是：123456789012"), "Something else"
            )
        )
        self.assertEqual(runner.gateway_user_id, "123456789012")

    def test_masked_value_hides_the_middle(self) -> None:
        self.assertEqual(_masked_value("123456789012"), "12********12")
        self.assertEqual(_masked_value("1234"), "****")


class SignInMethodTests(unittest.TestCase):
    """登录方式优先级：网页/环境变量 > 流程配置默认值 > 新建账号。"""

    def _runner(self, default: str = "", **credentials: str) -> VatAutomation:
        settings = Settings(
            start_url="https://example.test",
            profile_dir=Path(".browser-profile"),
            artifacts_dir=Path("artifacts"),
            answers={},
            pages=[],
            default_sign_in_method=default,
        )
        return VatAutomation(settings, interactive=False, credentials=dict(credentials))

    def test_provided_method_wins_over_config_default(self) -> None:
        runner = self._runner(
            "Government Gateway", HMRC_SIGN_IN_METHOD="Create new sign in details"
        )
        self.assertEqual(runner._sign_in_method(), "Create new sign in details")

    def test_config_default_is_used_when_not_provided(self) -> None:
        self.assertEqual(
            self._runner("Government Gateway")._sign_in_method(),
            "Government Gateway",
        )

    def test_fallback_is_creating_a_new_account(self) -> None:
        self.assertEqual(
            self._runner()._sign_in_method(), "Create new sign in details"
        )


class BrowserErrorRecoveryTests(unittest.TestCase):
    """HMRC 偶发 ERR_CONNECTION_CLOSED 时应退回上一页重试，而不是直接停。"""

    def test_browser_error_page_is_detected(self) -> None:
        self.assertTrue(
            VatAutomation._is_browser_error_page("chrome-error://chromewebdata/", "")
        )
        self.assertTrue(
            VatAutomation._is_browser_error_page("https://x.test", "无法访问此网站")
        )
        self.assertTrue(
            VatAutomation._is_browser_error_page(
                "https://x.test", "This site can’t be reached"
            )
        )
        self.assertFalse(
            VatAutomation._is_browser_error_page(
                "https://www.tax.service.gov.uk/register-for-vat/", "Check your answers"
            )
        )

    def test_browser_error_is_retried_from_last_good_page(self) -> None:
        class FakePage:
            def __init__(self) -> None:
                self.url = "chrome-error://chromewebdata/"
                self.gotos: list[str] = []

            async def wait_for_load_state(self, _state: str) -> None:
                return None

            async def wait_for_timeout(self, _ms: int) -> None:
                return None

            async def goto(self, url: str, wait_until: str | None = None) -> None:
                self.gotos.append(url)
                self.url = url

        class RetryRunner(VatAutomation):
            events: list[str] = []
            snapshots: list[str] = []

            async def _heading(self, page: object) -> str:
                url = getattr(page, "url", "")
                return "无法访问此网站" if url.startswith("chrome-error") else "Get an EORI number"

            async def _audit(self, event: str, **_details: object) -> None:
                self.events.append(event)

            async def _snapshot(
                self, _page: object, _heading: str, *, reason: str, missing: object = None
            ) -> Path:
                self.snapshots.append(reason)
                return Path("/private/tmp/never-written.png")

        settings = Settings(
            start_url="https://www.gov.uk/eori/apply-for-eori",
            profile_dir=Path(".browser-profile"),
            artifacts_dir=Path("artifacts"),
            answers={},
            pages=[],
            max_steps=1,
        )
        page = FakePage()
        runner = RetryRunner(settings, interactive=False)
        runner._last_good_url = "https://www.gov.uk/eori/apply-for-eori"
        # 恢复成功后会继续循环，因此这里只会因为步数上限结束，而不是网络错误。
        with self.assertRaisesRegex(AutomationStopped, "最大步骤数"):
            asyncio.run(runner._drive(page))
        self.assertEqual(page.gotos, ["https://www.gov.uk/eori/apply-for-eori"])
        self.assertIn("browser-error-retry", runner.events)
        self.assertEqual(runner.snapshots, [])

    def test_network_error_stops_with_clear_reason(self) -> None:
        class FakePage:
            url = "chrome-error://chromewebdata/"

            async def wait_for_load_state(self, _state: str) -> None:
                return None

            async def wait_for_timeout(self, _ms: int) -> None:
                return None

            async def goto(self, _url: str, wait_until: str | None = None) -> None:
                return None  # 仍停留在错误页

        class AlwaysErrorRunner(VatAutomation):
            events: list[str] = []
            snapshots: list[str] = []

            async def _heading(self, _page: object) -> str:
                return "无法访问此网站"

            async def _audit(self, event: str, **_details: object) -> None:
                self.events.append(event)

            async def _snapshot(
                self, _page: object, _heading: str, *, reason: str, missing: object = None
            ) -> Path:
                self.snapshots.append(reason)
                return Path("/private/tmp/never-written.png")

        settings = Settings(
            start_url="https://www.gov.uk/eori/apply-for-eori",
            profile_dir=Path(".browser-profile"),
            artifacts_dir=Path("artifacts"),
            answers={},
            pages=[],
        )
        runner = AlwaysErrorRunner(settings, interactive=False)
        runner._last_good_url = "https://www.gov.uk/eori/apply-for-eori"
        with self.assertRaisesRegex(AutomationStopped, "连接错误"):
            asyncio.run(runner._drive(FakePage()))
        self.assertEqual(runner.snapshots, ["network-error"])
        self.assertEqual(
            runner.events.count("browser-error-retry"), BROWSER_ERROR_RETRIES
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
