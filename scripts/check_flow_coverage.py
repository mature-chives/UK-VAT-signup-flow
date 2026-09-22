from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vat_automation.config import normalize  # noqa: E402
from vat_automation.eori_flow import (  # noqa: E402
    EORI_SCREENSHOT_HEADINGS,
    EORI_SCREENSHOT_PATHS,
    EORI_SPECIAL_HANDLED_PATHS,
)
from vat_automation.screenshot_flow import (  # noqa: E402
    GENERIC_ANSWER_PATHS,
    SCREENSHOT_PATHS,
    SPECIAL_HANDLED_PATHS,
)

DEFAULT_CONFIGS = {
    "vat": "vat-config.test.json",
    "eori": "vat-config.eori.flow.json",
}


def normalized(path: str) -> str:
    return path.replace("{id}", "")


def unmatched_paths(
    paths: tuple[str, ...], configured: list[str], special: tuple[str, ...]
) -> list[str]:
    uncovered: list[str] = []
    for path in paths:
        candidate = normalized(path)
        covered = any(item and item in candidate for item in configured)
        covered = covered or any(item in candidate for item in special)
        if not covered:
            uncovered.append(path)
    return uncovered


def unmatched_headings(
    headings: tuple[str, ...], configured: list[str]
) -> list[str]:
    uncovered: list[str] = []
    for heading in headings:
        wanted = normalize(heading)
        if not any(item and normalize(item) in wanted for item in configured):
            uncovered.append(heading)
    return uncovered


def check_vat(config: dict[str, object]) -> dict[str, object]:
    configured = [
        rule.get("match", {}).get("path_contains", "")
        for rule in config.get("pages", [])
    ]
    uncovered = unmatched_paths(
        SCREENSHOT_PATHS,
        [*configured, *GENERIC_ANSWER_PATHS],
        SPECIAL_HANDLED_PATHS,
    )
    return {
        "flow": "vat",
        "screenshot_pages": len(SCREENSHOT_PATHS),
        "covered": len(SCREENSHOT_PATHS) - len(uncovered),
        "uncovered": uncovered,
    }


def check_eori(config: dict[str, object]) -> dict[str, object]:
    rules = config.get("pages", [])
    configured_paths = [
        str(rule.get("match", {}).get("path_contains", "")) for rule in rules
    ]
    configured_headings = [
        str(rule.get("match", {}).get("heading_contains", "")) for rule in rules
    ]
    uncovered_paths = unmatched_paths(
        EORI_SCREENSHOT_PATHS, configured_paths, EORI_SPECIAL_HANDLED_PATHS
    )
    uncovered_headings = unmatched_headings(
        EORI_SCREENSHOT_HEADINGS, configured_headings
    )
    total = len(EORI_SCREENSHOT_PATHS) + len(EORI_SCREENSHOT_HEADINGS)
    return {
        "flow": "eori",
        "screenshot_pages": total,
        "covered": total - len(uncovered_paths) - len(uncovered_headings),
        "uncovered": [*uncovered_paths, *uncovered_headings],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="检查截图页面是否都有流程规则")
    parser.add_argument("config", nargs="?", type=Path, default=None)
    parser.add_argument("--flow", choices=sorted(DEFAULT_CONFIGS), default="vat")
    args = parser.parse_args()
    config_path = args.config or Path(DEFAULT_CONFIGS[args.flow])
    config = json.loads(config_path.read_text(encoding="utf-8"))
    result = check_vat(config) if args.flow == "vat" else check_eori(config)
    uncovered = result["uncovered"]
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if uncovered else 0


if __name__ == "__main__":
    raise SystemExit(main())
