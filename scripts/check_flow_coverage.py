from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vat_automation.screenshot_flow import (  # noqa: E402
    GENERIC_ANSWER_PATHS,
    SCREENSHOT_PATHS,
    SPECIAL_HANDLED_PATHS,
)


def normalized(path: str) -> str:
    return path.replace("{id}", "")


def main() -> int:
    config_path = Path(sys.argv[1] if len(sys.argv) > 1 else "vat-config.test.json")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    configured = [
        rule.get("match", {}).get("path_contains", "")
        for rule in config.get("pages", [])
    ]
    uncovered: list[str] = []
    for path in SCREENSHOT_PATHS:
        candidate = normalized(path)
        covered = any(item and item in candidate for item in configured)
        covered = covered or any(item in candidate for item in SPECIAL_HANDLED_PATHS)
        covered = covered or any(item in candidate for item in GENERIC_ANSWER_PATHS)
        if not covered:
            uncovered.append(path)
    print(
        json.dumps(
            {
                "screenshot_pages": len(SCREENSHOT_PATHS),
                "covered": len(SCREENSHOT_PATHS) - len(uncovered),
                "uncovered": uncovered,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 1 if uncovered else 0


if __name__ == "__main__":
    raise SystemExit(main())
