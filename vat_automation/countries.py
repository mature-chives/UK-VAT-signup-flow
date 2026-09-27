"""国家字段归一化；地址中只识别明确的完整国家片段。"""
from __future__ import annotations

import re


def _key(value: str) -> str:
    return re.sub(r"[\W_]+", "", value.casefold())


_NAMES = {
    "China": ("中国", "中国内地", "中国大陆", "中华人民共和国", "PRC", "Mainland China"),
    "Hong Kong": ("香港", "中国香港", "香港中国", "香港特别行政区", "Hong Kong China", "China Hong Kong", "Hong Kong SAR"),
    "Ireland": ("爱尔兰", "愛爾蘭", "Republic of Ireland"),
    "United Kingdom": ("英国", "英國", "Great Britain"),
}
_ALIASES = {_key(alias): name for name, aliases in _NAMES.items() for alias in (name, *aliases)}
_CODES = {"cn": "China", "chn": "China", "hk": "Hong Kong", "hkg": "Hong Kong", "ie": "Ireland", "irl": "Ireland", "gb": "United Kingdom", "gbr": "United Kingdom", "uk": "United Kingdom"}


def normalize_country(value: str) -> str:
    """未知国家保留人工填写值，交给 HMRC 实际选项匹配。"""
    return _ALIASES.get(_key(value), _CODES.get(_key(value), value.strip()))


def split_address_country(raw: str) -> tuple[str, str]:
    """分离地址末尾的完整国家名，允许国家紧跟邮编，不识别短代码。"""
    address = raw.strip(" ,，;；\n")
    # Northern Ireland 不能按 Ireland 后缀识别为爱尔兰共和国。
    if re.search(r"\bNorthern\s+Ireland$", address, re.IGNORECASE):
        return address, ""
    aliases = sorted(
        ((alias, name) for name, names in _NAMES.items() for alias in (name, *names)),
        key=lambda item: len(item[0]), reverse=True,
    )
    for alias, name in aliases:
        pattern = r"[\s,，]+".join(re.escape(part) for part in alias.split())
        match = re.search(r"(?:^|[\s,，;；])(" + pattern + r")$", address, re.IGNORECASE)
        if match:
            return address[:match.start(1)].strip(" ,，;；\n"), name
    return address, ""


def address_country(raw: str) -> str:
    return split_address_country(raw)[1]
