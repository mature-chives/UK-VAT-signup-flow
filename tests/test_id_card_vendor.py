from __future__ import annotations

import asyncio
import hashlib
import os
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs

import httpx

from workbench.id_card_translate import (
    BAIDU_TRANSLATE_ENDPOINT,
    VENDOR_REJECT_MESSAGE,
    VendorConfigError,
    VendorHttpError,
    VendorReject,
    VendorRequest,
    _ID_CARD_FIELD_KEYS,
    async_translate_id_card_fields,
    explain_baidu_error,
    get_vendor,
    prepare_vendor_text,
    BaiduTranslateVendor,
)


def _baidu_ok(dst: str) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "from": "zh",
                "to": "en",
                "trans_result": [{"src": "src", "dst": dst}],
            },
        )

    return httpx.MockTransport(handler)


class PrepareVendorTextTests(unittest.TestCase):
    def test_normal_address_passes(self) -> None:
        self.assertEqual(
            prepare_vendor_text("北京市东城区幽静街111号"),
            "北京市东城区幽静街111号",
        )

    def test_rejects_digit_runs_and_fullwidth(self) -> None:
        cases = (
            "450821197308071202",
            "４５０８２１197308071202",
            "450821 19730807 1202",
            "450821-19730807-1202",
            {"identity_document_number": "450821197308071202", "address": "北京市"},
            str(
                {
                    "full_name": "赵君楚",
                    "identity_document_number": "450821197308071202",
                    "address": "广西壮族自治区贵港市平南县上渡街道",
                }
            ),
        )
        for item in cases:
            with self.subTest(item=item):
                with self.assertRaises(VendorReject) as caught:
                    prepare_vendor_text(item)
                self.assertEqual(str(caught.exception), VENDOR_REJECT_MESSAGE)


class BaiduVendorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._sleep = patch("workbench.id_card_translate.asyncio.sleep", new_callable=AsyncMock)
        self._sleep.start()

    def tearDown(self) -> None:
        self._sleep.stop()

    def test_sign_matches_official_md5(self) -> None:
        vendor = BaiduTranslateVendor(
            app_id="2015063000000001",
            secret="12345678",
            salt_factory=lambda: "1435660288",
            transport=_baidu_ok("apple"),
        )
        expected = hashlib.md5(
            "2015063000000001apple143566028812345678".encode("utf-8")
        ).hexdigest()
        self.assertEqual(vendor._sign("apple", "1435660288"), expected)

    def test_outbound_body_is_only_one_text_field(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                200,
                json={
                    "from": "zh",
                    "to": "en",
                    "trans_result": [
                        {
                            "src": "北京市东城区幽静街111号",
                            "dst": "No. 111 Youjing Street, Dongcheng District, Beijing",
                        }
                    ],
                },
            )

        vendor = BaiduTranslateVendor(
            app_id="app-id",
            secret="app-secret",
            salt_factory=lambda: "9",
            transport=httpx.MockTransport(handler),
        )
        dst = asyncio.run(
            vendor.translate(
                VendorRequest(text="北京市东城区幽静街111号", target_lang="en")
            )
        )
        self.assertIn("Beijing", dst)
        self.assertEqual(len(captured), 1)
        request = captured[0]
        self.assertEqual(str(request.url), BAIDU_TRANSLATE_ENDPOINT)
        body = request.content.decode("utf-8")
        form = {key: values[0] for key, values in parse_qs(body).items()}
        self.assertEqual(
            set(form),
            {"q", "from", "to", "appid", "salt", "sign"},
        )
        self.assertEqual(form["q"], "北京市东城区幽静街111号")
        self.assertEqual(form["from"], "zh")
        self.assertEqual(form["to"], "en")
        self.assertEqual(form["appid"], "app-id")
        for key in _ID_CARD_FIELD_KEYS:
            self.assertNotIn(f"{key}=", body)
            if key != "address":
                self.assertNotIn(key, body)
        self.assertNotIn("450821197308071202", body)
        self.assertNotIn("identity_document_number", body)
        expected_sign = hashlib.md5(
            "app-id北京市东城区幽静街111号9app-secret".encode("utf-8")
        ).hexdigest()
        self.assertEqual(form["sign"], expected_sign)

    def test_logger_does_not_contain_source_text(self) -> None:
        vendor = BaiduTranslateVendor(
            app_id="app-id",
            secret="app-secret",
            transport=_baidu_ok("Beijing"),
        )
        with self.assertLogs("workbench.id_card_translate", level="INFO") as logs:
            asyncio.run(
                vendor.translate(VendorRequest(text="北京市东城区幽静街111号"))
            )
        joined = "\n".join(logs.output)
        self.assertNotIn("北京市", joined)
        self.assertNotIn("幽静", joined)
        self.assertNotIn("app-secret", joined)
        self.assertIn("chars=", joined)

    def test_http_error_and_vendor_error_code(self) -> None:
        def timeout_handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("slow")

        vendor = BaiduTranslateVendor(
            app_id="app-id",
            secret="app-secret",
            transport=httpx.MockTransport(timeout_handler),
        )
        with self.assertRaises(VendorHttpError) as timed_out:
            asyncio.run(vendor.translate(VendorRequest(text="北京市")))
        self.assertIn("没有响应", str(timed_out.exception))
        self.assertNotIn("Timeout", str(timed_out.exception))

        def api_error(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"error_code": "54001", "error_msg": "Invalid Sign"})

        vendor = BaiduTranslateVendor(
            app_id="app-id",
            secret="app-secret",
            transport=httpx.MockTransport(api_error),
        )
        with self.assertRaises(VendorHttpError) as signed:
            asyncio.run(vendor.translate(VendorRequest(text="北京市")))
        message = str(signed.exception)
        self.assertIn("密钥不正确", message)
        self.assertNotIn("Invalid Sign", message)
        self.assertNotIn("54001", message)
        self.assertNotIn("TRANSLATE_", message)
        self.assertEqual(signed.exception.code, "54001")

    def test_invalid_client_ip_is_rewritten_for_operators(self) -> None:
        wrapped = explain_baidu_error(
            {
                "error_code": "58000",
                "error_msg": "INVALID_CLIENT_IP",
                "data": {"client_ip": "116.30.229.105"},
            }
        )
        self.assertIn("116.30.229.105", wrapped)
        self.assertIn("允许列表", wrapped)
        self.assertNotIn("INVALID_CLIENT_IP", wrapped)
        self.assertNotIn("58000", wrapped)
        self.assertNotIn("error_msg", wrapped)
        self.assertIn("允许列表", explain_baidu_error({"error_code": "58000"}))
        unknown = explain_baidu_error({"error_code": "59999", "error_msg": "WEIRD"})
        self.assertNotIn("59999", unknown)
        self.assertNotIn("WEIRD", unknown)
        self.assertNotIn("北京市", explain_baidu_error({"error_code": "58000", "q": "北京市"}))

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "error_code": "58000",
                    "error_msg": "INVALID_CLIENT_IP",
                    "data": {"client_ip": "116.30.229.105"},
                },
            )

        vendor = BaiduTranslateVendor(
            app_id="app-id",
            secret="app-secret",
            transport=httpx.MockTransport(handler),
        )
        with self.assertRaises(VendorHttpError) as caught:
            asyncio.run(vendor.translate(VendorRequest(text="北京市东城区幽静街111号")))
        message = str(caught.exception)
        self.assertIn("116.30.229.105", message)
        self.assertIn("允许列表", message)
        self.assertNotIn("幽静", message)
        self.assertNotIn("INVALID_CLIENT_IP", message)
        self.assertEqual(caught.exception.code, "58000")

    def test_retries_rate_limit_then_succeeds(self) -> None:
        hits = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            hits["n"] += 1
            if hits["n"] == 1:
                return httpx.Response(200, json={"error_code": "54003", "error_msg": "Limit"})
            return httpx.Response(200, json={"trans_result": [{"dst": "Beijing"}]})

        vendor = BaiduTranslateVendor(
            app_id="app-id",
            secret="app-secret",
            transport=httpx.MockTransport(handler),
        )
        dst = asyncio.run(vendor.translate(VendorRequest(text="北京市")))
        self.assertEqual(dst, "Beijing")
        self.assertEqual(hits["n"], 2)

    def test_rejects_id_number_before_http(self) -> None:
        called = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            called["n"] += 1
            return httpx.Response(200, json={"trans_result": [{"dst": "x"}]})

        vendor = BaiduTranslateVendor(
            app_id="app-id",
            secret="app-secret",
            transport=httpx.MockTransport(handler),
        )
        with self.assertRaises(VendorReject):
            asyncio.run(
                vendor.translate(VendorRequest(text="450821197308071202"))
            )
        self.assertEqual(called["n"], 0)


class VendorSelectionTests(unittest.TestCase):
    def test_missing_vendor_returns_none(self) -> None:
        env = {"TRANSLATE_VENDOR": "", "TRANSLATE_APP_ID": "", "TRANSLATE_API_KEY": ""}
        with patch.dict(os.environ, env, clear=False):
            self.assertIsNone(get_vendor())

    def test_baidu_without_keys_raises(self) -> None:
        env = {
            "TRANSLATE_VENDOR": "baidu",
            "TRANSLATE_APP_ID": "",
            "TRANSLATE_API_KEY": "",
        }
        with patch.dict(os.environ, env, clear=False):
            with self.assertRaises(VendorConfigError):
                get_vendor()

    def test_keys_without_vendor_name_select_baidu(self) -> None:
        env = {
            "TRANSLATE_VENDOR": "",
            "TRANSLATE_APP_ID": "app-id",
            "TRANSLATE_API_KEY": "app-secret",
        }
        with patch.dict(os.environ, env, clear=False):
            vendor = get_vendor()
        self.assertIsInstance(vendor, BaiduTranslateVendor)

    def test_async_pack_sends_only_address_and_authority(self) -> None:
        seen: list[str] = []

        class FakeVendor:
            async def translate(self, req: VendorRequest) -> str:
                seen.append(req.text)
                return f"EN:{req.text}"

        fields = {
            "full_name": "潘涵涵",
            "sex": "男",
            "ethnicity": "汉",
            "birth_date": "2006年8月10日",
            "address": "北京市东城区幽静街111号",
            "identity_document_number": "110101200608104339",
            "issuing_authority": "北京市公安局",
            "valid_period": "2026.08.10-2031.08.10",
        }
        translated = asyncio.run(
            async_translate_id_card_fields(fields, vendor=FakeVendor())
        )
        self.assertEqual(seen, ["北京市东城区幽静街111号", "北京市公安局"])
        self.assertEqual(translated["full_name"], "Pan Hanhan")
        self.assertEqual(translated["identity_document_number"], "110101200608104339")
        self.assertEqual(translated["address"], "EN:北京市东城区幽静街111号")
        self.assertEqual(translated["issuing_authority"], "EN:北京市公安局")


if __name__ == "__main__":
    unittest.main()
