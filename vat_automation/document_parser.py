from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from collections.abc import Mapping
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from .company_identity import normalized_identity_values
from .countries import address_country, infer_address_country, normalize_country, split_address_country


FIELD_DEFINITIONS: dict[str, tuple[str, tuple[str, ...]]] = {
    "project_code": ("项目编号（来自文件名）", ()),
    "application_reference": ("HMRC申请参考名称", ()),
    "title": ("称谓", ("称谓（女士/先生）", "title")),
    "business_name": (
        "公司正式名称",
        (
            "business name", "official business name", "company name",
            "公司名称", "企业名称",
            "公司名称（拼音或英文名称，要跟账号后台注册名称一致）",
        ),
    ),
    "trading_name": ("交易名称", ("trading name", "trade name", "交易名称", "营业名称")),
    "full_name": ("法人/负责人姓名", ("full name", "applicant name", "director name", "法人姓名", "姓名", "负责人姓名")),
    "first_name": ("名", ("first name", "given name", "名")),
    "last_name": ("姓", ("last name", "surname", "family name", "姓")),
    "email": (
        "个人电子邮箱",
        ("email", "email address", "邮箱", "电子邮箱", "个人邮箱", "个人电子邮箱"),
    ),
    "phone": ("个人手机号", ("phone", "mobile", "telephone number", "mobile number", "手机号码", "手机号", "联系电话")),
    "business_description": (
        "业务描述",
        ("business description", "what does the business do", "goods or services", "业务描述", "业务描述（用英文表述）", "经营范围", "销售产品名称（列举1-2个主要产品即可）"),
    ),
    "business_description_original": ("原始业务描述", ()),
    "estimated_turnover": (
        "预计应税营业额",
        ("estimated taxable turnover", "taxable turnover", "预计应税营业额", "预计营业额", "预估之后连续12个月的总营业额（英镑，大约预估）"),
    ),
    "birth_date": ("出生日期", ("date of birth", "birth date", "出生日期")),
    "premises": (
        "公司地址第1行（英文）",
        ("premises", "address line 1", "room and building", "房间楼宇", "地址第一行"),
    ),
    "street": ("公司地址第2行（英文）", ("street", "address line 2", "street address", "街道地址", "地址第二行")),
    "locality": ("公司地址第3行（英文，选填）", ("locality", "district", "区", "街道")),
    "city": ("公司地址第4行（英文，选填）", ("city", "town or city", "城市")),
    "region": ("公司地址第5行（英文，选填）", ("region", "province", "state", "省", "地区")),
    "postcode": ("公司地址邮编", ("postcode", "postal code", "zip code", "邮编")),
    "country": ("公司地址国家（英文）", ("country", "国家", "公司地址国家", "business address country")),
    "home_premises": ("居住地址第1行（英文）", ()),
    "home_street": ("居住地址第2行（英文）", ()),
    "home_locality": ("居住地址第3行（英文，选填）", ()),
    "home_city": ("居住地址第4行（英文，选填）", ()),
    "home_region": ("居住地址第5行（英文，选填）", ()),
    "home_postcode": ("居住地址邮编", ()),
    "home_country": ("居住地址国家（英文）", ("居住国家", "居住地址国家", "home country", "country of residence")),
    "previous_name": ("曾用名", ("曾用名（如有）", "previous name")),
    "name_change_date": ("现用名更改时间", ("现用名更改时间（如涉及）",)),
    "occupation": ("职业", ("职业", "occupation")),
    "nationality_visa": ("国籍及签证类型", ("国籍以及签证类型（如涉及签证）", "nationality")),
    "china_visa_expiry": ("中国签证有效期", ("中国签证有效期（如适用）",)),
    "identity_document_number": ("护照/身份证号码", ("护照或身份证号码, 如有", "passport number", "identity card number")),
    "residential_address": ("现居住地址及邮编", ("现居住地址以及邮编（需提供相应的2份证明材料）",)),
    "residential_since": ("现地址起始居住时间", ("什么时候开始在以上地址居住的",)),
    "previous_residential_address": ("过去3年其他地址", ("如果以上地址居住不满3年，请填写3年间的地址和居住时间",)),
    "former_adviser": ("上一任会计师/税务师", ("上一任会计师/税务师的姓名和联系方式（请提供姓名/电话/Email）",)),
    "company_registration_number": (
        "公司注册号/统一社会信用代码",
        (
            "公司注册号（统一社会信用代码）", "公司注册号", "company registration number",
            "海外税务识别号", "海外税号", "税务识别号", "中国纳税人识别号，如有",
            "overseas tax identifier", "tax identifier", "tax id",
        ),
    ),
    "company_identifier_type": ("公司编号类型", ("公司编号类型", "identifier type")),
    "_uscc": ("统一社会信用代码", ("统一社会信用代码", "unified social credit code", "USCC")),
    "_brn": ("香港商业登记号码 BRN", ("BRN", "business registration number", "商业登记号码", "商業登記號碼", "商业登记证号码", "商業登記證號碼")),
    "_crn": ("香港旧公司注册编号", ("CRN", "CR No.", "香港公司注册编号", "香港公司註冊編號")),
    "company_registration_country": ("公司注册国家/地区", ("公司注册国家（请根据实际情况填写）", "公司注册国家", "公司注册地", "注册司法辖区", "country of incorporation")),
    "company_incorporation_date": ("公司成立日期", ("公司成立日期",)),
    "business_address": (
        "公司注册地址",
        (
            "公司注册地址（与Amazon或Ebay等在线平台注册地址一致，如地址是中文，则用对应英文 或拼音表示）",
            "公司注册地址（与Amazon或Ebay等在线平台注册地址一致，如地址是中文，则用对应英文或拼音表示）",
        ),
    ),
    "business_postcode": ("公司注册地址邮编", ("公司注册地址邮编",)),
    "business_type": ("营业类型", ("营业类型（贸易/物流/金融/数字货币交易/IT/咨询/旅游/建筑/房产/餐饮/法律 等）",)),
    "is_small_business": ("是否小微企业", ("公司是否属于小微企业",)),
    "vat_number": (
        "英国VAT号",
        (
            "vat number", "vat registration number", "uk vat number",
            "英国VAT号", "VAT注册号", "VAT号码", "VAT税号", "增值税号",
        ),
    ),
    "vat_registration_date": (
        "英国VAT注册生效日期",
        (
            "vat registration date", "date of vat registration",
            "vat effective date", "VAT注册日期", "VAT生效日期", "VAT注册生效日期",
        ),
    ),
    "vat_contact_email": ("VAT沟通邮箱", ("注册VAT的沟通邮箱",)),
    "business_phone": ("生意联系电话", ("生意联系电话",)),
    "sales_platform": ("销售平台", ("销售平台（例如Amazon、Ebay等）",)),
    "amazon_store_url": ("亚马逊前台店铺链接", ("亚马逊需提供前台店铺链接",)),
    "seller_token": ("卖家记号", ("卖家记号",)),
    "first_uk_trade_date": ("平台英国第一笔贸易时间", ("平台英国第一笔贸易时间（如果还未开始FBA销售，留空；如已开始，注册英国FBA销售日期）",)),
    "vat_scheme": ("VAT税率方案", ("注册VAT税率方案（Flat VAT还是Standard VAT）",)),
}

ANSWER_MAPPING = {
    "business_name": "Business name",
    "trading_name": "Trading name",
    "full_name": "Full name",
    "first_name": "First name",
    "last_name": "Last name",
    "email": "Email address",
    "phone": "Telephone number",
    "business_description": "What does the business do?",
}
ADDRESS_KEYS = {"premises", "street", "locality", "city", "region", "postcode", "country"}
SUPPORTED_SUFFIXES = {".pdf", ".docx", ".xlsx", ".txt", ".json", ".csv", ".tsv"}
# 资料袋里的日期字段 → HMRC 页面上的 day/month/year 字段前缀。
DOCUMENT_DATE_FIELDS: tuple[tuple[str, str], ...] = (
    ("company_incorporation_date", "company-incorporation-date"),
    ("vat_registration_date", "vat-registration-date"),
)
VAT_REGISTRATION_FIELD_KEYS = (
    "project_code", "application_reference",
    "full_name", "first_name", "last_name", "birth_date",
    "email", "phone", "residential_address",
    "home_premises", "home_street", "home_locality", "home_city",
    "home_region", "home_postcode", "home_country", "business_name",
    "trading_name", "company_registration_number",
    "company_registration_country", "company_identifier_type", "company_incorporation_date",
    "business_address", "business_postcode",
    "premises", "street", "locality", "city", "region", "postcode",
    "country",
    "vat_contact_email", "business_phone", "business_description", "business_description_original",
    "vat_number", "vat_registration_date",
)


def extract_document(filename: str, content: bytes) -> dict[str, Any]:
    suffix = Path(filename).suffix.casefold()
    if suffix not in SUPPORTED_SUFFIXES:
        raise ValueError(
            "资料文档仅支持 PDF、DOCX、XLSX、TXT、JSON、CSV 或 TSV。"
        )
    if not content:
        raise ValueError("资料文档为空。")

    warnings: list[str] = []
    credentials: dict[str, str] = {}
    if suffix == ".xlsx":
        raw_values, warnings, credentials = _xlsx_values(content)
    else:
        raw_values = _parse_values(_extract_text(suffix, content))
    parsed_values = _derive_values(raw_values)
    if raw_values.get("_uscc") and raw_values.get("_brn"):
        warnings.append("资料同时含内地统一社会信用代码及香港 BRN，请核对当前办理公司，不能自动选取其一。")
    project_code = _extract_project_code(filename)
    if project_code:
        parsed_values["project_code"] = project_code
        business_name = parsed_values.get("business_name", "").strip()
        if business_name:
            parsed_values["application_reference"] = (
                f"{project_code}-UK-{business_name}"
            )
    values = {
        key: parsed_values[key]
        for key in VAT_REGISTRATION_FIELD_KEYS
        if parsed_values.get(key)
    }
    for required in ("business_name", "full_name", "email", "phone"):
        if not values.get(required):
            warnings.append(f"未识别到{FIELD_DEFINITIONS[required][0]}，请人工补充。")
    if not any(values.get(key) for key in ADDRESS_KEYS):
        warnings.append("未识别到结构化地址，请人工补充地址字段。")
    for raw_key, key in (("business_address", "country"), ("residential_address", "home_country")):
        detected = address_country(parsed_values.get(raw_key, ""))
        if not values.get(key):
            warnings.append(f"未能从资料判断{FIELD_DEFINITIONS[key][0]}，请补充。")
        elif detected and detected != normalize_country(values[key]):
            warnings.append(f"{FIELD_DEFINITIONS[key][0]}与原始地址中的国家不一致，请人工核对。")
    if raw_values.get("vat_number") and not re.fullmatch(
        r"[0-9]{9}", parsed_values.get("vat_number", "")
    ):
        warnings.append("英国 VAT 号去除 GB 前缀和空格后应为 9 位数字，请人工核对。")

    return {
        "values": values,
        "labels": {
            key: FIELD_DEFINITIONS[key][0] for key in VAT_REGISTRATION_FIELD_KEYS
        },
        "warnings": warnings,
        # GG 号只交给登录区，不进资料袋、流程配置或普通客户字段。
        "credentials": credentials,
    }


def extracted_answers(values: dict[str, str]) -> dict[str, str]:
    return {
        answer: values[key]
        for key, answer in ANSWER_MAPPING.items()
        if values.get(key)
    }


def extracted_address(values: dict[str, str]) -> dict[str, str]:
    return {key: values[key] for key in ADDRESS_KEYS if values.get(key)}


def extracted_home_address(values: dict[str, str]) -> dict[str, str]:
    return {
        key.removeprefix("home_"): value
        for key, value in values.items()
        if key.startswith("home_") and value
    }


def prepare_document_values(values: Mapping[str, str]) -> dict[str, str]:
    """网页确认后的提取结果 → 填表资料袋。不含密码，也不写入流程 JSON。"""
    bag = {
        key: str(value).strip()
        for key, value in values.items()
        if str(value).strip()
    }
    bag = normalized_identity_values(bag)
    if bag.get("vat_number"):
        bag["vat_number"] = normalize_vat_number(bag["vat_number"])
        if not re.fullmatch(r"[0-9]{9}", bag["vat_number"]):
            raise ValueError("英国 VAT 号应为 9 位数字（不含 GB 前缀），请核对后再注册。")
    bag.update(extracted_birth_date(bag))
    for source, prefix in DOCUMENT_DATE_FIELDS:
        bag.update(extracted_date_parts(bag.get(source, ""), prefix))
    for key in ("country", "home_country", "company_registration_country"):
        if key in bag:
            bag[key] = normalize_country(bag[key])
    # 业务规则：海外税号直接填写公司注册号，不另设有无税号选项。
    bag.pop("tax-identifier-radio", None)
    bag.pop("tax-identifier", None)
    identifier = bag.get("company_registration_number", "")
    if identifier:
        bag["tax-identifier-radio"] = "Yes"
        bag["tax-identifier"] = identifier
    return bag


def validate_application_values(values: Mapping[str, str], *, is_eori: bool = False) -> None:
    """两个网页入口在启动和继续前共用的地址国家校验。"""
    english_keys = ["premises", "street", "locality", "city", "region"]
    if not is_eori:
        english_keys += ["home_" + key for key in english_keys] + ["business_description"]
    chinese_fields = [FIELD_DEFINITIONS[key][0] for key in english_keys if re.search(r"[\u3400-\u9fff]", values.get(key, ""))]
    if chinese_fields:
        raise ValueError("以下申请字段仍含中文，请翻译或补充英文后再确认：" + "、".join(chinese_fields))
    required = ["country"] if is_eori else ["country", "home_country"]
    missing = [FIELD_DEFINITIONS[key][0] for key in required if not values.get(key, "").strip()]
    if missing:
        raise ValueError("请明确填写并核对：" + "、".join(missing))
    for raw_key, country_key in (("business_address", "country"), ("residential_address", "home_country")):
        if is_eori and country_key == "home_country":
            continue
        detected = address_country(values.get(raw_key, ""))
        if detected and detected != normalize_country(values.get(country_key, "")):
            raise ValueError(f"{FIELD_DEFINITIONS[country_key][0]}与原始地址中的国家不一致，请核对并修正。")


def extracted_birth_date(values: Mapping[str, str]) -> dict[str, str]:
    """出生日期按 HMRC 登录页面用的字段名展开（含裸 Day/Month/Year）。"""
    raw = (values.get("birth_date") or "").strip()
    if not raw:
        return {}
    parts = extracted_date_parts(raw, "date-of-birth")
    parts.update(
        {
            "Day": parts["date-of-birth.day"],
            "Month": parts["date-of-birth.month"],
            "Year": parts["date-of-birth.year"],
        }
    )
    return parts


def extracted_date_parts(raw: str, prefix: str) -> dict[str, str]:
    """把日期字符串拆成 <prefix>.day/.month/.year，供页面按字段名取值。"""
    raw = (raw or "").strip()
    if not raw:
        return {}
    for pattern in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            parsed = datetime.strptime(raw, pattern).date()
            return {
                f"{prefix}.day": str(parsed.day),
                f"{prefix}.month": str(parsed.month),
                f"{prefix}.year": str(parsed.year),
            }
        except ValueError:
            continue
    raise ValueError(
        f"{prefix} 日期格式应为 YYYY-MM-DD、DD/MM/YYYY 或 DD-MM-YYYY：{raw}"
    )


def _extract_text(suffix: str, content: bytes) -> str:
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise RuntimeError("缺少本地 PDF 解析依赖 pypdf。") from exc
        reader = PdfReader(io.BytesIO(content))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    if suffix == ".docx":
        return _docx_text(content)
    if suffix == ".json":
        data = json.loads(content.decode("utf-8-sig"))
        return "\n".join(_flatten_json(data))
    if suffix in {".csv", ".tsv"}:
        delimiter = "\t" if suffix == ".tsv" else ","
        rows = csv.reader(io.StringIO(content.decode("utf-8-sig")), delimiter=delimiter)
        return "\n".join(_row_text(row) for row in rows if any(cell.strip() for cell in row))
    for encoding in ("utf-8-sig", "utf-16", "gb18030"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("无法识别资料文档的文本编码。")


def _docx_text(content: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        xml = archive.read("word/document.xml")
    root = ElementTree.fromstring(xml)
    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    body = root.find(namespace + "body")
    if body is None:
        return ""
    lines: list[str] = []
    for child in body:
        if child.tag == namespace + "tbl":
            for row in child.iter(namespace + "tr"):
                cells: list[str] = []
                for cell in row.findall(namespace + "tc"):
                    paragraphs = []
                    for paragraph in cell.iter(namespace + "p"):
                        paragraph_text = "".join(
                            node.text or ""
                            for node in paragraph.iter(namespace + "t")
                        ).strip()
                        if paragraph_text:
                            paragraphs.append(paragraph_text)
                    if paragraphs:
                        cells.append(" ".join(paragraphs))
                if cells:
                    lines.append(" ".join(cells))
        elif child.tag == namespace + "p":
            text = "".join(
                node.text or "" for node in child.iter(namespace + "t")
            ).strip()
            if text:
                lines.append(text)
    return "\n".join(lines)


_XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_XLSX_ADDRESS_FIELDS = {
    "门牌号单元号不要有等特殊字符": "premises",
    "道路小区": "street",
    "区": "locality",
    "市": "city",
    "省份": "region",
    "邮编": "postcode",
    "国家": "country",
}


def _xlsx_values(content: bytes) -> tuple[dict[str, str], list[str], dict[str, str]]:
    """按单元格读取；采集表保留 A 分组/B 子字段/C 答案的对应关系。"""
    values: dict[str, str] = {}
    warnings: list[str] = []
    credentials: dict[str, str] = {}
    aliases = {
        _field_key(alias): key
        for key, (_, names) in FIELD_DEFINITIONS.items()
        for alias in (key, *names)
    }
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
            shared = [
                "".join(node.text or "" for node in item.iter(_XLSX_NS + "t"))
                for item in root
            ]
        formats: list[str] = []
        if "xl/styles.xml" in archive.namelist():
            styles = ElementTree.fromstring(archive.read("xl/styles.xml"))
            custom = {
                item.get("numFmtId"): item.get("formatCode", "")
                for item in styles.findall(f"{_XLSX_NS}numFmts/{_XLSX_NS}numFmt")
            }
            for style in styles.findall(f"{_XLSX_NS}cellXfs/{_XLSX_NS}xf"):
                number_id = int(style.get("numFmtId", "0"))
                builtin_date = number_id in {
                    14, 15, 16, 17, 22, *range(27, 37), *range(50, 59),
                }
                formats.append(
                    "yyyy-mm-dd" if builtin_date else custom.get(str(number_id), "")
                )
        date_1904 = False
        if "xl/workbook.xml" in archive.namelist():
            workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
            props = workbook.find(_XLSX_NS + "workbookPr")
            date_1904 = props is not None and props.get("date1904") in {"1", "true"}

        for name in sorted(
            item for item in archive.namelist()
            if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", item)
        ):
            sheet = ElementTree.fromstring(archive.read(name))
            rows: dict[int, dict[str, str]] = {}
            for row in sheet.findall(f"{_XLSX_NS}sheetData/{_XLSX_NS}row"):
                row_number = int(row.get("r", str(len(rows) + 1)))
                cells: dict[str, str] = {}
                for index, cell in enumerate(row.findall(_XLSX_NS + "c")):
                    reference = cell.get("r", f"{chr(65 + index)}{row_number}")
                    column = re.sub(r"\d", "", reference)
                    if (
                        cell.find(_XLSX_NS + "f") is not None
                        and not cell.findtext(_XLSX_NS + "v")
                    ):
                        message = "Excel 中有公式未保存计算结果，请用 Excel/WPS 重新计算并保存后上传，或人工补齐。"
                        if message not in warnings:
                            warnings.append(message)
                    if cell.get("t") == "e":
                        message = "Excel 中有单元格计算错误，已跳过错误值，请检查原表并补齐资料。"
                        if message not in warnings:
                            warnings.append(message)
                    cells[column] = _xlsx_cell_value(cell, shared, formats, date_1904)
                rows[row_number] = cells

            # 用表内结构而非文件名识别采集表，普通两列 XLSX 继续兼容。
            collection = any(
                aliases.get(_field_key(row.get("A", ""))) == "full_name"
                and _field_key(row.get("B", "")) in {"中文名字", "英文名字"}
                for row in rows.values()
            )
            if not collection:
                for row in rows.values():
                    values.update(_parse_values(_row_text(list(row.values()))))
                continue

            groups: list[tuple[int, int, str]] = []
            address_leads: dict[str, str] = {}
            for merged in sheet.findall(f"{_XLSX_NS}mergeCells/{_XLSX_NS}mergeCell"):
                match = re.fullmatch(r"A(\d+):A(\d+)", merged.get("ref", ""))
                if match:
                    start, end = map(int, match.groups())
                    groups.append((start, end, rows.get(start, {}).get("A", "")))
            for row_number, row in rows.items():
                # 用户确认的采集表位置：公司中文名称行 B 列存放分组 GG 号。
                # 不扫描所有数字单元格，避免把税号或电话误当登录账号。
                if _field_key(row.get("A", "")) == "公司中文名称":
                    candidate = re.sub(r"\s+", "", row.get("B", ""))
                    if re.fullmatch(r"[0-9]{12}", candidate):
                        credentials["HMRC_USER_ID"] = candidate
                value = row.get("C", "")
                if not value:
                    continue  # B 列是说明/子字段，不能将其当成空答案的替代值。
                label = row.get("A", "") or next(
                    (label for start, end, label in groups if start <= row_number <= end),
                    "",
                )
                key = aliases.get(_field_key(label))
                sublabel = _field_key(row.get("B", ""))
                if key in {"full_name", "previous_name"}:
                    if sublabel == "英文名字":
                        values[key] = value
                elif key in {"business_address", "residential_address"}:
                    address_key = _XLSX_ADDRESS_FIELDS.get(sublabel)
                    if address_key:
                        prefix = "home_" if key == "residential_address" else ""
                        values[prefix + address_key] = value
                        if key == "business_address" and address_key == "postcode":
                            values["business_postcode"] = value
                    elif sublabel in {"", "完整地址"}:
                        values[key] = value
                        if not sublabel:
                            address_leads[key] = value
                elif key:
                    values[key] = value
                elif _field_key(label) == "注册日期":
                    # 此模板的顶部 VAT 信息区，不能泛化为公司的成立日期。
                    values["vat_registration_date"] = value
            for address_key, lead in address_leads.items():
                _complete_xlsx_address(values, address_key, lead)
    if any(key.startswith("home_") for key in values) and not (
        values.get("home_country") or values.get("residential_address")
    ):
        warnings.append("居住地址未提供国家，请人工补充；不会直接沿用公司注册国家。")
    return values, warnings, credentials


def _complete_xlsx_address(values: dict[str, str], address_key: str, lead: str) -> None:
    """保留采集表地址组无子标签的首行，不能只存原文而漏进填表字段。"""
    prefix = "home_" if address_key == "residential_address" else ""
    premises = values.get(prefix + "premises", "")
    street = values.get(prefix + "street", "")
    original_parts = [lead] + [values.get(prefix + key, "") for key in (
        "premises", "street", "locality", "city", "region", "postcode",
    )]
    # 完整地址行已有相同分项时，只补分项之前尚未包含的楼宇信息。
    position = lead.casefold().find(premises.casefold()) if premises else -1
    missing_lead = lead[:position].strip(" ,，") if position >= 0 else lead
    country = _normalize_country(values.get("company_registration_country", ""))
    street_pattern = r"\b(?:road|rd|street|st|avenue|ave|lane|ln)\b"
    if (
        not prefix and country == "Hong Kong" and missing_lead
        and re.search(street_pattern, premises, re.I)
        and not re.search(street_pattern, street, re.I)
    ):
        # 香港模板常按“楼宇 / 街道门牌 / 地区 / 区域 / 香港”逐行填入。
        values["premises"] = missing_lead
        values["street"] = premises
        values["locality"] = ", ".join(dict.fromkeys(
            part for part in (street, values.get("locality", "")) if part
        ))
    elif missing_lead:
        values[prefix + "premises"] = ", ".join(
            part for part in (missing_lead, premises) if part
        )
    # 原始核对文本按表内顺序保留，不让重新分项覆盖首行中的其他内容。
    source_parts: list[str] = []
    for part in original_parts:
        if part and _field_key(part) not in _field_key(", ".join(source_parts)):
            source_parts.append(part)
    values[address_key] = ", ".join(source_parts)
    if not prefix and country:
        values.setdefault("country", country)


def _xlsx_cell_value(
    cell: ElementTree.Element,
    shared: list[str],
    formats: list[str],
    date_1904: bool,
) -> str:
    kind = cell.get("t", "n")
    raw = cell.findtext(_XLSX_NS + "v", "")
    if kind == "s":
        if not raw.isdigit() or int(raw) >= len(shared):
            raise ValueError("Excel 文本索引无效，请重新保存为 XLSX 后上传。")
        raw = shared[int(raw)]
    elif kind == "inlineStr":
        raw = "".join(node.text or "" for node in cell.iter(_XLSX_NS + "t"))
    elif kind == "e":
        return ""  # Excel 错误值不能作为注册答案。
    elif kind == "d":
        raw = raw.split("T", 1)[0]
    elif kind == "n" and raw:
        style = int(cell.get("s", "0"))
        fmt = formats[style] if 0 <= style < len(formats) else ""
        tokens = re.sub(r'"[^"]*"|\\.|\[[^\]]*\]', "", fmt).casefold()
        try:
            number = Decimal(raw)
            if not number.is_finite():
                raise ValueError("Excel 含无效数值，请检查原表。")
            if re.search(r"[yd]", tokens):
                if not date_1904 and int(number) == 60:
                    raise ValueError("Excel 中存在无效日期 1900-02-29，请修正原表。")
                base = datetime(1904, 1, 1) if date_1904 else datetime(1899, 12, 30)
                if not date_1904 and 0 < number < 60:
                    base += timedelta(days=1)
                raw = (base + timedelta(days=float(number))).date().isoformat()
            elif number == number.to_integral_value():
                raw = format(number.quantize(Decimal(1)), "f")
                if re.fullmatch(r"0{2,32}", fmt):
                    raw = raw.zfill(len(fmt))
        except (InvalidOperation, OverflowError) as exc:
            raise ValueError("Excel 数值或日期无法解析，请检查单元格格式。") from exc
    return re.sub(r"\s+", " ", raw).strip()


def _row_text(row: list[str]) -> str:
    cleaned = [cell.strip() for cell in row if cell.strip()]
    if len(cleaned) == 2:
        return f"{cleaned[0]}: {cleaned[1]}"
    return "\t".join(cleaned)


def _flatten_json(value: Any, prefix: str = "") -> list[str]:
    if isinstance(value, dict):
        lines: list[str] = []
        for key, item in value.items():
            lines.extend(_flatten_json(item, str(key)))
        return lines
    if isinstance(value, list):
        return [f"{prefix}: {', '.join(map(str, value))}"]
    return [f"{prefix}: {value}"]


def _parse_values(text: str) -> dict[str, str]:
    alias_map: dict[str, str] = {}
    for key, (_, aliases) in FIELD_DEFINITIONS.items():
        for alias in (key, *aliases):
            alias_map[_field_key(alias)] = key

    label_patterns: list[tuple[re.Pattern[str], str]] = []
    for key, (_, aliases) in FIELD_DEFINITIONS.items():
        for alias in sorted((key, *aliases), key=len, reverse=True):
            label_patterns.append(
                (
                    re.compile(
                        _flexible_label_pattern(alias) + r"\s*[:：]",
                        re.IGNORECASE,
                    ),
                    key,
                )
            )

    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        matches: list[tuple[int, int, str]] = []
        for pattern, key in label_patterns:
            for match in pattern.finditer(line):
                matches.append((match.start(), match.end(), key))
        if matches:
            matches.sort(key=lambda item: (item[0], -(item[1] - item[0])))
            selected: list[tuple[int, int, str]] = []
            for item in matches:
                if not any(
                    start <= item[0] and item[1] <= end
                    for start, end, _ in selected
                ):
                    selected.append(item)
            selected.sort(key=lambda item: item[0])
            # 地址内的“邮编：”属于该地址，不能拆成全局公司邮编，
            # 否则居住地址尾部的邮编和国家会一起丢失。
            scoped: list[tuple[int, int, str]] = []
            for item in selected:
                if item[2] == "postcode" and scoped and scoped[-1][2] in {"residential_address", "business_address"}:
                    continue
                scoped.append(item)
            selected = scoped
            for index, (_, value_start, key) in enumerate(selected):
                value_end = selected[index + 1][0] if index + 1 < len(selected) else len(line)
                value = line[value_start:value_end].strip(" 	,;，；")
                if value:
                    values[key] = value
            continue
        parts = re.split(r"\s*[:：]\s*|\t+", line, maxsplit=1)
        if len(parts) != 2:
            continue
        canonical = alias_map.get(_field_key(parts[0]))
        value = parts[1].strip()
        if canonical and value:
            values[canonical] = value
    return values


def normalize_vat_number(value: str) -> str:
    """仅去除英国前缀和分组空白，保留前导零，不截断或删除其他字符。"""
    compact = re.sub(r"\s+", "", value)
    return re.sub(r"^GB", "", compact, count=1, flags=re.IGNORECASE)


def _derive_values(values: dict[str, str]) -> dict[str, str]:
    derived = dict(values)
    if derived.get("_uscc") and derived.get("_brn"):
        derived["company_identifier_type"] = ""
        derived["company_registration_number"] = ""
    else:
        for source, kind in (("_uscc", "USCC"), ("_brn", "BRN"), ("_crn", "CRN")):
            if derived.get(source):
                derived["company_registration_number"] = derived[source]
                derived["company_identifier_type"] = kind
                break
    derived = normalized_identity_values(derived)
    if derived.get("vat_number"):
        derived["vat_number"] = normalize_vat_number(derived["vat_number"])
    if derived.get("full_name"):
        derived["full_name"] = _normalize_latin_name(derived["full_name"])
    for key in ("email", "vat_contact_email"):
        if derived.get(key):
            derived[key] = _normalize_email(derived[key])
    for key in ("phone", "business_phone"):
        if derived.get(key):
            derived[key] = _normalize_phone(derived[key])
    if derived.get("business_type") and not derived.get("business_description"):
        derived["business_description"] = derived["business_type"]

    name_parts = derived.get("full_name", "").split()
    if len(name_parts) >= 2:
        derived.setdefault("first_name", name_parts[0])
        derived.setdefault("last_name", " ".join(name_parts[1:]))

    if derived.get("business_address"):
        address = _parse_business_address(
            derived["business_address"],
            derived.get("business_postcode", ""),
            derived.get("country", ""),
        )
        for key, value in address.items():
            derived.setdefault(key, value)
    if derived.get("residential_address"):
        home_address = _parse_business_address(
            derived["residential_address"], derived.get("home_postcode", ""), derived.get("home_country", "")
        )
        for key, value in home_address.items():
            derived.setdefault(f"home_{key}", value)
    # 上传识别时补齐合理默认值；人工编辑和暂停继续不会重新生成已清空字段。
    if not derived.get("country"):
        business = ", ".join(derived.get(key, "") for key in ("business_address", "premises", "street", "locality", "city", "region"))
        derived["country"] = infer_address_country(business) or derived.get("company_registration_country", "")
    if not derived.get("home_country"):
        home = ", ".join(derived.get(key, "") for key in ("residential_address", "home_premises", "home_street", "home_locality", "home_city", "home_region"))
        derived["home_country"] = infer_address_country(home)
    for key in ("country", "home_country", "company_registration_country"):
        if derived.get(key):
            derived[key] = _normalize_country(derived[key])
    return derived


def _normalize_country(value: str) -> str:
    return normalize_country(value)


def _normalize_phone(value: str) -> str:
    """删除电话号码后的模板备注，只保留数字，不自动添加加号。"""
    number_part = re.split(r"[()（）]", value, maxsplit=1)[0]
    return re.sub(r"\D+", "", number_part)


def _normalize_email(value: str) -> str:
    """移除 Word 换行产生的邮箱内部空白，并丢弃模板尾注。"""
    match = re.search(
        r"[A-Z0-9._%+\-]+\s*@\s*(?:[A-Z0-9\-]+\s*\.\s*)+[A-Z]{2,}",
        value,
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", "", match.group(0) if match else value).strip()


def _normalize_latin_name(value: str) -> str:
    """只保留法人姓名中的英文/拼音部分，不将中文姓名提交给 HMRC。"""
    return " ".join(re.findall(r"[A-Za-z][A-Za-z'\-]*", value))


def _parse_business_address(raw: str, postcode: str, country: str) -> dict[str, str]:
    address, detected_country = split_address_country(raw)
    address = re.sub(r"\s+", " ", address).strip(" ,")
    # 显式标注的邮编可含字母和空格；不将其拼入街道或城市。
    postcode_label = re.search(r"(?:邮编|postcode|postal\s+code)\s*[:：]\s*([A-Za-z0-9][A-Za-z0-9 -]*)$", address, re.IGNORECASE)
    if postcode_label:
        if not postcode:
            postcode = postcode_label[1].strip()
        address = address[:postcode_label.start()].strip(" ,，")
    if re.search(r"[\u3400-\u9fff]", address):
        english_start = re.search(
            r"(?:\b\d+[A-Za-z]?\s*,\s*(?=(?:Building|Bldg)\b)|"
            r"\b(?:Room|Rm|Suite|Unit|Building|Bldg)\b|\bNo\.)",
            address,
            flags=re.IGNORECASE,
        )
        if english_start:
            address = address[english_start.start():]
    if not postcode:
        postcode_match = re.search(r"(?:^|\s)(\d{5,6})$", address)
        if postcode_match:
            postcode = postcode_match.group(1)
            address = address[:postcode_match.start()].strip(" ,")
    parts = [part.strip() for part in re.split(r"[,，]", address) if part.strip()]
    locality_index = next(
        (
            index
            for index, part in enumerate(parts)
            if re.search(r"\b(?:District|County|City|Province|State)\b", part, re.I)
        ),
        len(parts),
    )
    street_index = next(
        (
            index
            for index, part in enumerate(parts[:locality_index])
            if re.search(
                r"(?:\bNo\.|\b(?:Road|Rd|Street|St|Avenue|Ave|Community)\b)",
                part,
                re.I,
            )
        ),
        min(1, locality_index),
    )
    result: dict[str, str] = {}
    if street_index:
        result["premises"] = ", ".join(parts[:street_index])
    if street_index < locality_index:
        result["street"] = ", ".join(parts[street_index:locality_index])
    locality_parts = parts[locality_index:]
    for part in locality_parts:
        if "district" in part.casefold() or "county" in part.casefold():
            result.setdefault("locality", part)
        elif "city" in part.casefold():
            result.setdefault("city", part)
        elif "province" in part.casefold() or "state" in part.casefold():
            result.setdefault("region", part)
        elif result.get("city"):
            result.setdefault("region", part)
    if locality_parts and not result.get("city"):
        result["city"] = locality_parts[0]
    if postcode:
        result["postcode"] = postcode.strip()
    normalized_country = _normalize_country(country)
    result["country"] = normalized_country or detected_country or infer_address_country(raw)
    return {key: value for key, value in result.items() if value}


def _field_key(value: str) -> str:
    return re.sub(r"[\W_]+", "", value.casefold(), flags=re.UNICODE)


def _flexible_label_pattern(value: str) -> str:
    """模板标签中的人工空格可有可无，其余字符仍按原文精确匹配。"""
    return r"\s*".join(re.escape(part) for part in re.split(r"\s+", value.strip()))


def _extract_project_code(filename: str) -> str:
    stem = Path(filename).stem
    # 项目编号自身采用“英文字母 + 数字”的格式。编号后的内容是可变的
    # 公司名称或人工备注，不能作为识别条件，也不能并入项目编号。
    match = re.search(
        r"新注册\s*VAT[-_\s]*([A-Za-z]+\d+)",
        stem,
        flags=re.IGNORECASE,
    )
    return match.group(1).upper() if match else ""
