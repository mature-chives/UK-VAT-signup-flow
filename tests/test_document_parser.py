import io
import json
import unittest
import zipfile

from vat_automation.document_parser import (
    extract_document,
    extracted_birth_date,
)


class DocumentParserTests(unittest.TestCase):
    def test_txt_extracts_english_and_chinese_labels(self) -> None:
        result = extract_document(
            "vat-info.txt",
            (
                "Business name: Local Example Ltd\n"
                "负责人姓名：Alex Zhang\n"
                "Email: alex@example.test\n"
                "手机号：13800138000\n"
                "城市：Hangzhou\n"
            ).encode(),
        )
        self.assertEqual(result["values"]["business_name"], "Local Example Ltd")
        self.assertEqual(result["values"]["full_name"], "Alex Zhang")
        self.assertEqual(result["values"]["city"], "Hangzhou")
        self.assertNotIn("未识别到结构化地址，请人工补充地址字段。", result["warnings"])

    def test_result_excludes_fields_not_used_by_vat_registration(self) -> None:
        result = extract_document(
            "vat-info.txt",
            (
                "Business name: Example Ltd\n"
                "Occupation: Designer\n"
                "Estimated taxable turnover: 85000\n"
            ).encode(),
        )
        self.assertIn("business_name", result["values"])
        self.assertNotIn("occupation", result["values"])
        self.assertNotIn("estimated_turnover", result["values"])
        self.assertNotIn("occupation", result["labels"])
        self.assertNotIn("estimated_turnover", result["labels"])
        self.assertNotIn("text_preview", result)

    def test_phone_annotation_is_removed_without_adding_plus(self) -> None:
        values = extract_document(
            "vat-info.txt",
            (
                "Business name: Example Ltd\n"
                "Full name: San Zhang\n"
                "Email: user@example.test\n"
                "手机号码：8613592850576(国际区号+电话号)\n"
                "City: Hangzhou\n"
            ).encode(),
        )["values"]
        self.assertEqual(values["phone"], "8613592850576")

    def test_json_is_flattened_without_external_service(self) -> None:
        content = json.dumps(
            {"Business name": "JSON Ltd", "Full name": "Li Lei"}
        ).encode()
        values = extract_document("vat.json", content)["values"]
        self.assertEqual(values["business_name"], "JSON Ltd")
        self.assertEqual(values["full_name"], "Li Lei")

    def test_docx_paragraphs_are_read_from_local_zip(self) -> None:
        document_xml = b'''<?xml version="1.0" encoding="UTF-8"?>
        <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
          <w:body><w:p><w:r><w:t>Business name: DOCX Ltd</w:t></w:r></w:p></w:body>
        </w:document>'''
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("word/document.xml", document_xml)
        values = extract_document("vat.docx", buffer.getvalue())["values"]
        self.assertEqual(values["business_name"], "DOCX Ltd")

    def test_xlsx_two_column_row_becomes_label_value(self) -> None:
        worksheet = b'''<?xml version="1.0" encoding="UTF-8"?>
        <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
          <sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>Business name</t></is></c>
          <c r="B1" t="inlineStr"><is><t>XLSX Ltd</t></is></c></row></sheetData>
        </worksheet>'''
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("xl/worksheets/sheet1.xml", worksheet)
        values = extract_document("vat.xlsx", buffer.getvalue())["values"]
        self.assertEqual(values["business_name"], "XLSX Ltd")

    def test_vat_authorisation_docx_template_extracts_and_derives_fields(self) -> None:
        rows = [
            "法人姓名：李明 Ming Li",
            "出生日期：1990-02-03",
            "手机号码：8613900000000",
            "电子邮箱：personal@example.test",
            "公司名称（拼音或英文名称，要跟账号后台注册名称一致）：Local Trading Ltd",
            "公司注册号（统一社会信用代码）：ABC123 公司注册国家（请根据实际情况填写）：中国",
            "公司注册地址（与Amazon或Ebay等在线平台注册地址一致，如地址是中文，则用对应英文 或拼音表示）：Room 8, Building 2, No. 9 Test Road, Demo District, Sample City, Zhejiang 公司注册地址邮编：310000",
            "注册VAT的沟通邮箱：vat@example.test",
            "生意联系电话：13800138000",
        ]
        table_rows = "".join(
            f"<w:tr><w:tc><w:p><w:r><w:t>{row}</w:t></w:r></w:p></w:tc></w:tr>"
            for row in rows
        )
        document_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body><w:tbl>{table_rows}</w:tbl></w:body></w:document>"
        ).encode()
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("word/document.xml", document_xml)
        values = extract_document("authorisation.docx", buffer.getvalue())["values"]
        self.assertEqual(values["business_name"], "Local Trading Ltd")
        self.assertEqual(values["full_name"], "Ming Li")
        self.assertEqual(values["company_registration_number"], "ABC123")
        self.assertEqual(values["email"], "personal@example.test")
        self.assertEqual(values["phone"], "8613900000000")
        self.assertEqual(values["vat_contact_email"], "vat@example.test")
        self.assertEqual(values["business_phone"], "13800138000")
        self.assertEqual(values["first_name"], "Ming")
        self.assertEqual(values["last_name"], "Li")
        self.assertEqual(values["premises"], "Room 8,Building 2")
        self.assertEqual(values["country"], "China")

    def test_chinese_legal_representative_name_is_removed(self) -> None:
        values = extract_document(
            "vat-info.txt",
            (
                "Business name: Example Ltd\n"
                "法人姓名：张三 San Zhang\n"
                "Email: user@example.test\n"
                "Phone: 8613800138000\n"
                "City: Hangzhou\n"
            ).encode(),
        )["values"]
        self.assertEqual(values["full_name"], "San Zhang")
        self.assertEqual(values["first_name"], "San")
        self.assertEqual(values["last_name"], "Zhang")

    def test_project_reference_uses_filename_code_and_english_business_name(self) -> None:
        values = extract_document(
            "客户信息及服务授权表-新注册VAT-AB223322公司名.txt",
            (
                "Business name: Hangzhou Fell Wheel Technology Co., Ltd\n"
                "Full name: San Zhang\n"
                "Email: user@example.test\n"
                "Phone: 8613800138000\n"
                "City: Hangzhou\n"
            ).encode(),
        )["values"]
        self.assertEqual(values["project_code"], "AB223322")
        self.assertEqual(
            values["application_reference"],
            "AB223322-UK-Hangzhou Fell Wheel Technology Co., Ltd",
        )

    def test_birth_date_is_converted_to_hmrc_components(self) -> None:
        self.assertEqual(
            extracted_birth_date({"birth_date": "31/12/1990"}),
            {
                "date-of-birth.day": "31",
                "date-of-birth.month": "12",
                "date-of-birth.year": "1990",
                "Day": "31",
                "Month": "12",
                "Year": "1990",
            },
        )


if __name__ == "__main__":
    unittest.main()
