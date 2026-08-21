from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import secrets
import unicodedata
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from pypinyin import Style, lazy_pinyin

logger = logging.getLogger("workbench.id_card_translate")

BAIDU_TRANSLATE_ENDPOINT = "https://fanyi-api.baidu.com/api/trans/vip/translate"
VENDOR_NARRATIVE_FIELDS = ("address", "issuing_authority")
_ID_CARD_FIELD_KEYS = (
    "full_name",
    "sex",
    "ethnicity",
    "birth_date",
    "address",
    "identity_document_number",
    "issuing_authority",
    "valid_period",
)
_VENDOR_DIGIT_RUN = re.compile(r"\d{15,18}")
_VENDOR_STRIP = re.compile(r"[\s\-\u00a0\u3000\u2010-\u2015\u2212\uff0d]")
VENDOR_REJECT_MESSAGE = "住址/签发机关含连续数字，请从校对表去掉号码后再试"
VENDOR_FAIL_RETRY = "没法生成翻译件，请稍后再试。若连续失败，请联系管理员。"
RETRYABLE_VENDOR_CODES = {"54003", "54005", "52001", "timeout"}
VENDOR_RETRY_DELAY_S = 1.2
VENDOR_FIELD_GAP_S = 1.1
_CLIENT_IP = re.compile(r"^[0-9A-Fa-f.:]{3,45}$")
# 只给工作台操作员看：不出现供应商错误码、英文原文、密钥变量名。
_TRANSLATE_USER_HINTS = {
    "timeout": "翻译接口暂时没有响应，请稍后再生成。",
    "error": VENDOR_FAIL_RETRY,
    "bad_json": VENDOR_FAIL_RETRY,
    "52001": "翻译接口暂时没有响应，请稍后再生成。",
    "52002": "翻译接口出错，请稍后再生成。",
    "52003": "翻译接口还没开通，或账号无效。请管理员检查翻译服务配置后再试。",
    "54000": "翻译服务配置不完整。请管理员把密钥填好后再生成。",
    "54001": "翻译服务密钥不正确。请管理员核对后再生成。",
    "54003": "翻译调用太频繁，请等一会儿再生成。",
    "54004": "翻译服务额度用完了。请管理员充值后再生成。",
    "54005": "翻译调用太频繁，请等一会儿再生成。",
    "58000": (
        "这台电脑现在的上网地址还不能使用翻译接口。"
        "请管理员把该地址加入允许列表；如果地址经常变，就把地址限制关掉，然后再生成。"
    ),
    "58001": "翻译接口不支持这次的语言，请联系管理员。",
    "58002": "翻译接口已关闭。请管理员重新开通后再生成。",
    "90107": "翻译服务账号还没通过认证。请管理员处理后再生成。",
}

ID_CARD_EN_LABELS = {
    "full_name": "Name",
    "sex": "Sex",
    "ethnicity": "Ethnicity",
    "birth_date": "Date of Birth",
    "address": "Residential Address",
    "identity_document_number": "Citizen ID number",
    "issuing_authority": "Authority",
    "valid_period": "Valid through",
}

SEX_EN = {"男": "Male", "女": "Female"}

ETHNICITY_EN = {
    "汉": "Han",
    "汉族": "Han",
    "蒙古": "Mongol",
    "蒙古族": "Mongol",
    "回": "Hui",
    "回族": "Hui",
    "藏": "Tibetan",
    "藏族": "Tibetan",
    "维吾尔": "Uygur",
    "维吾尔族": "Uygur",
    "苗": "Miao",
    "苗族": "Miao",
    "彝": "Yi",
    "彝族": "Yi",
    "壮": "Zhuang",
    "壮族": "Zhuang",
    "布依": "Buyei",
    "朝鲜": "Korean",
    "满": "Manchu",
    "满族": "Manchu",
    "侗": "Dong",
    "瑶": "Yao",
    "白": "Bai",
    "土家": "Tujia",
    "哈尼": "Hani",
    "哈萨克": "Kazak",
    "傣": "Dai",
    "黎": "Li",
}

_PLACE_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("特别行政区", "Special Administrative Region"),
    ("壮族自治区", "Zhuang Autonomous Region"),
    ("回族自治区", "Hui Autonomous Region"),
    ("维吾尔自治区", "Uygur Autonomous Region"),
    ("自治区", "Autonomous Region"),
    ("自治州", "Autonomous Prefecture"),
    ("自治县", "Autonomous County"),
    ("街道", "Sub-district"),
    ("地区", "Prefecture"),
    ("省", "Province"),
    ("市", "City"),
    ("区", "District"),
    ("县", "County"),
    ("镇", "Town"),
    ("乡", "Township"),
    ("路", "Road"),
    ("街", "Street"),
    ("号", "No."),
)

_MUNICIPALITIES = {
    "北京": "Beijing",
    "上海": "Shanghai",
    "天津": "Tianjin",
    "重庆": "Chongqing",
}

_BIRTH = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
_MONTHS = (
    "",
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def romanize_name(chinese: str) -> str:
    parts = [item for item in lazy_pinyin(chinese.strip(), style=Style.NORMAL) if item]
    if not parts:
        return chinese.strip()
    surname = parts[0].capitalize()
    given = "".join(parts[1:]).capitalize()
    return f"{surname} {given}".strip()


def _romanize_token(token: str) -> str:
    if not token:
        return ""
    if token in _MUNICIPALITIES:
        return _MUNICIPALITIES[token]
    syllables = lazy_pinyin(token, style=Style.NORMAL)
    return "".join(syllables).capitalize() if syllables else token


def translate_place(chinese: str) -> str:
    text = re.sub(r"\s+", "", chinese or "")
    if not text:
        return ""
    chunks: list[str] = []
    rest = text
    while rest:
        matched = False
        best: tuple[int, str, str] | None = None
        for suffix, english in _PLACE_SUFFIXES:
            index = rest.find(suffix)
            if index < 0:
                continue
            if (
                best is None
                or index < best[0]
                or (index == best[0] and len(suffix) > len(best[1]))
            ):
                best = (index, suffix, english)
        if best is not None:
            index, suffix, english = best
            head, rest = rest[:index], rest[index + len(suffix) :]
            if suffix == "号":
                number = head if head.isdigit() else _romanize_token(head)
                chunks.append(f"No. {number}")
            elif head in _MUNICIPALITIES and suffix in {"市", "区"}:
                chunks.append(_MUNICIPALITIES[head])
            else:
                place = _romanize_token(head) if head else ""
                chunks.append(f"{place} {english}".strip())
            continue
        chunks.append(_romanize_token(rest))
        break
    return ", ".join(item for item in chunks if item)


def translate_authority(chinese: str) -> str:
    text = re.sub(r"\s+", "", chinese or "")
    if not text:
        return ""
    match = re.match(r"^(.*)公安局(.+)分局$", text)
    if match:
        city = translate_place(match.group(1))
        district = translate_place(match.group(2))
        if match.group(2) and "区" not in match.group(2) and "县" not in match.group(2):
            district = f"{_romanize_token(match.group(2))} District"
        return f"Public Security Sub-Bureau of {district}, {city}"
    match = re.match(r"^(.*)公安分局$", text)
    if match:
        return f"Public Security Sub-Bureau of {translate_place(match.group(1))}"
    match = re.match(r"^(.*)公安局$", text)
    if match:
        return f"Public Security Bureau of {translate_place(match.group(1))}"
    return translate_place(text)


def translate_birth_date(chinese: str) -> str:
    match = _BIRTH.search(chinese or "")
    if not match:
        return (chinese or "").strip()
    year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
    if 1 <= month <= 12:
        return f"{day} {_MONTHS[month]} {year}"
    return chinese.strip()


def translate_valid_period(chinese: str) -> str:
    text = (chinese or "").strip()
    text = text.replace("—", "-").replace("～", "-").replace("~", "-")
    text = re.sub(r"\s*-\s*", " -- ", text)
    return text


class VendorReject(ValueError):
    """住址/签发机关未通过出网前过滤。"""


class VendorConfigError(RuntimeError):
    """TRANSLATE_VENDOR 已开但密钥或供应商无效。"""


class VendorHttpError(RuntimeError):
    """云翻译 HTTP 失败或供应商返回错误码。"""

    def __init__(self, message: str, *, code: str = "") -> None:
        super().__init__(message)
        self.code = code


def _safe_client_ip(value: object) -> str:
    text = str(value or "").strip()
    return text if _CLIENT_IP.fullmatch(text) else ""


def wrap_translate_error(*, code: str = "", body: dict[str, Any] | None = None) -> str:
    """把供应商返回收成操作员能看懂的一句话。禁止回传 error_msg / 错误码原文。"""
    key = str(code or (body or {}).get("error_code") or "").strip()
    payload = body if isinstance(body, dict) else {}
    if key == "58000":
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        ip = _safe_client_ip((data or {}).get("client_ip"))
        if ip:
            return (
                f"这台电脑现在的上网地址 {ip} 还不能使用翻译接口。"
                "请管理员把这个地址加入允许列表；如果地址经常变，就把地址限制关掉，然后再生成。"
            )
    return _TRANSLATE_USER_HINTS.get(key, VENDOR_FAIL_RETRY)


def explain_baidu_error(body: dict[str, Any]) -> str:
    return wrap_translate_error(body=body, code=str(body.get("error_code") or ""))


@dataclass(frozen=True, slots=True)
class VendorRequest:
    text: str
    target_lang: str = "en"


class VendorClient(Protocol):
    async def translate(self, req: VendorRequest) -> str: ...


def prepare_vendor_text(text: object) -> str:
    """检测连续证件号码；返回原始文本供 POST，不把压缩结果发出去。"""
    if not isinstance(text, str):
        raise VendorReject(VENDOR_REJECT_MESSAGE)
    original = text.strip()
    if not original:
        return ""
    compact = _VENDOR_STRIP.sub("", unicodedata.normalize("NFKC", original))
    if _VENDOR_DIGIT_RUN.search(compact):
        raise VendorReject(VENDOR_REJECT_MESSAGE)
    return original


class BaiduTranslateVendor:
    def __init__(
        self,
        app_id: str,
        secret: str,
        endpoint: str = BAIDU_TRANSLATE_ENDPOINT,
        *,
        timeout: float = 10.0,
        transport: Any = None,
        salt_factory: Any | None = None,
        retry_delay_s: float = VENDOR_RETRY_DELAY_S,
        max_retries: int = 2,
    ) -> None:
        self._app_id = app_id
        self._secret = secret
        self.endpoint = endpoint
        self.timeout = timeout
        self._transport = transport
        self._salt_factory = salt_factory or (lambda: str(secrets.randbelow(2**31)))
        self._retry_delay_s = retry_delay_s
        self._max_retries = max_retries

    def _sign(self, query: str, salt: str) -> str:
        raw = f"{self._app_id}{query}{salt}{self._secret}"
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    async def translate(self, req: VendorRequest) -> str:
        text = prepare_vendor_text(req.text)
        if not text:
            return ""
        attempts = self._max_retries + 1
        last_error: VendorHttpError | None = None
        for attempt in range(attempts):
            salt = str(self._salt_factory())
            payload = {
                "q": text,
                "from": "zh",
                "to": req.target_lang or "en",
                "appid": self._app_id,
                "salt": salt,
                "sign": self._sign(text, salt),
            }
            try:
                return await self._once(payload, text)
            except VendorHttpError as exc:
                last_error = exc
                if exc.code not in RETRYABLE_VENDOR_CODES or attempt + 1 >= attempts:
                    raise
                logger.info(
                    "vendor translate chars=%s status=%s code=%s retry=%s",
                    len(text),
                    "retry",
                    exc.code,
                    attempt + 1,
                )
                if self._retry_delay_s > 0:
                    await asyncio.sleep(self._retry_delay_s)
        raise last_error or VendorHttpError(VENDOR_FAIL_RETRY)

    async def _once(self, payload: dict[str, str], text: str) -> str:
        status: object = "error"
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self._transport) as client:
                response = await client.post(self.endpoint, data=payload)
            status = response.status_code
            response.raise_for_status()
            body = response.json()
        except httpx.TimeoutException as exc:
            logger.info("vendor translate chars=%s status=%s code=%s", len(text), status, "timeout")
            raise VendorHttpError(wrap_translate_error(code="timeout"), code="timeout") from exc
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            logger.info("vendor translate chars=%s status=%s code=%s", len(text), status, "error")
            raise VendorHttpError(wrap_translate_error(code="error"), code="error") from exc
        if not isinstance(body, dict):
            logger.info("vendor translate chars=%s status=%s code=%s", len(text), status, "bad_json")
            raise VendorHttpError(wrap_translate_error(code="bad_json"), code="bad_json")
        code = str(body.get("error_code") or "")
        logger.info("vendor translate chars=%s status=%s code=%s", len(text), status, code or "ok")
        if code and code not in {"0", "52000"}:
            raise VendorHttpError(wrap_translate_error(code=code, body=body), code=code)
        parts = body.get("trans_result") or []
        if not isinstance(parts, list):
            raise VendorHttpError(VENDOR_FAIL_RETRY, code=code)
        dst = " ".join(str(item.get("dst") or "").strip() for item in parts if isinstance(item, dict)).strip()
        if not dst:
            raise VendorHttpError(VENDOR_FAIL_RETRY, code=code)
        return dst


def get_vendor() -> VendorClient | None:
    name = os.environ.get("TRANSLATE_VENDOR", "").strip().lower()
    app_id = os.environ.get("TRANSLATE_APP_ID", "").strip()
    secret = os.environ.get("TRANSLATE_API_KEY", "").strip()
    if not name or name in {"local", "none", "null"}:
        if app_id and secret:
            name = "baidu"
        else:
            return None
    if name != "baidu":
        raise VendorConfigError("未配置翻译服务")
    if not app_id or not secret:
        raise VendorConfigError("未配置翻译服务")
    endpoint = os.environ.get("TRANSLATE_ENDPOINT", "").strip() or BAIDU_TRANSLATE_ENDPOINT
    return BaiduTranslateVendor(app_id=app_id, secret=secret, endpoint=endpoint)


async def async_translate_id_card_fields(
    fields: dict[str, str],
    *,
    vendor: VendorClient | None = None,
    field_gap_s: float | None = None,
) -> dict[str, str]:
    translated = translate_id_card_fields(fields)
    client = get_vendor() if vendor is None else vendor
    if client is None:
        return translated
    chinese = {key: str(value).strip() for key, value in fields.items() if str(value).strip()}
    pending = [
        key for key in VENDOR_NARRATIVE_FIELDS if chinese.get(key)
    ]
    if field_gap_s is None:
        gap = VENDOR_FIELD_GAP_S if isinstance(client, BaiduTranslateVendor) else 0.0
    else:
        gap = field_gap_s
    for index, key in enumerate(pending):
        if index and gap > 0:
            await asyncio.sleep(gap)
        translated[key] = await client.translate(
            VendorRequest(text=chinese[key], target_lang="en")
        )
    return translated


def translate_id_card_fields(fields: dict[str, str]) -> dict[str, str]:
    """本地翻译身份证字段。住址/签发机关在配置云翻译后由 async 路径覆盖。"""
    chinese = {key: str(value).strip() for key, value in fields.items() if str(value).strip()}
    translated: dict[str, str] = {}
    if chinese.get("full_name"):
        translated["full_name"] = romanize_name(chinese["full_name"])
    if chinese.get("sex"):
        translated["sex"] = SEX_EN.get(chinese["sex"], chinese["sex"])
    if chinese.get("ethnicity"):
        translated["ethnicity"] = ETHNICITY_EN.get(
            chinese["ethnicity"], _romanize_token(chinese["ethnicity"])
        )
    if chinese.get("birth_date"):
        translated["birth_date"] = translate_birth_date(chinese["birth_date"])
    if chinese.get("address"):
        translated["address"] = translate_place(chinese["address"])
    if chinese.get("identity_document_number"):
        translated["identity_document_number"] = chinese["identity_document_number"]
    if chinese.get("issuing_authority"):
        translated["issuing_authority"] = translate_authority(chinese["issuing_authority"])
    if chinese.get("valid_period"):
        translated["valid_period"] = translate_valid_period(chinese["valid_period"])
    return translated


def classify_id_card_side(fields: dict[str, str]) -> str:
    if fields.get("full_name") or fields.get("identity_document_number"):
        return "front"
    if fields.get("issuing_authority") or fields.get("valid_period"):
        return "back"
    return "unknown"


_PORTRAIT_LABELS = {"image", "figure", "photo"}


def normalize_bbox(
    bbox: list[float] | list[int] | None, width: int, height: int
) -> list[int] | None:
    if not bbox:
        return None
    try:
        nums = [float(item) for item in bbox]
    except (TypeError, ValueError):
        return None
    if len(nums) == 8:
        xs, ys = nums[0::2], nums[1::2]
        nums = [min(xs), min(ys), max(xs), max(ys)]
    if len(nums) != 4:
        return None
    x1, y1, x2, y2 = nums
    if x2 < x1:
        x1, x2 = x2, x1
    if y2 < y1:
        y1, y2 = y2, y1
    if width and height and x2 <= 1.5 and y2 <= 1.5:
        x1, x2 = x1 * width, x2 * width
        y1, y2 = y1 * height, y2 * height
    box = [
        max(0, int(x1)),
        max(0, int(y1)),
        min(width, int(x2)) if width else int(x2),
        min(height, int(y2)) if height else int(y2),
    ]
    if box[2] - box[0] < 20 or box[3] - box[1] < 20:
        return None
    return box


def _bbox_from_block(raw: object) -> list[float] | None:
    if isinstance(raw, (list, tuple)) and len(raw) in {4, 8}:
        try:
            return [float(item) for item in raw]
        except (TypeError, ValueError):
            return None
    return None


def extract_portrait_bbox(source: dict[str, Any]) -> list[int] | None:
    width = int(source.get("width") or 0)
    height = int(source.get("height") or 0)
    area = width * height if width and height else 0
    candidates: list[list[int]] = []
    blocks = list(source.get("parsing_res_list") or [])
    layout = source.get("layout_det_res") or {}
    for item in layout.get("boxes") or []:
        if isinstance(item, dict):
            blocks.append(
                {
                    "block_label": item.get("label") or item.get("block_label"),
                    "block_bbox": item.get("coordinate") or item.get("block_bbox"),
                }
            )
    for block in blocks:
        if not isinstance(block, dict):
            continue
        label = str(block.get("block_label") or block.get("label") or "").casefold()
        if label not in _PORTRAIT_LABELS:
            continue
        raw = _bbox_from_block(block.get("block_bbox") or block.get("bbox") or block.get("coordinate"))
        if raw is None:
            continue
        coords = normalize_bbox(raw, width, height)
        if coords is None:
            continue
        if area:
            box_area = max(0, coords[2] - coords[0]) * max(0, coords[3] - coords[1])
            ratio = box_area / area
            if ratio > 0.55:
                continue
        candidates.append(coords)
    if not candidates:
        return None
    return max(candidates, key=lambda box: (box[0] + box[2]) / 2)
