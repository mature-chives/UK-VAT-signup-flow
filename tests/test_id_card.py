import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from workbench.app import (
    IdCardConfirmRequest,
    TaskCreateRequest,
    confirm_id_card_task,
    context,
    create_task,
    ocr_id_card_task,
)
from workbench.id_card import extract_id_card_fields, merge_id_card_fields
from workbench.id_card_translate import extract_portrait_bbox
from workbench.store import WorkbenchStore
from workbench import app as workbench_app


ZHAO_BLOCKS = {
    "parsing_res_list": [
        {"block_label": "text", "block_content": "姓名 赵君楚", "block_order": 1},
        {"block_label": "text", "block_content": "性别 女 民族 汉", "block_order": 2},
        {"block_label": "text", "block_content": "出生 1973 年 8 月 7 日", "block_order": 3},
        {
            "block_label": "text",
            "block_content": "住址 广西壮族自治区贵港市平南县上渡街道",
            "block_order": 4,
        },
        {"block_label": "image", "block_content": "", "block_order": None},
        {
            "block_label": "text",
            "block_content": "公民身份号码 450821197308071202",
            "block_order": 5,
        },
    ]
}

PAN_BLOCKS = {
    "parsing_res_list": [
        {"block_label": "text", "block_content": "姓名：___ 潘涵涵", "block_order": 1},
        {"block_label": "text", "block_content": "性别 男 民族 汉", "block_order": 2},
        {"block_label": "text", "block_content": "出生 2006年 8 月 10 日", "block_order": 3},
        {"block_label": "text", "block_content": "住址 北京市东城区幽静街111号", "block_order": 4},
        {
            "block_label": "text",
            "block_content": "公民身份号码 110101200608104339",
            "block_order": 5,
        },
    ]
}

COMPACT_TEXT = "姓名赵君楚\n性别女民族汉\n出生1973年8月7日\n住址广西壮族自治区贵港市平\n南县上渡街道\n公民身份号码450821197308071202"

BACK_TEXT = "签发机关 北京市公安局东城分局\n有效期限 2020.08.10-2030.08.10"


class ExtractIdCardFieldsTests(unittest.TestCase):
    def test_extracts_front_side_from_paddleocrvl_blocks(self) -> None:
        fields = extract_id_card_fields(ZHAO_BLOCKS)
        self.assertEqual(fields["full_name"], "赵君楚")
        self.assertEqual(fields["sex"], "女")
        self.assertEqual(fields["ethnicity"], "汉")
        self.assertEqual(fields["birth_date"], "1973年8月7日")
        self.assertEqual(fields["address"], "广西壮族自治区贵港市平南县上渡街道")
        self.assertEqual(fields["identity_document_number"], "450821197308071202")

    def test_strips_ocr_noise_before_name(self) -> None:
        fields = extract_id_card_fields(PAN_BLOCKS)
        self.assertEqual(fields["full_name"], "潘涵涵")
        self.assertEqual(fields["sex"], "男")
        self.assertEqual(fields["address"], "北京市东城区幽静街111号")
        self.assertEqual(fields["identity_document_number"], "110101200608104339")

    def test_joins_address_split_across_lines(self) -> None:
        fields = extract_id_card_fields(COMPACT_TEXT)
        self.assertEqual(fields["full_name"], "赵君楚")
        self.assertEqual(fields["address"], "广西壮族自治区贵港市平南县上渡街道")
        self.assertEqual(fields["identity_document_number"], "450821197308071202")

    def test_extracts_back_side_and_merges_with_front(self) -> None:
        front = extract_id_card_fields(ZHAO_BLOCKS)
        back = extract_id_card_fields(BACK_TEXT)
        merged = merge_id_card_fields(front, back)
        self.assertEqual(merged["full_name"], "赵君楚")
        self.assertEqual(merged["issuing_authority"], "北京市公安局东城分局")
        self.assertEqual(merged["valid_period"], "2020.08.10-2030.08.10")

    def test_portrait_bbox_comes_from_image_block(self) -> None:
        payload = {
            "width": 1800,
            "height": 1200,
            "parsing_res_list": [
                {"block_label": "text", "block_content": "姓名 赵君楚"},
                {"block_label": "image", "block_content": "", "block_bbox": [1103, 254, 1600, 836]},
            ],
        }
        self.assertEqual(extract_portrait_bbox(payload), [1103, 254, 1600, 836])

    def test_portrait_bbox_accepts_eight_point_polygon(self) -> None:
        payload = {
            "width": 1800,
            "height": 1200,
            "parsing_res_list": [
                {
                    "block_label": "image",
                    "block_bbox": [1103, 254, 1600, 254, 1600, 836, 1103, 836],
                }
            ],
        }
        self.assertEqual(extract_portrait_bbox(payload), [1103, 254, 1600, 836])


class OcrTaskTests(unittest.TestCase):
    def setUp(self) -> None:
        self._workspace = tempfile.TemporaryDirectory()
        self.store = WorkbenchStore(Path(self._workspace.name) / "data")
        context.store = self.store
        self._original = workbench_app.recognize_id_card_pack
        workbench_app.recognize_id_card_pack = lambda paths: {
            "fields": {"full_name": "赵君楚", "identity_document_number": "450821197308071202"},
            "sides": [],
        }

    def tearDown(self) -> None:
        workbench_app.recognize_id_card_pack = self._original
        self._workspace.cleanup()

    def _task_with_photo(self, plugin_id: str) -> dict:
        customer = self.store.upsert_customer(customer_id=None, name="示例")
        uploaded = self.store.add_customer_file(
            customer["id"],
            filename="id.jpg",
            content=b"fake-image",
            category="translation-source",
        )
        return asyncio.run(
            create_task(
                TaskCreateRequest(
                    plugin_id=plugin_id,
                    customer_id=customer["id"],
                    file_ids=[uploaded["id"]],
                ),
                username="alice",
                _=None,
            )
        )

    def test_ocr_saves_fields_on_id_card_task(self) -> None:
        task = self._task_with_photo("translate-id")
        result = asyncio.run(ocr_id_card_task(task["id"], "alice", None))
        self.assertEqual(result["fields"]["full_name"], "赵君楚")
        saved = self.store.get_task(task["id"])
        self.assertEqual(saved["extracted_id_card"]["fields"]["full_name"], "赵君楚")
        self.assertEqual(saved["status"], "in_progress")

    def test_ocr_rejects_non_id_card_plugin(self) -> None:
        task = self._task_with_photo("translate-license")
        with self.assertRaises(Exception) as caught:
            asyncio.run(ocr_id_card_task(task["id"], "alice", None))
        self.assertEqual(caught.exception.status_code, 404)

    def _task_ready_to_confirm(self, fields: dict[str, str] | None = None) -> dict:
        from io import BytesIO

        from PIL import Image

        customer = self.store.upsert_customer(customer_id=None, name="示例")
        buffer = BytesIO()
        Image.new("RGB", (120, 76), (120, 130, 140)).save(buffer, format="JPEG")
        uploaded = self.store.add_customer_file(
            customer["id"],
            filename="id.jpg",
            content=buffer.getvalue(),
            category="translation-source",
        )
        task = asyncio.run(
            create_task(
                TaskCreateRequest(
                    plugin_id="translate-id",
                    customer_id=customer["id"],
                    file_ids=[uploaded["id"]],
                ),
                username="alice",
                _=None,
            )
        )
        extracted = fields or {
            "full_name": "潘涵涵",
            "sex": "男",
            "ethnicity": "汉",
            "birth_date": "2006年8月10日",
            "address": "北京市东城区幽静街111号",
            "identity_document_number": "110101200608104339",
            "issuing_authority": "北京市公安局",
            "valid_period": "2026.08.10-2031.08.10",
        }
        return self.store.update_task(
            task["id"],
            extracted_id_card={
                "fields": extracted,
                "sides": [{"file_id": uploaded["id"], "side": "front", "portrait_bbox": None}],
            },
        )

    def test_confirm_writes_two_page_pdf(self) -> None:
        from pypdf import PdfReader

        task = self._task_ready_to_confirm()
        env = {
            "TRANSLATE_VENDOR": "",
            "TRANSLATE_APP_ID": "",
            "TRANSLATE_API_KEY": "",
        }
        with patch.dict(os.environ, env, clear=False):
            result = asyncio.run(
                confirm_id_card_task(task["id"], IdCardConfirmRequest(), "alice", None)
            )
        self.assertEqual(result["translated"]["full_name"], "Pan Hanhan")
        saved = self.store.get_task(task["id"])
        self.assertEqual(saved["status"], "delivered")
        pdf_path = Path(saved["result_files"][0]["path"])
        reader = PdfReader(pdf_path)
        self.assertEqual(len(reader.pages), 2)

    def test_confirm_vendor_never_receives_id_number(self) -> None:
        from workbench.id_card_translate import VendorRequest

        task = self._task_ready_to_confirm()
        seen: list[str] = []

        class FakeVendor:
            async def translate(self, req: VendorRequest) -> str:
                seen.append(req.text)
                return "English " + req.text

        with patch("workbench.id_card_translate.get_vendor", return_value=FakeVendor()):
            result = asyncio.run(
                confirm_id_card_task(task["id"], IdCardConfirmRequest(), "alice", None)
            )
        self.assertEqual(seen, ["北京市东城区幽静街111号", "北京市公安局"])
        self.assertTrue(all("110101200608104339" not in item for item in seen))
        self.assertEqual(result["translated"]["address"], "English 北京市东城区幽静街111号")
        self.assertEqual(self.store.get_task(task["id"])["status"], "delivered")

    def test_confirm_vendor_failure_blocks_without_pdf(self) -> None:
        from workbench.id_card_translate import VendorHttpError, VendorRequest

        task = self._task_ready_to_confirm(
            {
                "full_name": "潘涵涵",
                "address": "北京市东城区幽静街111号",
                "identity_document_number": "110101200608104339",
            }
        )

        class BoomVendor:
            async def translate(self, req: VendorRequest) -> str:
                raise VendorHttpError(
                    "这台电脑现在的上网地址 116.30.229.105 还不能使用翻译接口。"
                    "请管理员把这个地址加入允许列表后再生成。",
                    code="58000",
                )

        with patch("workbench.id_card_translate.get_vendor", return_value=BoomVendor()):
            with self.assertRaises(Exception) as caught:
                asyncio.run(
                    confirm_id_card_task(task["id"], IdCardConfirmRequest(), "alice", None)
                )
        self.assertEqual(caught.exception.status_code, 502)
        self.assertIn("允许列表", caught.exception.detail)
        self.assertIn("116.30.229.105", caught.exception.detail)
        self.assertNotIn("INVALID_CLIENT_IP", caught.exception.detail)
        saved = self.store.get_task(task["id"])
        self.assertEqual(saved["status"], "blocked")
        self.assertIn("允许列表", saved["message"])
        self.assertEqual(saved.get("result_files") or [], [])

    def test_confirm_rate_limit_stays_in_progress(self) -> None:
        from workbench.id_card_translate import VendorHttpError, VendorRequest

        task = self._task_ready_to_confirm(
            {
                "full_name": "潘涵涵",
                "address": "北京市东城区幽静街111号",
            }
        )

        class BusyVendor:
            async def translate(self, req: VendorRequest) -> str:
                raise VendorHttpError("翻译调用太频繁，请等一会儿再生成。", code="54003")

        with patch("workbench.id_card_translate.get_vendor", return_value=BusyVendor()):
            with self.assertRaises(Exception) as caught:
                asyncio.run(
                    confirm_id_card_task(task["id"], IdCardConfirmRequest(), "alice", None)
                )
        self.assertEqual(caught.exception.status_code, 502)
        saved = self.store.get_task(task["id"])
        self.assertEqual(saved["status"], "in_progress")
        self.assertEqual(saved["extracted_id_card"]["translate_error"], caught.exception.detail)
        self.assertEqual(saved.get("result_files") or [], [])


if __name__ == "__main__":
    unittest.main()
