"""HMRC 登录信息的本机私有存储（0600 JSON，git 忽略）。

工作台按客户存、单用户 vat-web 按账号存，都用这一份实现，避免两边规则不一致。
只存白名单键；对外只暴露掩码后的 User ID，绝不返回值本身。
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


CREDENTIAL_KEYS = ("HMRC_EMAIL", "HMRC_USER_ID", "HMRC_PASSWORD", "HMRC_MFA_PHONE")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def mask_value(value: str) -> str:
    """掩码保留首尾各两位，长度不足时全部打码。"""
    text = str(value)
    if len(text) <= 4:
        return "*" * len(text)
    return f"{text[:2]}{'*' * (len(text) - 4)}{text[-2:]}"


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        stream = os.fdopen(descriptor, "w", encoding="utf-8")
    except BaseException:
        os.close(descriptor)
        raise
    with stream:
        stream.write(text)
    path.chmod(0o600)


class CredentialStore:
    """`owner_id`（工作台用客户 ID，vat-web 用账号名）→ 登录信息。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.path.is_file():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        owners = raw.get("owners", {})
        return owners if isinstance(owners, dict) else {}

    def _save(self, owners: dict[str, dict[str, Any]]) -> None:
        payload = json.dumps(
            {"version": 1, "owners": owners}, ensure_ascii=False, indent=2
        )
        _write_private(self.path, payload + "\n")

    def get(self, owner_id: str) -> dict[str, str]:
        with self._lock:
            record = self._load().get(owner_id) or {}
        values = record.get("values", {})
        return {
            key: str(value)
            for key, value in dict(values).items()
            if key in CREDENTIAL_KEYS and str(value).strip()
        }

    def save(self, owner_id: str, values: Mapping[str, str]) -> dict[str, Any]:
        clean = {
            key: str(value).strip()
            for key, value in values.items()
            if key in CREDENTIAL_KEYS and str(value).strip()
        }
        if not clean:
            raise ValueError("没有可保存的登录信息。")
        with self._lock:
            owners = self._load()
            existing = owners.get(owner_id) or {}
            previous = dict(existing.get("values", {}))
            if (clean.get("HMRC_USER_ID") and previous.get("HMRC_USER_ID")
                    and clean["HMRC_USER_ID"] != previous["HMRC_USER_ID"]):
                for key in ("HMRC_EMAIL", "HMRC_MFA_METHOD", "HMRC_MFA_PHONE",
                            "HMRC_MFA_PHONE_IS_UK", "HMRC_MFA_PHONE_COUNTRY"):
                    previous.pop(key, None)
            merged = {**previous, **clean}
            record = {"values": merged, "updated_at": _now()}
            owners[owner_id] = record
            self._save(owners)
        return dict(record)

    def clear(self, owner_id: str) -> None:
        with self._lock:
            owners = self._load()
            if owner_id in owners:
                del owners[owner_id]
                self._save(owners)

    def public(self, owner_id: str) -> dict[str, Any]:
        """给界面看的脱敏信息：不包含任何明文凭据。"""
        with self._lock:
            record = self._load().get(owner_id) or {}
        values = dict(record.get("values", {}))
        if not values:
            return {
                "saved": False,
                "user_id": "",
                "email": "",
                "has_password": False,
                "has_phone": False,
                "updated_at": "",
            }
        return {
            "saved": True,
            "user_id": mask_value(values.get("HMRC_USER_ID", "")),
            "email": str(values.get("HMRC_EMAIL", "")),
            "has_password": bool(values.get("HMRC_PASSWORD", "")),
            "has_phone": bool(values.get("HMRC_MFA_PHONE", "")),
            "updated_at": str(record.get("updated_at", "")),
        }
