from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree


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
    "email": ("个人电子邮箱", ("email", "email address", "邮箱", "电子邮箱")),
    "phone": ("个人手机号", ("phone", "mobile", "telephone number", "mobile number", "手机号码", "手机号", "联系电话")),
    "business_description": (
        "业务描述",
        ("business description", "what does the business do", "goods or services", "业务描述", "经营范围", "销售产品名称（列举1-2个主要产品即可）"),
    ),
    "estimated_turnover": (
        "预计应税营业额",
        ("estimated taxable turnover", "taxable turnover", "预计应税营业额", "预计营业额", "预估之后连续12个月的总营业额（英镑，大约预估）"),
    ),
    "birth_date": ("出生日期", ("date of birth", "birth date", "出生日期")),
    "overseas_tax_identifier": (
        "海外税务识别号",
        ("overseas tax identifier", "tax identifier", "tax id", "海外税号", "税务识别号", "中国纳税人识别号，如有"),
    ),
    "premises": (
        "房间/楼宇",
        ("premises", "address line 1", "room and building", "房间楼宇", "地址第一行"),
    ),
    "street": ("街道地址", ("street", "address line 2", "street address", "街道地址", "地址第二行")),
    "locality": ("区/街道", ("locality", "district", "区", "街道")),
    "city": ("城市", ("city", "town or city", "城市")),
    "region": ("省/地区", ("region", "province", "state", "省", "地区")),
    "postcode": ("邮编", ("postcode", "postal code", "zip code", "邮编")),
    "country": ("国家", ("country", "国家")),
    "home_premises": ("居住地址第1行（英文）", ()),
    "home_street": ("居住地址第2行（英文）", ()),
    "home_locality": ("居住地址区/街道（英文）", ()),
    "home_city": ("居住地址城市（英文）", ()),
    "home_region": ("居住地址省/地区（英文）", ()),
    "home_postcode": ("居住地址邮编", ()),
    "home_country": ("居住地址国家（英文）", ()),
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
    "company_registration_number": ("公司注册号/统一社会信用代码", ("公司注册号（统一社会信用代码）",)),
    "company_registration_country": ("公司注册国家", ("公司注册国家（请根据实际情况填写）",)),
    "company_incorporation_date": ("公司成立日期", ("公司成立日期",)),
    "business_address": ("公司注册地址", ("公司注册地址（与Amazon或Ebay等在线平台注册地址一致，如地址是中文，则用对应英文 或拼音表示）",)),
    "business_postcode": ("公司注册地址邮编", ("公司注册地址邮编",)),
    "business_type": ("营业类型", ("营业类型（贸易/物流/金融/数字货币交易/IT/咨询/旅游/建筑/房产/餐饮/法律 等）",)),
    "is_small_business": ("是否小微企业", ("公司是否属于小微企业",)),
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
VAT_REGISTRATION_FIELD_KEYS = (
    "project_code", "application_reference",
    "full_name", "first_name", "last_name", "birth_date",
    "overseas_tax_identifier", "email", "phone", "residential_address",
    "home_premises", "home_street", "home_locality", "home_city",
    "home_region", "home_postcode", "home_country", "business_name",
    "trading_name", "company_registration_number",
    "company_registration_country", "company_incorporation_date",
    "business_address", "business_postcode", "vat_contact_email",
    "business_phone", "business_description",
    "premises", "street", "locality", "city", "region", "postcode",
    "country",
)


def extract_document(filename: str, content: bytes) -> dict[str, Any]:
    suffix = Path(filename).suffix.casefold()
    if suffix not in SUPPORTED_SUFFIXES:
        raise ValueError(
            "资料文档仅支持 PDF、DOCX、XLSX、TXT、JSON、CSV 或 TSV。"
        )
    if not content:
        raise ValueError("资料文档为空。")

    text = _extract_text(suffix, content)
    parsed_values = _derive_values(_parse_values(text))
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
    warnings: list[str] = []
    for required in ("business_name", "full_name", "email", "phone"):
        if not values.get(required):
            warnings.append(f"未识别到{FIELD_DEFINITIONS[required][0]}，请人工补充。")
    if not any(values.get(key) for key in ADDRESS_KEYS):
        warnings.append("未识别到结构化地址，请人工补充地址字段。")

    return {
        "values": values,
        "labels": {
            key: FIELD_DEFINITIONS[key][0] for key in VAT_REGISTRATION_FIELD_KEYS
        },
        "warnings": warnings,
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


def extracted_birth_date(values: dict[str, str]) -> dict[str, str]:
    raw = values.get("birth_date", "").strip()
    if not raw:
        return {}
    for pattern in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            parsed = datetime.strptime(raw, pattern).date()
            return {
                "date-of-birth.day": str(parsed.day),
                "date-of-birth.month": str(parsed.month),
                "date-of-birth.year": str(parsed.year),
                "Day": str(parsed.day),
                "Month": str(parsed.month),
                "Year": str(parsed.year),
            }
        except ValueError:
            continue
    raise ValueError("出生日期格式应为 YYYY-MM-DD、DD/MM/YYYY 或 DD-MM-YYYY。")


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
    if suffix == ".xlsx":
        return _xlsx_text(content)
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


def _xlsx_text(content: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in root:
                shared.append("".join(node.text or "" for node in item.iter() if node.tag.endswith("}t")))
        lines: list[str] = []
        for name in sorted(
            item for item in archive.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", item)
        ):
            root = ElementTree.fromstring(archive.read(name))
            for row in (node for node in root.iter() if node.tag.endswith("}row")):
                values: list[str] = []
                for cell in (node for node in row if node.tag.endswith("}c")):
                    kind = cell.attrib.get("t", "")
                    value_node = next((node for node in cell.iter() if node.tag.endswith("}v")), None)
                    inline = "".join(node.text or "" for node in cell.iter() if node.tag.endswith("}t"))
                    value = inline or (value_node.text if value_node is not None else "") or ""
                    if kind == "s" and value.isdigit() and int(value) < len(shared):
                        value = shared[int(value)]
                    values.append(value.strip())
                if any(values):
                    lines.append(_row_text(values))
    return "\n".join(lines)


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
        for alias in aliases:
            alias_map[_field_key(alias)] = key

    label_patterns: list[tuple[re.Pattern[str], str]] = []
    for key, (_, aliases) in FIELD_DEFINITIONS.items():
        for alias in sorted(aliases, key=len, reverse=True):
            label_patterns.append(
                (re.compile(re.escape(alias) + r"\s*[:：]", re.IGNORECASE), key)
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


def _derive_values(values: dict[str, str]) -> dict[str, str]:
    derived = dict(values)
    if derived.get("full_name"):
        derived["full_name"] = _normalize_latin_name(derived["full_name"])
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
            derived.get("company_registration_country", ""),
        )
        for key, value in address.items():
            derived.setdefault(key, value)
    if derived.get("residential_address"):
        home_address = _parse_business_address(
            derived["residential_address"], "", "China"
        )
        for key, value in home_address.items():
            derived.setdefault(f"home_{key}", value)
    return derived


def _normalize_phone(value: str) -> str:
    """删除电话号码后的模板备注，只保留数字，不自动添加加号。"""
    number_part = re.split(r"[()（）]", value, maxsplit=1)[0]
    return re.sub(r"\D+", "", number_part)


def _normalize_latin_name(value: str) -> str:
    """只保留法人姓名中的英文/拼音部分，不将中文姓名提交给 HMRC。"""
    return " ".join(re.findall(r"[A-Za-z][A-Za-z'\-]*", value))


def _parse_business_address(raw: str, postcode: str, country: str) -> dict[str, str]:
    address = re.sub(r"\s+", " ", raw).strip(" ,")
    english_start = re.search(
        r"(?:\b(?:Room|Rm|Suite|Unit|Building|Bldg)\b|\bNo\.)",
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
    parts = [part.strip() for part in address.split(",") if part.strip()]
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
                r"(?:\bNo\.|\b(?:Road|Rd|Street|St|Avenue|Ave)\b)",
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
    normalized_country = {"中国": "China", "PRC": "China"}.get(country.strip(), country.strip())
    result["country"] = normalized_country or "China"
    return {key: value for key, value in result.items() if value}


def _field_key(value: str) -> str:
    return re.sub(r"[\W_]+", "", value.casefold(), flags=re.UNICODE)


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
