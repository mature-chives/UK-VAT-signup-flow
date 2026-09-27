"""英国 EORI 号注册流程（materials/英国EORI注册流程）的配置生成器。

与 screenshot_flow.py 的 VAT 流程分开：EORI 走 customs-registration-services
服务，页面更少，地址栏只有两行街道地址，且不需要上传身份证明。
客户真值一律用 doc: / env: 占位符，不写进本文件。
"""

from __future__ import annotations


EORI_START_URL = "https://www.gov.uk/eori/apply-for-eori"
EORI_GUIDE_URL = "https://www.gov.uk/eori"
EORI_SERVICE_ROOT = "/customs-registration-services/eori-only"
EORI_REGISTER_PATH = f"{EORI_SERVICE_ROOT}/register"
# customs 通知邮箱：使用人工核对过的资料字段，不自动复用登录邮箱。
EORI_EMAIL_ANSWER = "doc:vat_contact_email"
# VAT 注册地址邮编：优先用结构化地址里的 postcode，退回授权表的原始邮编。
EORI_VAT_POSTCODE_ANSWER = "doc:postcode|doc:business_postcode"


def _rule(
    path: str = "",
    *,
    heading: str = "",
    default: object | None = None,
    answers: dict[str, object] | None = None,
    action: str = "continue",
    address: str | None = None,
    address_format: str | None = None,
) -> dict[str, object]:
    match: dict[str, str] = {}
    if path:
        match["path_contains"] = path
    if heading:
        match["heading_contains"] = heading
    rule: dict[str, object] = {
        "match": match,
        "answers": answers or {},
        "action": action,
    }
    if default is not None:
        rule["default_answer"] = default
    if address is not None:
        rule["address"] = address
    if address_format is not None:
        rule["address_format"] = address_format
    return rule


def eori_global_answers() -> dict[str, str]:
    """全局答案仅含申请人资料；地址国家与认证手机号国家分别处理。"""
    return {
        "Business name": "doc:business_name",
        "Full name": "doc:full_name",
        "Telephone": "doc:phone",
    }


def eori_page_rules() -> list[dict[str, object]]:
    """EORI 注册流程表，顺序与 materials/ 截图一致。

    已知页面路径用 path_contains；截图里看不出路径的页面按页面标题匹配，
    后写的规则优先。
    """
    established = {
        "Day": "doc:company-incorporation-date.day",
        "Month": "doc:company-incorporation-date.month",
        "Year": "doc:company-incorporation-date.year",
        "date-established.day": "doc:company-incorporation-date.day",
        "date-established.month": "doc:company-incorporation-date.month",
        "date-established.year": "doc:company-incorporation-date.year",
    }
    vat_registered = {
        "Day": "doc:vat-registration-date.day",
        "Month": "doc:vat-registration-date.month",
        "Year": "doc:vat-registration-date.year",
        "vat-registered-date.day": "doc:vat-registration-date.day",
        "vat-registered-date.month": "doc:vat-registration-date.month",
        "vat-registered-date.year": "doc:vat-registration-date.year",
    }
    return [
        # 自定义通知邮箱 → 确认邮箱提示 → 邮箱验证码（验证码由人工输入）。
        # 路径和标题各留一条规则：HMRC 改文案时路径仍能兜住。
        _rule(
            heading="What email address can we use for customs notifications",
            answers={
                "What email address can we use for customs notifications?": EORI_EMAIL_ANSWER,
                "Email address": EORI_EMAIL_ANSWER,
                "email-address": EORI_EMAIL_ANSWER,
            },
        ),
        _rule(
            f"{EORI_REGISTER_PATH}/matching/what-is-your-email",
            answers={
                "What email address can we use for customs notifications?": EORI_EMAIL_ANSWER,
                "Email address": EORI_EMAIL_ANSWER,
                "email-address": EORI_EMAIL_ANSWER,
            },
        ),
        _rule(heading="the email address you want to use", default="Yes"),
        _rule(f"{EORI_REGISTER_PATH}/matching/check-your-email", default="Yes"),
        # 登录后先问是否属于英国 VAT group（截图 image3），海外公司固定 No
        _rule(heading="part of a VAT group", default="No"),
        _rule(f"{EORI_REGISTER_PATH}/vat-group", default="No"),
        _rule(
            f"{EORI_REGISTER_PATH}/matching/user-location",
            default="Rest of the world",
        ),
        _rule(
            f"{EORI_REGISTER_PATH}/matching/organisation-type",
            default="Organisation",
        ),
        _rule(
            f"{EORI_REGISTER_PATH}/matching/name/third-country-organisation",
            default="doc:business_name",
        ),
        _rule(
            f"{EORI_REGISTER_PATH}/matching/utr/third-country-organisation",
            default="No",
        ),
        _rule(
            f"{EORI_REGISTER_PATH}/matching/address/third-country-organisation",
            address="doc:business",
            address_format="eori",
        ),
        _rule(f"{EORI_REGISTER_PATH}/date-established", answers=established),
        # SIC 与 VAT 注册保持一致：47910 网上零售
        _rule(f"{EORI_REGISTER_PATH}/sic-code", default="47910"),
        _rule(
            f"{EORI_REGISTER_PATH}/disclose-personal-details-consent",
            default="No",
        ),
        _rule(f"{EORI_REGISTER_PATH}/vat-registered-uk", default="Yes"),
        # 英国 VAT 证书信息页：号码与注册邮编来自 VAT 证书，日期来自注册生效日期
        _rule(
            heading="Your UK VAT details",
            answers={
                "What is your VAT registration number?": "doc:vat_number",
                "VAT registration number": "doc:vat_number",
                "vatNumber": "doc:vat_number",
                "vat-number": "doc:vat_number",
                "What is the postcode where your organisation is registered for VAT?": EORI_VAT_POSTCODE_ANSWER,
                "Postcode of your VAT registration address": EORI_VAT_POSTCODE_ANSWER,
                "vat-postcode": EORI_VAT_POSTCODE_ANSWER,
            },
        ),
        _rule(heading="When did you become VAT registered", answers=vat_registered),
        _rule(
            heading="EORI number application contact details",
            answers={
                "Telephone": "doc:phone",
                "Telephone number": "doc:phone",
                "telephone": "doc:phone",
            },
        ),
        _rule(
            heading="Do you want us to use this address to send you information",
            default="Yes",
        ),
    ]


def build_eori_flow_config() -> dict[str, object]:
    """EORI 流程配置：Web/工作台运行时直接读这份配置，不含客户真值。"""
    return {
        "flow_name": "英国 EORI 注册",
        "start_url": EORI_START_URL,
        # 默认用已有 Government Gateway 账号登录；没有账号时可在网页切换为建号。
        "default_sign_in_method": "Government Gateway",
        "profile_dir": ".browser-profile-eori",
        "artifacts_dir": "artifacts-eori",
        "browser_channel": "chrome",
        "headless": False,
        # 与 VAT 流程一致：真实客户资料由人工在最终核对页确认后才提交。
        "allow_live_application": True,
        # EORI 不需要上传身份证明文件。
        "identity_documents_required": 0,
        # 出错时停留 10 分钟，等网页上的人工决定（继续 / 取消）。
        "error_hold_seconds": 600,
        "max_steps": 80,
        "answers": eori_global_answers(),
        "pages": eori_page_rules(),
    }


# 截图里能确认路径的 EORI 页面（用于覆盖率检查）。
EORI_SCREENSHOT_PATHS: tuple[str, ...] = (
    f"{EORI_REGISTER_PATH}/vat-group",
    f"{EORI_REGISTER_PATH}/matching/what-is-your-email",
    f"{EORI_REGISTER_PATH}/matching/check-your-email",
    f"{EORI_REGISTER_PATH}/matching/user-location",
    f"{EORI_REGISTER_PATH}/matching/organisation-type",
    f"{EORI_REGISTER_PATH}/matching/name/third-country-organisation",
    f"{EORI_REGISTER_PATH}/matching/utr/third-country-organisation",
    f"{EORI_REGISTER_PATH}/matching/address/third-country-organisation",
    f"{EORI_REGISTER_PATH}/date-established",
    f"{EORI_REGISTER_PATH}/sic-code",
    f"{EORI_REGISTER_PATH}/disclose-personal-details-consent",
    f"{EORI_REGISTER_PATH}/vat-registered-uk",
    f"{EORI_REGISTER_PATH}/review-details",
)

# 截图能看到标题、但看不到路径的页面，按标题匹配。
EORI_SCREENSHOT_HEADINGS: tuple[str, ...] = (
    "What email address can we use for customs notifications?",
    # 该页标题里带客户邮箱，只能用不随客户变化的后半句匹配。
    "the email address you want to use",
    "Is your organisation part of a VAT group in the UK?",
    "Your UK VAT details",
    "When did you become VAT registered?",
    "EORI number application contact details",
    "Do you want us to use this address to send you information about your EORI number application?",
)

# 由 runner 或入口流程直接处理，不需要页面规则。
EORI_SPECIAL_HANDLED_PATHS: tuple[str, ...] = (
    EORI_GUIDE_URL,
    EORI_START_URL,
    "/sign-in-to-hmrc-online-services/identity/",
    "/multi-factor/",
    "/email-verification/",
    f"{EORI_REGISTER_PATH}/review-details",
)
