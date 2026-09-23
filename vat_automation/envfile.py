"""项目 `.env` 的读写：加载进程环境、把新账号写回文件。

`.env` 已加入 .gitignore，权限 0600，用于放本机凭据（百度翻译、HMRC 登录信息）。
"""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Iterable, Mapping
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


def default_env_path() -> Path:
    """已有 .env 就用它，否则用当前目录下的 .env。"""
    for candidate in env_file_candidates():
        if candidate.is_file():
            return candidate
    return env_file_candidates()[0]


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


def env_credentials(
    keys: Iterable[str], environ: Mapping[str, str] | None = None
) -> dict[str, str]:
    """从进程环境（含 .env 载入的值）取指定凭据键，只返回非空的。"""
    source = os.environ if environ is None else environ
    return {
        key: str(source[key]).strip()
        for key in keys
        if str(source.get(key) or "").strip()
    }


def _encode_env_value(value: str) -> str:
    """写出安全的 .env 值；含引号或换行的值拒绝写入，避免写坏文件。"""
    text = str(value)
    if "\n" in text or "\r" in text:
        raise ValueError("登录信息里不能有换行符，无法写入 .env。")
    if '"' in text:
        raise ValueError('登录信息里不能有英文双引号，请先在终端环境变量里设置。')
    if text.strip() != text or re.search(r"[\s#'\"]", text):
        return f'"{text}"'
    return text


def upsert_env_file(path: Path, values: Mapping[str, str]) -> Path:
    """把键值合并写回 .env：保留注释和其它键，权限 0600，原子替换。"""
    remaining = {
        key: _encode_env_value(value)
        for key, value in values.items()
        if str(value).strip()
    }
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    lines: list[str] = []
    for raw in existing.splitlines():
        key = ""
        stripped = raw.strip()
        if stripped and not stripped.startswith("#"):
            body = stripped[7:].lstrip() if stripped.startswith("export ") else stripped
            if "=" in body:
                key = body.split("=", 1)[0].strip()
        if key and key in remaining:
            lines.append(f"{key}={remaining.pop(key)}")
            continue
        lines.append(raw)
    if remaining:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("# HMRC 登录信息（本机私密文件，勿提交）")
        lines.extend(f"{key}={value}" for key, value in remaining.items())
    text = "\n".join(lines).rstrip("\n") + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=".env-", suffix=".tmp"
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    except Exception:
        Path(temp_name).unlink(missing_ok=True)
        raise
    path.chmod(0o600)
    return path
