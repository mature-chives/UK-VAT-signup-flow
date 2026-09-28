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
    country = split_address_country(raw)[1]
    if country:
        return country
    # 中文地址常以国家开头，国家名与省市之间不一定有空格。
    for prefix, name in (("中国香港", "Hong Kong"), ("香港", "Hong Kong"), ("中华人民共和国", "China"), ("中国", "China"), ("爱尔兰", "Ireland"), ("英国", "United Kingdom")):
        if raw.strip().startswith(prefix):
            return name
    return ""


def infer_address_country(raw: str) -> str:
    """优先使用明确国家；中国省市名称只用于生成待人工核对的提取结果。"""
    explicit = address_country(raw)
    if explicit:
        return explicit
    provinces = "河北|山西|辽宁|吉林|黑龙江|江苏|浙江|安徽|福建|江西|山东|河南|湖北|湖南|广东|海南|四川|贵州|云南|陕西|甘肃|青海"
    cities = "北京|上海|天津|重庆|杭州|宁波|温州|南京|苏州|广州|深圳|成都|武汉|西安|郑州|长沙|福州|厦门|济南|青岛|合肥|南昌|昆明|贵阳|南宁|海口|沈阳|大连|长春|哈尔滨|石家庄|太原|兰州|西宁|银川|乌鲁木齐|拉萨|呼和浩特"
    if re.search(r"(?:" + provinces + r")省|(?:" + cities + r")市|内蒙古自治区|广西壮族自治区|西藏自治区|宁夏回族自治区|新疆维吾尔自治区", raw):
        return "China"
    # 英文地址只取独立省份片段，避免把街道名中的地名当作国家。
    english = "Hebei|Shanxi|Liaoning|Jilin|Heilongjiang|Jiangsu|Zhejiang|Anhui|Fujian|Jiangxi|Shandong|Henan|Hubei|Hunan|Guangdong|Hainan|Sichuan|Guizhou|Yunnan|Shaanxi|Gansu|Qinghai|Guangxi|Xinjiang|Ningxia|Inner Mongolia"
    for part in re.split(r"[,，;；\n]", raw):
        if re.fullmatch(r"(?:" + english + r")(?:\s+(?:Province|Sheng|Autonomous Region))?", part.strip(), re.IGNORECASE):
            return "China"
    return ""
