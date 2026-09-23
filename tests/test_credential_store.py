import json
import tempfile
import unittest
from pathlib import Path

from vat_automation.credential_store import CredentialStore, mask_value


class CredentialStoreTests(unittest.TestCase):
    """按客户/账号保存 HMRC 登录信息，文件 0600，对外只给掩码。"""

    def setUp(self) -> None:
        self._workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self._workspace.cleanup)
        self.path = Path(self._workspace.name) / "credentials.json"
        self.store = CredentialStore(self.path)

    def test_save_get_and_clear(self) -> None:
        self.store.save(
            "cust-1",
            {
                "HMRC_EMAIL": "a@example.test",
                "HMRC_USER_ID": "123456789012",
                "HMRC_PASSWORD": "pw",
                "HMRC_MFA_PHONE": "13900000000",
                "NOT_ALLOWED": "忽略我",
            },
        )
        values = self.store.get("cust-1")
        self.assertEqual(values["HMRC_PASSWORD"], "pw")
        self.assertEqual(values["HMRC_USER_ID"], "123456789012")
        self.assertNotIn("NOT_ALLOWED", values)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.store.clear("cust-1")
        self.assertEqual(self.store.get("cust-1"), {})

    def test_merge_keeps_other_keys(self) -> None:
        self.store.save("cust-1", {"HMRC_EMAIL": "a@example.test", "HMRC_PASSWORD": "pw1"})
        self.store.save("cust-1", {"HMRC_USER_ID": "123456789012"})
        values = self.store.get("cust-1")
        self.assertEqual(values["HMRC_EMAIL"], "a@example.test")
        self.assertEqual(values["HMRC_PASSWORD"], "pw1")
        self.assertEqual(values["HMRC_USER_ID"], "123456789012")

    def test_public_view_is_masked_and_has_no_secret(self) -> None:
        self.store.save(
            "cust-1",
            {"HMRC_EMAIL": "a@example.test", "HMRC_USER_ID": "123456789012", "HMRC_PASSWORD": "pw"},
        )
        public = self.store.public("cust-1")
        self.assertEqual(public["user_id"], "12********12")
        self.assertTrue(public["has_password"])
        self.assertNotIn("pw", json.dumps(public, ensure_ascii=False))
        self.assertFalse(self.store.public("missing")["saved"])

    def test_empty_values_are_rejected_and_corrupt_file_is_tolerated(self) -> None:
        with self.assertRaises(ValueError):
            self.store.save("cust-1", {"HMRC_PASSWORD": "  "})
        self.path.write_text("{ not json", encoding="utf-8")
        self.assertEqual(self.store.get("cust-1"), {})

    def test_mask_value(self) -> None:
        self.assertEqual(mask_value("123456789012"), "12********12")
        self.assertEqual(mask_value("1234"), "****")


if __name__ == "__main__":
    unittest.main()
