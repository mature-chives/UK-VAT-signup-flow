from __future__ import annotations

import json
import os
import secrets
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from vat_automation.credential_store import CredentialStore


def _now() -> str:
    return datetime.now(UTC).isoformat()


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


class WorkbenchStore:
    """客户资料与任务。文件按客户/任务隔离，插件不各自建库。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.root.chmod(0o700)
        self.customers_path = root / "customers.json"
        self.tasks_path = root / "tasks.json"
        self.credentials_path = root / "credentials.json"
        self.credentials = CredentialStore(self.credentials_path)
        self.files_root = root / "files"
        self.files_root.mkdir(parents=True, exist_ok=True)
        self.files_root.chmod(0o700)
        self._lock = threading.Lock()

    def _load(self, path: Path, key: str) -> dict[str, dict[str, Any]]:
        if not path.is_file():
            return {}
        raw = json.loads(path.read_text(encoding="utf-8"))
        items = raw.get(key, {})
        return items if isinstance(items, dict) else {}

    def _save(self, path: Path, key: str, items: dict[str, dict[str, Any]]) -> None:
        payload = json.dumps({"version": 1, key: items}, ensure_ascii=False, indent=2)
        _write_private(path, payload + "\n")

    def list_customers(self) -> list[dict[str, Any]]:
        with self._lock:
            return sorted(self._load(self.customers_path, "customers").values(), key=lambda item: item.get("name", ""))

    def get_customer(self, customer_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._load(self.customers_path, "customers").get(customer_id)

    def upsert_customer(
        self,
        *,
        customer_id: str | None,
        name: str,
        project_code: str = "",
        fields: dict[str, str] | None = None,
        notes: str = "",
    ) -> dict[str, Any]:
        name = name.strip()
        if not name:
            raise ValueError("客户名称不能为空。")
        with self._lock:
            items = self._load(self.customers_path, "customers")
            now = _now()
            if customer_id:
                existing = items.get(customer_id)
                if existing is None:
                    raise KeyError(customer_id)
                existing.update(
                    {
                        "name": name,
                        "project_code": project_code.strip(),
                        "notes": notes,
                        "fields": {
                            key: value
                            for key, value in (fields or existing.get("fields", {})).items()
                            if str(value).strip()
                        },
                        "updated_at": now,
                    }
                )
                record = existing
            else:
                customer_id = secrets.token_hex(8)
                record = {
                    "id": customer_id,
                    "name": name,
                    "project_code": project_code.strip(),
                    "notes": notes,
                    "fields": {
                        key: value
                        for key, value in (fields or {}).items()
                        if str(value).strip()
                    },
                    "files": [],
                    "created_at": now,
                    "updated_at": now,
                }
                items[customer_id] = record
            self._save(self.customers_path, "customers", items)
            return dict(record)

    def add_customer_file(
        self,
        customer_id: str,
        *,
        filename: str,
        content: bytes,
        category: str,
    ) -> dict[str, Any]:
        if not content:
            raise ValueError("文件为空。")
        if len(content) > 25 * 1024 * 1024:
            raise ValueError("文件超过 25MB。")
        suffix = Path(filename).suffix.casefold()
        allowed = {
            ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".pdf",
            ".doc", ".docx", ".xls", ".xlsx", ".txt",
        }
        if suffix not in allowed:
            raise ValueError(f"不支持的文件格式：{filename}")
        with self._lock:
            items = self._load(self.customers_path, "customers")
            customer = items.get(customer_id)
            if customer is None:
                raise KeyError(customer_id)
            file_id = secrets.token_hex(8)
            folder = self.files_root / "customers" / customer_id
            folder.mkdir(parents=True, exist_ok=True)
            folder.chmod(0o700)
            path = folder / f"{file_id}{suffix}"
            path.write_bytes(content)
            path.chmod(0o600)
            record = {
                "id": file_id,
                "name": Path(filename).name,
                "category": category or "other",
                "path": str(path),
            }
            files = list(customer.get("files", []))
            files.append(record)
            customer["files"] = files
            customer["updated_at"] = _now()
            self._save(self.customers_path, "customers", items)
            return dict(record)

    def customer_file(self, customer_id: str, file_id: str) -> dict[str, Any] | None:
        customer = self.get_customer(customer_id)
        if customer is None:
            return None
        for item in customer.get("files", []):
            if item.get("id") == file_id:
                return item
        return None

    # ---- 按客户的 HMRC 登录信息（0600，只服务本机自动化）----

    def get_credentials(self, customer_id: str) -> dict[str, str]:
        return self.credentials.get(customer_id)

    def save_credentials(
        self, customer_id: str, values: dict[str, str]
    ) -> dict[str, Any]:
        """把登录信息挂到客户上；只接收白名单键，空值忽略。"""
        return self.credentials.save(customer_id, values)

    def clear_credentials(self, customer_id: str) -> None:
        self.credentials.clear(customer_id)

    def public_credentials(self, customer_id: str) -> dict[str, Any]:
        """给界面看的脱敏信息：只暴露是否保存、掩码后的 User ID 和更新时间。"""
        return self.credentials.public(customer_id)

    def list_tasks(self) -> list[dict[str, Any]]:
        with self._lock:
            return sorted(
                self._load(self.tasks_path, "tasks").values(),
                key=lambda item: item.get("created_at", ""),
                reverse=True,
            )

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._load(self.tasks_path, "tasks").get(task_id)

    def create_task(
        self,
        *,
        plugin_id: str,
        plugin_name: str,
        task_kind: str,
        customer_id: str,
        title: str,
        created_by: str,
        field_keys: list[str],
        file_ids: list[str],
        placeholder: bool,
    ) -> dict[str, Any]:
        customer = self.get_customer(customer_id)
        if customer is None:
            raise KeyError(customer_id)
        selected_fields = {
            key: customer.get("fields", {}).get(key, "")
            for key in field_keys
            if customer.get("fields", {}).get(key)
        }
        selected_files = [
            item
            for item in customer.get("files", [])
            if item.get("id") in set(file_ids)
        ]
        status = "queued" if placeholder else "open"
        record = {
            "id": secrets.token_hex(8),
            "plugin_id": plugin_id,
            "plugin_name": plugin_name,
            "task_kind": task_kind,
            "placeholder": placeholder,
            "customer_id": customer_id,
            "customer_name": customer.get("name", ""),
            "title": title or f"{plugin_name} · {customer.get('name', '')}",
            "status": status,
            "message": "能力尚未接入，任务已占位。" if placeholder else "已创建，待处理",
            "created_by": created_by,
            "created_at": _now(),
            "updated_at": _now(),
            "selected_fields": selected_fields,
            "selected_files": selected_files,
            "result_files": [],
        }
        with self._lock:
            items = self._load(self.tasks_path, "tasks")
            items[record["id"]] = record
            self._save(self.tasks_path, "tasks", items)
        return dict(record)

    def update_task(self, task_id: str, **changes: Any) -> dict[str, Any]:
        with self._lock:
            items = self._load(self.tasks_path, "tasks")
            record = items.get(task_id)
            if record is None:
                raise KeyError(task_id)
            record.update(changes)
            record["updated_at"] = _now()
            self._save(self.tasks_path, "tasks", items)
            return dict(record)

    def add_task_result_file(
        self, task_id: str, *, filename: str, content: bytes, kind: str
    ) -> dict[str, Any]:
        if not content:
            raise ValueError("文件为空。")
        suffix = Path(filename).suffix.casefold() or ".bin"
        with self._lock:
            items = self._load(self.tasks_path, "tasks")
            record = items.get(task_id)
            if record is None:
                raise KeyError(task_id)
            file_id = secrets.token_hex(8)
            folder = self.files_root / "tasks" / task_id
            folder.mkdir(parents=True, exist_ok=True)
            folder.chmod(0o700)
            path = folder / f"{file_id}{suffix}"
            path.write_bytes(content)
            path.chmod(0o600)
            item = {
                "id": file_id,
                "name": Path(filename).name,
                "kind": kind,
                "path": str(path),
            }
            files = list(record.get("result_files", []))
            files.append(item)
            record["result_files"] = files
            if kind == "translation":
                record["status"] = "delivered"
                record["message"] = "译文已回传"
            elif kind == "source":
                record["status"] = "in_progress"
                record["message"] = "原件已上传，待回传译文"
            record["updated_at"] = _now()
            self._save(self.tasks_path, "tasks", items)
            return dict(item)
