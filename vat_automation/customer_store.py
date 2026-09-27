"""vat-web 的用户私有客户、已确认资料和每次办理记录。"""
from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from .document_parser import VAT_REGISTRATION_FIELD_KEYS
from .company_identity import company_name_key, identify_company, identity_signature, normalized_identity_values


def now() -> str:
    return datetime.now(UTC).isoformat()


class CustomerStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self._lock = threading.RLock()

    @staticmethod
    def _id(value: str) -> str:
        if not re.fullmatch(r"[a-f0-9]{32}", value):
            raise KeyError("客户或记录不存在")
        return value

    def owner_dir(self, owner: str) -> Path:
        return self.root / self._id(owner)

    def _read(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        # 损坏时拒绝继续，不能把旧资料悄悄当空文件覆盖。
        return json.loads(path.read_text(encoding="utf-8"))

    def _write(self, path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        parent = path.parent
        while parent.is_relative_to(self.root):
            parent.chmod(0o700)
            if parent == self.root:
                break
            parent = parent.parent
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".pending-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def list(self, owner: str) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._read(self.owner_dir(owner) / "customers.json").values())

    def get(self, owner: str, customer_id: str) -> dict[str, Any]:
        with self._lock:
            record = self._read(self.owner_dir(owner) / "customers.json").get(self._id(customer_id))
            if not record:
                raise KeyError("客户不存在或不属于当前用户")
            return record

    def create(self, owner: str, name: str) -> dict[str, Any]:
        name = name.strip()
        if not name or len(name) > 200:
            raise ValueError("请填写 1–200 字的客户名称")
        with self._lock:
            path = self.owner_dir(owner) / "customers.json"
            records = self._read(path)
            record = {"id": secrets.token_hex(16), "name": name, "created_at": now()}
            records[record["id"]] = record
            self._write(path, records)
            return record

    def preview(self, values: Mapping[str, str]) -> dict[str, Any]:
        """只读预览；公司基础索引不含账号、证件、联系人或申请内容。"""
        identity = identify_company(values)
        result: dict[str, Any] = {"identity": identity, "status": "unresolved", "company": None}
        if not identity["valid"]:
            return result
        with self._lock:
            company = self._read(self.root / "companies.json").get(identity["key"])
        if not company:
            result["status"] = "new"
            return result
        different_name = company_name_key(company["name"]) != company_name_key(values.get("business_name", ""))
        old_date = company.get("incorporation_date", "")
        new_date = values.get("company_incorporation_date", "").strip()
        result.update(status="conflict" if different_name or (old_date and new_date and old_date != new_date) else "matched", company=company)
        return result

    def search_companies(self, query: str) -> list[dict[str, Any]]:
        needle = company_name_key(query)
        if len(needle) < 2:
            return []
        with self._lock:
            companies = self._read(self.root / "companies.json").values()
            return [c for c in companies if needle in company_name_key(c["name"]) or needle in c["number"]][:20]

    def confirm_company(
        self, owner: str, flow: str, values: Mapping[str, str],
        current_id: str = "", independent: bool = False,
    ) -> dict[str, Any]:
        clean = {k: v for k, v in normalized_identity_values(values).items() if k in VAT_REGISTRATION_FIELD_KEYS}
        name = clean.get("business_name", "").strip()
        if not name or len(name) > 200:
            raise ValueError("请核对公司名称（1–200 字）。")
        with self._lock:
            current = self.get(owner, current_id) if current_id else None
            preview = self.preview(clean)
            if preview["status"] == "conflict" and not independent:
                raise ValueError("编号匹配，但公司名称或成立日期与已有档案冲突。请核对原件、修正资料，或明确选择仅作为独立申请；不会覆盖已有公司。")
            company = None
            if preview["identity"]["valid"] and not independent:
                company = preview["company"]
                if not company:
                    identity = preview["identity"]
                    company = {
                        "id": secrets.token_hex(16), "name": name,
                        "jurisdiction": identity["jurisdiction"], "type": identity["type"],
                        "number": identity["number"], "incorporation_date": clean.get("company_incorporation_date", ""),
                    }
                    companies = self._read(self.root / "companies.json")
                    companies[identity["key"]] = company
                    self._write(self.root / "companies.json", companies)
            path = self.owner_dir(owner) / "customers.json"
            records = self._read(path)
            signature = identity_signature(clean)
            record = next((r for r in records.values() if company and r.get("company_id") == company["id"]), None)
            if not company and current and not current.get("company_id") and current.get("identity_signature") == signature:
                record = current
            if record is None:
                record = {
                    "id": secrets.token_hex(16), "name": name, "created_at": now(),
                    "company_id": company["id"] if company else "",
                    "identity_key": preview["identity"]["key"] if company else "",
                    "identity_signature": signature,
                }
                records[record["id"]] = record
                self._write(path, records)
            self.save_draft(owner, record["id"], flow, clean)
            return {"customer_id": record["id"], "customer_name": record["name"], "values": clean,
                    "association": "linked" if company else "independent", "preview": preview}

    def validate_company(self, owner: str, customer_id: str, values: Mapping[str, str]) -> None:
        record = self.get(owner, customer_id)
        if "identity_signature" not in record:
            return  # 历史手动档案保持私有，不自动共享或迁移。
        if record["identity_signature"] != identity_signature(values):
            raise ValueError("公司身份信息已更改，请先重新确认资料；运行中不能换成另一家公司。")
        if record.get("company_id") and self.preview(values)["status"] == "conflict":
            raise ValueError("公司名称或成立日期与已关联公司冲突，请核对后重新确认。")

    def folder(self, owner: str, customer_id: str, flow: str) -> Path:
        self.get(owner, customer_id)
        if flow not in {"vat", "eori"}:
            raise ValueError("未知业务")
        return self.owner_dir(owner) / customer_id / flow

    def draft(self, owner: str, customer_id: str, flow: str) -> dict[str, Any]:
        with self._lock:
            result = self._read(self.folder(owner, customer_id, flow) / "draft.json")
            if not result:
                other = "eori" if flow == "vat" else "vat"
                result = self._read(self.folder(owner, customer_id, other) / "draft.json")
            return result

    def save_draft(self, owner: str, customer_id: str, flow: str, values: Mapping[str, str]) -> None:
        self.validate_company(owner, customer_id, values)
        clean = {key: str(value) for key, value in values.items() if key in VAT_REGISTRATION_FIELD_KEYS}
        with self._lock:
            self._write(self.folder(owner, customer_id, flow) / "draft.json", {"values": clean, "updated_at": now()})

    def save_run(self, owner: str, customer_id: str, flow: str, record: dict[str, Any]) -> None:
        with self._lock:
            path = self.folder(owner, customer_id, flow) / "runs" / self._id(record["id"]) / "record.json"
            self._write(path, record)

    def runs(self, owner: str, customer_id: str, flow: str) -> list[dict[str, Any]]:
        with self._lock:
            folder = self.folder(owner, customer_id, flow) / "runs"
            return sorted((self._read(path) for path in folder.glob("*/record.json")), key=lambda r: r["started_at"], reverse=True)

    def run(self, owner: str, customer_id: str, flow: str, run_id: str) -> dict[str, Any]:
        with self._lock:
            result = self._read(self.folder(owner, customer_id, flow) / "runs" / self._id(run_id) / "record.json")
            if not result:
                raise KeyError("办理记录不存在")
            return result
