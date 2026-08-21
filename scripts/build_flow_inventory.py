from __future__ import annotations

import argparse
import json
import re
from collections import OrderedDict
from pathlib import Path
from urllib.parse import urlsplit


DOMAIN_RE = re.compile(
    r"(?P<url>(?:(?:www\.)?(?:tax|access)\.service\.gov\.uk|www\.gov\.uk)/[^\s\"<>]+)",
    re.IGNORECASE,
)
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f-]{20,}$", re.IGNORECASE)


def canonicalize(raw: str) -> tuple[str, str]:
    value = raw.strip("•- .,:;?").replace("register-tor", "register-for")
    value = value.replace("multi-tactor", "multi-factor")
    value = value.replace("check-IT-you-can-register-tor", "check-if-you-can-register-for")
    value = value.replace("tlat-rate", "flat-rate")
    value = value.replace("classitication", "classification")
    value = value.replace("suomit", "submit").replace("cneck", "check")
    value = value.replace("email-address-veritied", "email-address-verified")
    if not value.startswith("http"):
        value = "https://" + value
    parsed = urlsplit(value)
    parts = [part for part in parsed.path.split("/") if part]
    canonical: list[str] = []
    for part in parts:
        if UUID_RE.match(part):
            canonical.append("{id}")
        else:
            canonical.append(part)
    return value, "/" + "/".join(canonical)


def useful_lines(lines: list[str]) -> list[str]:
    rejected = (
        "class=", "govuk-", "data-", "aria-", "<input", "<div", "</",
        "computed", "styles", "elements", "console", "devtools", "filter",
        "font-size", "margin", "padding", "width:", "height:", "cursor:",
        "chrome", "cookies", "accessibility statement", "privacy policy",
        "terms and conditions", "crown copyright", "what's new",
    )
    result: list[str] = []
    seen: set[str] = set()
    for line in lines:
        text = re.sub(r"\s+", " ", line).strip(" •›")
        folded = text.casefold()
        if not text or len(text) < 2 or len(text) > 180:
            continue
        if any(token in folded for token in rejected):
            continue
        if DOMAIN_RE.search(text) or text in seen:
            continue
        if re.fullmatch(r"[\W_\d]+", text):
            continue
        seen.add(text)
        result.append(text)
    return result[:80]


def build(ocr_path: Path) -> list[dict[str, object]]:
    grouped: OrderedDict[str, dict[str, object]] = OrderedDict()
    for raw_line in ocr_path.read_text(encoding="utf-8").splitlines():
        record = json.loads(raw_line)
        detected = ""
        for line in record["lines"]:
            match = DOMAIN_RE.search(line)
            if match:
                detected = match.group("url")
                break
        if not detected:
            continue
        url, path = canonicalize(detected)
        item = grouped.setdefault(
            path,
            {"path": path, "first_url": url, "screenshots": [], "text": []},
        )
        item["screenshots"].append(record["file"])
        combined = list(item["text"]) + useful_lines(record["lines"])
        item["text"] = list(dict.fromkeys(combined))[:120]
    return list(grouped.values())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ocr", type=Path, default=Path("artifacts-flow/ocr.jsonl"))
    parser.add_argument(
        "--output", type=Path, default=Path("artifacts-flow/flow-inventory.json")
    )
    args = parser.parse_args()
    inventory = build(args.ocr)
    args.output.write_text(
        json.dumps(inventory, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"pages={len(inventory)} output={args.output}")


if __name__ == "__main__":
    main()
