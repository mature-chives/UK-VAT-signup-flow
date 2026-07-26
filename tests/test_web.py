import asyncio
import io
import unittest
from pathlib import Path

from fastapi import HTTPException, UploadFile

from vat_automation.web import (
    ContinueRequest,
    JobManager,
    StartRequest,
    identity_documents,
    index,
    parse_document,
)
from vat_automation.config import PageRule, Settings


class WebTests(unittest.TestCase):
    def test_home_contains_local_parser_and_three_document_upload(self) -> None:
        html = asyncio.run(index())
        self.assertIn("本地解析", html)
        self.assertIn("三份身份证明", html)
        self.assertIn("HMRC_MFA_PHONE_IS_UK:'No'", html)
        self.assertIn("HMRC_MFA_PHONE_COUNTRY:'China'", html)
        self.assertIn("确认资料无误", html)
        self.assertIn("extracted_confirmed:extractionConfirmed", html)
        self.assertNotIn("$('email').value = extractedValues.email", html)
        self.assertNotIn("$('phone').value = extractedValues.phone", html)
        self.assertIn("标准税率营业额 £10,000", html)
        self.assertIn("SIC 行业代码 47910", html)
        self.assertIn("暂停自动化", html)
        self.assertIn("应用修改并继续", html)
        self.assertIn("/api/pause", html)
        self.assertIn("/api/continue", html)

    def test_start_rejects_unconfirmed_extraction(self) -> None:
        manager = JobManager()
        with self.assertRaisesRegex(ValueError, "检查并确认"):
            manager.start(
                StartRequest(
                    config_path="vat-config.test.json",
                    extracted_confirmed=False,
                )
            )

    def test_pause_checkpoint_keeps_job_alive_and_returns_updated_values(self) -> None:
        manager = JobManager()
        with manager._lock:
            manager._state["status"] = "running"
        manager.pause()

        async def scenario() -> dict[str, str]:
            task = asyncio.create_task(
                manager._pause_checkpoint(
                    "https://example.test/register-for-vat/telephone-number",
                    "What is your telephone number?",
                )
            )
            for _ in range(20):
                await asyncio.sleep(0.005)
                if manager.snapshot()["status"] == "paused":
                    break
            self.assertEqual(manager.snapshot()["status"], "paused")
            manager.continue_after_pause(
                ContinueRequest(
                    extracted_values={"phone": "8613592850576"},
                    extracted_confirmed=True,
                )
            )
            return await task

        updates = asyncio.run(scenario())
        self.assertEqual(updates, {"phone": "8613592850576"})
        self.assertEqual(manager.snapshot()["status"], "running")

    def test_extracted_project_reference_overrides_configured_reference(self) -> None:
        manager = JobManager()
        settings = Settings(
            start_url="https://example.test",
            profile_dir=Path(".browser-profile"),
            artifacts_dir=Path("artifacts"),
            answers={},
            pages=[
                PageRule(
                    path_contains="/register-for-vat/application-reference",
                    answers={"value": "OLD-REFERENCE"},
                )
            ],
        )
        manager._apply_extracted_values(
            settings,
            {
                "application_reference": (
                    "AB223322-UK-Hangzhou Fell Wheel Technology Co., Ltd"
                )
            },
        )
        self.assertEqual(
            settings.answer_for(
                "value",
                "https://example.test/register-for-vat/application-reference",
                "Choose an application reference",
            ),
            (True, "AB223322-UK-Hangzhou Fell Wheel Technology Co., Ltd"),
        )

    def test_parse_document_endpoint(self) -> None:
        result = asyncio.run(
            parse_document(
                UploadFile(
                    filename="vat.txt",
                    file=io.BytesIO(b"Business name: Endpoint Ltd"),
                )
            )
        )
        self.assertEqual(result["values"]["business_name"], "Endpoint Ltd")

    def test_identity_documents_require_exactly_three(self) -> None:
        with self.assertRaises(HTTPException) as context:
            asyncio.run(
                identity_documents(
                    [UploadFile(filename="one.pdf", file=io.BytesIO(b"one"))]
                )
            )
        self.assertEqual(context.exception.status_code, 400)

    def test_identity_files_are_private_and_provided_in_order(self) -> None:
        manager = JobManager()
        manager.store_identity_documents(
            [("one.pdf", b"one"), ("two.png", b"two"), ("three.txt", b"three")]
        )
        manager._active_identity_documents = list(manager._identity_documents)
        paths = [Path(asyncio.run(manager._identity_file("document"))) for _ in range(3)]
        self.assertEqual([path.read_bytes() for path in paths], [b"one", b"two", b"three"])
        self.assertTrue(all(path.stat().st_mode & 0o777 == 0o600 for path in paths))
        self.assertFalse(asyncio.run(manager._identity_files_remaining()))

    def test_invalid_second_file_cleans_partial_write(self) -> None:
        manager = JobManager()
        before = set(manager._upload_dir.iterdir())
        with self.assertRaisesRegex(ValueError, "不支持"):
            manager.store_identity_documents(
                [("one.pdf", b"one"), ("bad.exe", b"bad"), ("three.txt", b"three")]
            )
        self.assertEqual(set(manager._upload_dir.iterdir()), before)


if __name__ == "__main__":
    unittest.main()
