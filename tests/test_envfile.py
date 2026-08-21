from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from workbench.envfile import apply_env, load_env_file, parse_env_text


class EnvFileTests(unittest.TestCase):
    def test_parse_skips_comments_and_empty(self) -> None:
        text = """
# comment
TRANSLATE_VENDOR=baidu
export TRANSLATE_APP_ID='123456'
TRANSLATE_API_KEY="secret value"
EMPTY=
not-a-pair
"""
        self.assertEqual(
            parse_env_text(text),
            {
                "TRANSLATE_VENDOR": "baidu",
                "TRANSLATE_APP_ID": "123456",
                "TRANSLATE_API_KEY": "secret value",
            },
        )

    def test_apply_does_not_override_existing(self) -> None:
        environ = {"TRANSLATE_API_KEY": "from-shell", "TRANSLATE_APP_ID": ""}
        loaded = apply_env(
            {
                "TRANSLATE_API_KEY": "from-file",
                "TRANSLATE_APP_ID": "app-id",
                "TRANSLATE_VENDOR": "baidu",
            },
            environ,
        )
        self.assertEqual(environ["TRANSLATE_API_KEY"], "from-shell")
        self.assertEqual(environ["TRANSLATE_APP_ID"], "app-id")
        self.assertEqual(environ["TRANSLATE_VENDOR"], "baidu")
        self.assertEqual(loaded, ["TRANSLATE_APP_ID", "TRANSLATE_VENDOR"])

    def test_load_env_file_reads_path(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / ".env"
            path.write_text(
                "TRANSLATE_VENDOR=baidu\nTRANSLATE_APP_ID=app-id\n",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"TRANSLATE_VENDOR": "", "TRANSLATE_APP_ID": ""},
                clear=False,
            ):
                loaded = load_env_file(path)
                self.assertEqual(os.environ.get("TRANSLATE_VENDOR"), "baidu")
                self.assertEqual(os.environ.get("TRANSLATE_APP_ID"), "app-id")
            self.assertEqual(loaded, path.resolve())
            self.assertIsNone(load_env_file(Path(raw) / "missing.env"))


if __name__ == "__main__":
    unittest.main()
