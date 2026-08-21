from __future__ import annotations

import os
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]


def env_file_candidates() -> list[Path]:
    seen: set[Path] = set()
    paths: list[Path] = []
    for item in (Path.cwd() / ".env", _REPO_ROOT / ".env"):
        resolved = item.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        paths.append(resolved)
    return paths


def parse_env_text(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key and value:
            values[key] = value
    return values


def apply_env(values: dict[str, str], environ: dict[str, str] | None = None) -> list[str]:
    target = os.environ if environ is None else environ
    loaded: list[str] = []
    for key, value in values.items():
        if not value or (target.get(key) or "").strip():
            continue
        target[key] = value
        loaded.append(key)
    return loaded


def load_env_file(path: Path | None = None) -> Path | None:
    """把 KEY=VALUE 写入进程环境。已存在且非空的变量不覆盖。"""
    candidates = [path.resolve()] if path is not None else env_file_candidates()
    for candidate in candidates:
        if not candidate.is_file():
            continue
        values = parse_env_text(candidate.read_text(encoding="utf-8-sig"))
        apply_env(values)
        return candidate
    return None
