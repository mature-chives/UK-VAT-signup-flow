<<<<<<< Updated upstream
from __future__ import annotations

from copy import deepcopy
from datetime import date


def _rule(
    path: str,
    *,
    default: object | None = None,
    answers: dict[str, object] | None = None,
    action: str = "continue",
    address: str | None = None,
) -> dict[str, object]:
    rule: dict[str, object] = {
        "match": {"path_contains": path},
        "answers": answers or {},
        "action": action,
    }
    if default is not None:
        rule["default_answer"] = default
    if address is not None:
        rule["address"] = address
    return rule


def flow_global_answers() -> dict[str, str]:
    """正式流程表的全局答案：流程字面量 + 申请人占位符。不含会撞车的 Email address。"""
    return {
        "Why do you want to register the business for VAT?": (
            "It’s selling goods or services and needs or wants to charge VAT to customers"
        ),
        "Country": "China",
        "Business name": "doc:business_name",
        "Trading name": "doc:trading_name",
        "Full name": "doc:full_name",
        "What does the business do?": "doc:business_description",
    }


def flow_page_rules() -> list[dict[str, object]]:
    """海外企业 VAT 路径的流程表。申请人真值用 doc: / env:，不写进本文件。"""
    birth = {
        "date-of-birth.day": "doc:date-of-birth.day",
        "date-of-birth.month": "doc:date-of-birth.month",
        "date-of-birth.year": "doc:date-of-birth.year",
        "Day": "doc:date-of-birth.day",
        "Month": "doc:date-of-birth.month",
        "Year": "doc:date-of-birth.year",
    }
    return [
        {
            "match": {
                "path_contains": "/business-account",
                "heading_contains": "Add a tax to your account",
            },
            "answers": {},
            "default_answer": "VAT",
            "action": "continue",
        },
        _rule("/business-account/add-tax/vat", default="VAT"),
        _rule("/business-account/add-tax/vat/do-you-have-a-vat-number", default="No"),
        _rule(
            "/register-for-vat/application-reference",
            answers={"value": "doc:application_reference"},
        ),
        _rule("/register-for-vat/honesty-declaration"),
        _rule("/check-if-you-can-register-for-vat/fixed-establishment", default="false"),
        _rule("/check-if-you-can-register-for-vat/business-entity-overseas", default="55"),
        _rule("/check-if-you-can-register-for-vat/agricultural-flat-rate", default="false"),
        _rule("/check-if-you-can-register-for-vat/business-activities-next-12-months", default="false"),
        _rule("/check-if-you-can-register-for-vat/whos-the-application-for", default="own"),
        _rule("/check-if-you-can-register-for-vat/registration-reason", default="selling-goods-and-services"),
        _rule("/check-if-you-can-register-for-vat/made-or-intend-to-make-taxable-supplies", default="true"),
        _rule(
            "/check-if-you-can-register-for-vat/date-of-taxable-supplies-in-uk",
            answers={
                "thresholdTaxableSuppliesDate.day": "date:uk-next-month-first.day",
                "thresholdTaxableSuppliesDate.month": "date:uk-next-month-first.month",
                "thresholdTaxableSuppliesDate.year": "date:uk-next-month-first.year",
                "Day": "date:uk-next-month-first.day",
                "Month": "date:uk-next-month-first.month",
                "Year": "date:uk-next-month-first.year",
            },
        ),
        _rule("/non-uk-company-utr", action="skip:I do not have the company's UTR number"),
        _rule(
            "/overseas-identifier",
            answers={
                "tax-identifier-radio": "doc:tax-identifier-radio",
                "tax-identifier": "doc:overseas_tax_identifier",
            },
        ),
        _rule(
            "/overseas-tax-identifier-country",
            answers={"countryAutocomplete": "China", "country": "China"},
        ),
        _rule(
            "/identify-your-sole-trader-business/",
            answers={
                "first-name": "doc:first_name",
                "First name": "doc:first_name",
                "last-name": "doc:last_name",
                "Last name": "doc:last_name",
            },
        ),
        _rule("/date-of-birth", answers=birth),
        _rule(
            "/national-insurance-number",
            action="skip:I do not have a National Insurance number",
        ),
        _rule("/register-for-vat/role-in-the-business", default="director"),
        _rule("/register-for-vat/changed-name", default="false"),
        _rule(
            "/register-for-vat/home-address/international",
            address="doc:home",
        ),
        _rule("/register-for-vat/current-address", default="true"),
        _rule(
            "/register-for-vat/email-address",
            answers={"email-address": "doc:email", "Email address": "doc:email"},
        ),
        _rule(
            "/register-for-vat/telephone-number",
            answers={
                "telephone-number": "doc:phone",
                "Telephone number": "doc:phone",
            },
        ),
        _rule("/register-for-vat/confirm-trading-name", default="true"),
        _rule(
            "/register-for-vat/principal-place-business/international",
            address="doc:business",
        ),
        _rule(
            "/register-for-vat/business-email",
            answers={
                "businessEmailAddress": "doc:vat_contact_email|env:HMRC_EMAIL",
            },
        ),
        _rule(
            "/register-for-vat/business-telephone-number",
            answers={"daytimePhone": "doc:business_phone|env:HMRC_MFA_PHONE"},
        ),
        _rule("/register-for-vat/business-has-website", default="false"),
        _rule("/register-for-vat/vat-correspondence-language", default="english"),
        _rule("/register-for-vat/contact-preference", default="email"),
        _rule("/register-for-vat/land-and-property", default="false"),
        _rule(
            "/search-standard-industry-classification-codes",
            answers={"sicSearch": "47910"},
            action="Search",
        ),
        _rule(
            "/check-confirm-standard-industry-classification-codes",
            action="Confirm and continue",
        ),
        _rule("/register-for-vat/other-business-involvements", default="false"),
        _rule("/register-for-vat/imports-or-exports", default="true"),
        _rule("/register-for-vat/apply-for-eori", default="true"),
        _rule("/register-for-vat/standard-rate-turnover", answers={"standardRateSupplies": "10000"}),
        _rule("/register-for-vat/reduced-rate-turnover", answers={"reducedRateSupplies": "0"}),
        _rule("/register-for-vat/zero-rated-turnover", answers={"zeroRatedSupplies": "0"}),
        _rule("/register-for-vat/total-taxable-turnover", default="true"),
        _rule("/register-for-vat/sell-or-move-nip", default="false"),
        _rule("/register-for-vat/receive-goods-nip", default="false"),
        _rule("/register-for-vat/claim-vat-refunds", default="false"),
        _rule("/register-for-vat/send-goods-overseas", default="false"),
        _rule("/register-for-vat/storing-goods-for-dispatch", default="UK"),
        _rule("/register-for-vat/using-fulfilment-warehouse", default="false"),
        _rule("/register-for-vat/how-often-submit-returns", default="quarterly"),
        _rule(
            "/register-for-vat/submit-vat-returns",
            default="vat-return-stagger:uk-next-month-first",
        ),
        _rule("/register-for-vat/tax-representative", default="false"),
        _rule("/register-for-vat/join-flat-rate", default="false"),
        _rule("/register-for-vat/attachment-method", default="2"),
        _rule("/register-for-vat/file-upload/upload-document", action="stop"),
    ]


def screenshot_page_rules(
    *, suffix: str, birth_date: date
) -> list[dict[str, object]]:
    """测试夹具：同一路径覆盖为字面量假客户，并去掉依赖资料袋的地址页。"""
    overlays: dict[str, dict[str, object]] = {
        "/register-for-vat/application-reference": {"value": f"TEST-{suffix}"},
        "/overseas-identifier": {
            "tax-identifier-radio": "Yes",
            "tax-identifier": f"TEST-{suffix}-TAX",
        },
        "/identify-your-sole-trader-business/": {
            "first-name": "Alex",
            "last-name": f"Tester{suffix}",
        },
        "/date-of-birth": {
            "date-of-birth.day": str(birth_date.day),
            "date-of-birth.month": str(birth_date.month),
            "date-of-birth.year": str(birth_date.year),
            "Day": str(birth_date.day),
            "Month": str(birth_date.month),
            "Year": str(birth_date.year),
        },
        "/register-for-vat/email-address": {"email-address": "env:HMRC_EMAIL"},
        "/register-for-vat/telephone-number": {"telephone-number": "env:HMRC_MFA_PHONE"},
        "/register-for-vat/business-email": {"businessEmailAddress": "env:HMRC_EMAIL"},
        "/register-for-vat/business-telephone-number": {
            "daytimePhone": "env:HMRC_MFA_PHONE"
        },
    }
    skip_document_address = {
        "/register-for-vat/home-address/international",
        "/register-for-vat/principal-place-business/international",
    }
    result: list[dict[str, object]] = []
    for rule in flow_page_rules():
        match = rule.get("match", {})
        path = str(match.get("path_contains", "")) if isinstance(match, dict) else ""
        if path in skip_document_address:
            continue
        item = deepcopy(rule)
        if path in overlays:
            item["answers"] = dict(overlays[path])
        result.append(item)
    return result


def build_flow_config() -> dict[str, object]:
    return {
        "start_url": "https://www.gov.uk/log-in-register-hmrc-online-services",
        "profile_dir": ".browser-profile",
        "artifacts_dir": "artifacts",
        "browser_channel": "chrome",
        "headless": False,
        "allow_live_application": False,
        "max_steps": 250,
        "answers": flow_global_answers(),
        "pages": flow_page_rules(),
    }


SPECIAL_HANDLED_PATHS = (
    "/log-in-register-hmrc-online-services",
    "/sign-in-to-hmrc-online-services/identity/",
    "/registration/",
    "/multi-factor/",
    "/register-for-vat/application-progress",
    "/register-for-vat/email-address-verification",
    "/register-for-vat/email-address-verified",
    "/register-for-vat/identity-documents-required",
    "/register-for-vat/file-upload/summary",
    "/check-your-answers-business",
    "/mtd-mandatory-information",
    "/35-character-limit",
    "/choose-standard-industry-classification-codes",
)

GENERIC_ANSWER_PATHS = (
    "/register-for-vat/home-address/international",
    "/register-for-vat/principal-place-business/international",
    "/register-for-vat/business-name",
    "/register-for-vat/business-description",
)

SCREENSHOT_PATHS = (
    "/log-in-register-hmrc-online-services",
    "/sign-in-to-hmrc-online-services/identity/sign-in/",
    "/sign-in-to-hmrc-online-services/identity/tax-agent/",
    "/sign-in-to-hmrc-online-services/identity/organisation/",
    "/registration/email", "/registration/code", "/registration/valid-email",
    "/registration/name", "/registration/password", "/registration/confirmation/",
    "/multi-factor/setup-security/", "/multi-factor/choose-method/",
    "/multi-factor/mobile-number-uk/", "/multi-factor/enter-mobile-country/",
    "/multi-factor/enter-mobile-number/", "/multi-factor/validate/",
    "/multi-factor/setup-complete/", "/business-account",
    "/business-account/add-tax/vat", "/business-account/add-tax/vat/interstitial",
    "/business-account/add-tax/vat/do-you-have-a-vat-number",
    "/register-for-vat/application-reference", "/register-for-vat/honesty-declaration",
    "/check-if-you-can-register-for-vat/fixed-establishment",
    "/check-if-you-can-register-for-vat/business-entity-overseas",
    "/check-if-you-can-register-for-vat/agricultural-flat-rate",
    "/check-if-you-can-register-for-vat/business-activities-next-12-months",
    "/check-if-you-can-register-for-vat/whos-the-application-for",
    "/check-if-you-can-register-for-vat/registration-reason",
    "/check-if-you-can-register-for-vat/made-or-intend-to-make-taxable-supplies",
    "/check-if-you-can-register-for-vat/date-of-taxable-supplies-in-uk",
    "/check-if-you-can-register-for-vat/mtd-mandatory-information",
    "/register-for-vat/application-progress",
    "/identify-your-overseas-business/{id}/non-uk-company-utr",
    "/identify-your-overseas-business/{id}/overseas-identifier",
    "/identify-your-overseas-business/{id}/overseas-tax-identifier-country",
    "/identify-your-overseas-business/{id}/check-your-answers-business",
    "/identify-your-sole-trader-business/{id}/full-name",
    "/identify-your-sole-trader-business/{id}/date-of-birth",
    "/identify-your-sole-trader-business/{id}/national-insurance-number",
    "/identify-your-sole-trader-business/{id}/check-your-answers-business",
    "/register-for-vat/role-in-the-business", "/register-for-vat/changed-name",
    "/register-for-vat/home-address/international", "/register-for-vat/current-address",
    "/register-for-vat/email-address", "/register-for-vat/email-address-verification",
    "/register-for-vat/email-address-verified", "/register-for-vat/telephone-number",
    "/register-for-vat/business-name", "/register-for-vat/confirm-trading-name",
    "/register-for-vat/35-character-limit",
    "/register-for-vat/principal-place-business/international",
    "/register-for-vat/business-email", "/register-for-vat/business-telephone-number",
    "/register-for-vat/business-has-website",
    "/register-for-vat/vat-correspondence-language", "/register-for-vat/contact-preference",
    "/register-for-vat/land-and-property", "/register-for-vat/business-description",
    "/register-for-vat/choose-standard-industry-classification-codes",
    "/sic-search/{id}/search-standard-industry-classification-codes",
    "/sic-search/{id}/check-confirm-standard-industry-classification-codes",
    "/register-for-vat/other-business-involvements", "/register-for-vat/imports-or-exports",
    "/register-for-vat/apply-for-eori", "/register-for-vat/standard-rate-turnover",
    "/register-for-vat/reduced-rate-turnover", "/register-for-vat/zero-rated-turnover",
    "/register-for-vat/total-taxable-turnover", "/register-for-vat/sell-or-move-nip",
    "/register-for-vat/receive-goods-nip", "/register-for-vat/claim-vat-refunds",
    "/register-for-vat/send-goods-overseas", "/register-for-vat/storing-goods-for-dispatch",
    "/register-for-vat/using-fulfilment-warehouse",
    "/register-for-vat/how-often-submit-returns", "/register-for-vat/submit-vat-returns",
    "/register-for-vat/tax-representative", "/register-for-vat/join-flat-rate",
    "/register-for-vat/identity-documents-required", "/register-for-vat/attachment-method",
    "/register-for-vat/file-upload/upload-document", "/register-for-vat/file-upload/summary",
)
=======
from __future__ import annotations

from datetime import date


def _rule(
    path: str,
    *,
    default: object | None = None,
    answers: dict[str, object] | None = None,
    action: str = "continue",
) -> dict[str, object]:
    rule: dict[str, object] = {
        "match": {"path_contains": path},
        "answers": answers or {},
        "action": action,
    }
    if default is not None:
        rule["default_answer"] = default
    return rule


def screenshot_page_rules(
    *, suffix: str, birth_date: date
) -> list[dict[str, object]]:
    """截图所示海外企业 VAT 路径的测试答案，最终提交仍不自动化。"""
    return [
        {
            "match": {
                "path_contains": "/business-account",
                "heading_contains": "Add a tax to your account",
            },
            "answers": {},
            "default_answer": "VAT",
            "action": "continue",
        },
        _rule("/business-account/add-tax/vat", default="VAT"),
        _rule("/business-account/add-tax/vat/do-you-have-a-vat-number", default="No"),
        _rule(
            "/register-for-vat/application-reference",
            answers={"value": f"TEST-{suffix}"},
        ),
        _rule("/register-for-vat/honesty-declaration"),
        _rule("/check-if-you-can-register-for-vat/fixed-establishment", default="false"),
        _rule("/check-if-you-can-register-for-vat/business-entity-overseas", default="55"),
        _rule("/check-if-you-can-register-for-vat/agricultural-flat-rate", default="false"),
        _rule("/check-if-you-can-register-for-vat/business-activities-next-12-months", default="false"),
        _rule("/check-if-you-can-register-for-vat/whos-the-application-for", default="own"),
        _rule("/check-if-you-can-register-for-vat/registration-reason", default="selling-goods-and-services"),
        _rule("/check-if-you-can-register-for-vat/made-or-intend-to-make-taxable-supplies", default="true"),
        _rule(
            "/check-if-you-can-register-for-vat/date-of-taxable-supplies-in-uk",
            answers={
                "thresholdTaxableSuppliesDate.day": "date:uk-next-month-first.day",
                "thresholdTaxableSuppliesDate.month": "date:uk-next-month-first.month",
                "thresholdTaxableSuppliesDate.year": "date:uk-next-month-first.year",
                "Day": "date:uk-next-month-first.day",
                "Month": "date:uk-next-month-first.month",
                "Year": "date:uk-next-month-first.year",
            },
        ),
        _rule("/non-uk-company-utr", action="skip:I do not have the company's UTR number"),
        _rule(
            "/overseas-identifier",
            answers={
                "tax-identifier-radio": "Yes",
                "tax-identifier": f"TEST-{suffix}-TAX",
            },
        ),
        _rule("/overseas-tax-identifier-country", answers={"countryAutocomplete": "China", "country": "China"}),
        _rule(
            "/identify-your-sole-trader-business/",
            answers={
                "first-name": "Alex",
                "last-name": f"Tester{suffix}",
            },
        ),
        _rule(
            "/date-of-birth",
            answers={
                "date-of-birth.day": str(birth_date.day),
                "date-of-birth.month": str(birth_date.month),
                "date-of-birth.year": str(birth_date.year),
                "Day": str(birth_date.day),
                "Month": str(birth_date.month),
                "Year": str(birth_date.year),
            },
        ),
        _rule("/national-insurance-number", action="skip:I do not have a National Insurance number"),
        _rule("/register-for-vat/role-in-the-business", default="director"),
        _rule("/register-for-vat/changed-name", default="false"),
        _rule("/register-for-vat/current-address", default="true"),
        _rule("/register-for-vat/email-address", answers={"email-address": "env:HMRC_EMAIL"}),
        _rule("/register-for-vat/telephone-number", answers={"telephone-number": "env:HMRC_MFA_PHONE"}),
        _rule("/register-for-vat/confirm-trading-name", default="true"),
        _rule("/register-for-vat/business-email", answers={"businessEmailAddress": "env:HMRC_EMAIL"}),
        _rule("/register-for-vat/business-telephone-number", answers={"daytimePhone": "env:HMRC_MFA_PHONE"}),
        _rule("/register-for-vat/business-has-website", default="false"),
        _rule("/register-for-vat/vat-correspondence-language", default="english"),
        _rule("/register-for-vat/contact-preference", default="email"),
        _rule("/register-for-vat/land-and-property", default="false"),
        _rule(
            "/search-standard-industry-classification-codes",
            answers={"sicSearch": "47910"},
            action="Search",
        ),
        _rule(
            "/check-confirm-standard-industry-classification-codes",
            action="Confirm and continue",
        ),
        _rule("/register-for-vat/other-business-involvements", default="false"),
        _rule("/register-for-vat/imports-or-exports", default="true"),
        _rule("/register-for-vat/apply-for-eori", default="true"),
        _rule("/register-for-vat/standard-rate-turnover", answers={"standardRateSupplies": "10000"}),
        _rule("/register-for-vat/reduced-rate-turnover", answers={"reducedRateSupplies": "0"}),
        _rule("/register-for-vat/zero-rated-turnover", answers={"zeroRatedSupplies": "0"}),
        _rule("/register-for-vat/total-taxable-turnover", default="true"),
        _rule("/register-for-vat/sell-or-move-nip", default="false"),
        _rule("/register-for-vat/receive-goods-nip", default="false"),
        _rule("/register-for-vat/claim-vat-refunds", default="false"),
        _rule("/register-for-vat/send-goods-overseas", default="false"),
        _rule("/register-for-vat/storing-goods-for-dispatch", default="UK"),
        _rule("/register-for-vat/using-fulfilment-warehouse", default="false"),
        _rule("/register-for-vat/how-often-submit-returns", default="quarterly"),
        _rule(
            "/register-for-vat/submit-vat-returns",
            default="vat-return-stagger:uk-next-month-first",
        ),
        _rule("/register-for-vat/tax-representative", default="false"),
        _rule("/register-for-vat/join-flat-rate", default="false"),
        _rule("/register-for-vat/attachment-method", default="2"),
        _rule("/register-for-vat/file-upload/upload-document", action="stop"),
    ]


SPECIAL_HANDLED_PATHS = (
    "/log-in-register-hmrc-online-services",
    "/sign-in-to-hmrc-online-services/identity/",
    "/registration/",
    "/multi-factor/",
    "/register-for-vat/application-progress",
    "/register-for-vat/email-address-verification",
    "/register-for-vat/email-address-verified",
    "/register-for-vat/identity-documents-required",
    "/register-for-vat/file-upload/summary",
    "/check-your-answers-business",
    "/mtd-mandatory-information",
    "/35-character-limit",
    "/choose-standard-industry-classification-codes",
)

GENERIC_ANSWER_PATHS = (
    "/register-for-vat/home-address/international",
    "/register-for-vat/principal-place-business/international",
    "/register-for-vat/business-name",
    "/register-for-vat/business-description",
)

SCREENSHOT_PATHS = (
    "/log-in-register-hmrc-online-services",
    "/sign-in-to-hmrc-online-services/identity/sign-in/",
    "/sign-in-to-hmrc-online-services/identity/tax-agent/",
    "/sign-in-to-hmrc-online-services/identity/organisation/",
    "/registration/email", "/registration/code", "/registration/valid-email",
    "/registration/name", "/registration/password", "/registration/confirmation/",
    "/multi-factor/setup-security/", "/multi-factor/choose-method/",
    "/multi-factor/mobile-number-uk/", "/multi-factor/enter-mobile-country/",
    "/multi-factor/enter-mobile-number/", "/multi-factor/validate/",
    "/multi-factor/setup-complete/", "/business-account",
    "/business-account/add-tax/vat", "/business-account/add-tax/vat/interstitial",
    "/business-account/add-tax/vat/do-you-have-a-vat-number",
    "/register-for-vat/application-reference", "/register-for-vat/honesty-declaration",
    "/check-if-you-can-register-for-vat/fixed-establishment",
    "/check-if-you-can-register-for-vat/business-entity-overseas",
    "/check-if-you-can-register-for-vat/agricultural-flat-rate",
    "/check-if-you-can-register-for-vat/business-activities-next-12-months",
    "/check-if-you-can-register-for-vat/whos-the-application-for",
    "/check-if-you-can-register-for-vat/registration-reason",
    "/check-if-you-can-register-for-vat/made-or-intend-to-make-taxable-supplies",
    "/check-if-you-can-register-for-vat/date-of-taxable-supplies-in-uk",
    "/check-if-you-can-register-for-vat/mtd-mandatory-information",
    "/register-for-vat/application-progress",
    "/identify-your-overseas-business/{id}/non-uk-company-utr",
    "/identify-your-overseas-business/{id}/overseas-identifier",
    "/identify-your-overseas-business/{id}/overseas-tax-identifier-country",
    "/identify-your-overseas-business/{id}/check-your-answers-business",
    "/identify-your-sole-trader-business/{id}/full-name",
    "/identify-your-sole-trader-business/{id}/date-of-birth",
    "/identify-your-sole-trader-business/{id}/national-insurance-number",
    "/identify-your-sole-trader-business/{id}/check-your-answers-business",
    "/register-for-vat/role-in-the-business", "/register-for-vat/changed-name",
    "/register-for-vat/home-address/international", "/register-for-vat/current-address",
    "/register-for-vat/email-address", "/register-for-vat/email-address-verification",
    "/register-for-vat/email-address-verified", "/register-for-vat/telephone-number",
    "/register-for-vat/business-name", "/register-for-vat/confirm-trading-name",
    "/register-for-vat/35-character-limit",
    "/register-for-vat/principal-place-business/international",
    "/register-for-vat/business-email", "/register-for-vat/business-telephone-number",
    "/register-for-vat/business-has-website",
    "/register-for-vat/vat-correspondence-language", "/register-for-vat/contact-preference",
    "/register-for-vat/land-and-property", "/register-for-vat/business-description",
    "/register-for-vat/choose-standard-industry-classification-codes",
    "/sic-search/{id}/search-standard-industry-classification-codes",
    "/sic-search/{id}/check-confirm-standard-industry-classification-codes",
    "/register-for-vat/other-business-involvements", "/register-for-vat/imports-or-exports",
    "/register-for-vat/apply-for-eori", "/register-for-vat/standard-rate-turnover",
    "/register-for-vat/reduced-rate-turnover", "/register-for-vat/zero-rated-turnover",
    "/register-for-vat/total-taxable-turnover", "/register-for-vat/sell-or-move-nip",
    "/register-for-vat/receive-goods-nip", "/register-for-vat/claim-vat-refunds",
    "/register-for-vat/send-goods-overseas", "/register-for-vat/storing-goods-for-dispatch",
    "/register-for-vat/using-fulfilment-warehouse",
    "/register-for-vat/how-often-submit-returns", "/register-for-vat/submit-vat-returns",
    "/register-for-vat/tax-representative", "/register-for-vat/join-flat-rate",
    "/register-for-vat/identity-documents-required", "/register-for-vat/attachment-method",
    "/register-for-vat/file-upload/upload-document", "/register-for-vat/file-upload/summary",
)
>>>>>>> Stashed changes
