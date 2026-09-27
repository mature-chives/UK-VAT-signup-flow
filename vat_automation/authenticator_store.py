"""TOTP 与本机加密保管：不向环境变量、日志或普通凭据文件写入种子。"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import struct
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qs, urlparse


class AuthenticatorError(RuntimeError):
    """错误只使用固定文案，不包含密钥、二维码或动态码。"""


@dataclass(slots=True)
class TotpParameters:
    secret: str = field(repr=False)
    algorithm: str = "SHA1"
    digits: int = 6
    period: int = 30

    def __post_init__(self) -> None:
        self.secret = re.sub(r"[\s-]", "", self.secret).upper().rstrip("=")
        if not re.fullmatch(r"[A-Z2-7]{16,128}", self.secret):
            raise AuthenticatorError("Authenticator 密钥格式无效，已停止自动绑定。")
        try:
            decoded = base64.b32decode(self.secret + "=" * (-len(self.secret) % 8))
        except (ValueError, binascii.Error):
            raise AuthenticatorError("Authenticator 密钥不能解码。") from None
        if len(decoded) < 10 or self.algorithm not in {"SHA1", "SHA256", "SHA512"}:
            raise AuthenticatorError("不支持该 Authenticator 密钥或算法。")
        if type(self.digits) is not int or self.digits not in {6, 8} or type(self.period) is not int or not 15 <= self.period <= 120:
            raise AuthenticatorError("Authenticator 位数或更新周期不受支持。")

    @classmethod
    def from_uri(cls, uri: str) -> TotpParameters:
        try:
            parsed = urlparse(uri)
            values = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
            if parsed.scheme != "otpauth" or parsed.netloc != "totp" or parsed.fragment:
                raise ValueError
            if any(len(items) != 1 for items in values.values()) or "secret" not in values:
                raise ValueError
            # HOTP 的 counter 不能混入基于时间的 TOTP 配置。
            if "counter" in values:
                raise ValueError
            return cls(values["secret"][0], values.get("algorithm", ["SHA1"])[0].upper(),
                       int(values.get("digits", ["6"])[0]), int(values.get("period", ["30"])[0]))
        except (TypeError, ValueError):
            raise AuthenticatorError("二维码不是受支持的标准 TOTP 配置。") from None

    def code(self, counter: int) -> str:
        key = base64.b32decode(self.secret + "=" * (-len(self.secret) % 8))
        digest = hmac.new(key, struct.pack(">Q", counter), self.algorithm.lower()).digest()
        offset = digest[-1] & 15
        number = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7fffffff) % (10 ** self.digits)
        return str(number).zfill(self.digits)


class AuthenticatorStore:
    def __init__(self, directory: Path | None = None, key_path: Path | None = None) -> None:
        root = Path(__file__).resolve().parents[1]
        self.directory = directory or root / ".authenticator"
        self.key_path = key_path or root / ".authenticator-key"
        self.db_path = self.directory / "vault.sqlite3"

    @staticmethod
    def _crypto() -> Any:
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError:
            raise AuthenticatorError("缺少 Authenticator 加密依赖，请先更新项目依赖。") from None
        return AESGCM

    def prepare(self) -> None:
        self._crypto()
        if self.directory.is_symlink() or self.key_path.is_symlink():
            raise AuthenticatorError("Authenticator 私有目录和主密钥不能使用符号链接。")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.directory.stat().st_mode & 0o077:
            raise AuthenticatorError("Authenticator 私有目录权限必须为 0700。")
        if not self.key_path.exists():
            if self.db_path.exists():
                raise AuthenticatorError("Authenticator 主密钥丢失，不能自动重建；请恢复原密钥。")
            # 完整写入并刷盘后再原子发布，避免并发任务读到半写入的主密钥。
            descriptor, temporary = tempfile.mkstemp(prefix=".authenticator-key-", dir=self.key_path.parent)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(secrets.token_bytes(32))
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.link(temporary, self.key_path)
                except FileExistsError:
                    pass
            finally:
                Path(temporary).unlink(missing_ok=True)
        self._key()
        if not self.db_path.exists():
            try:
                os.close(os.open(self.db_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
            except FileExistsError:
                pass
        if self.db_path.is_symlink() or self.db_path.stat().st_mode & 0o077:
            raise AuthenticatorError("Authenticator 数据库权限必须为 0600。")

    def _key(self) -> bytes:
        if self.key_path.is_symlink() or self.key_path.stat().st_mode & 0o077:
            raise AuthenticatorError("Authenticator 主密钥权限必须为 0600。")
        key = self.key_path.read_bytes()
        if len(key) != 32:
            raise AuthenticatorError("Authenticator 主密钥格式无效。")
        return key

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            self.prepare()
            db = sqlite3.connect(self.db_path, timeout=5)
            db.row_factory = sqlite3.Row
            try:
                db.execute("""CREATE TABLE IF NOT EXISTS entries (
                    entry_id TEXT PRIMARY KEY, encrypted BLOB NOT NULL,
                    status TEXT NOT NULL, counter INTEGER NOT NULL DEFAULT -1,
                    updated REAL NOT NULL)""")
                db.commit()
                db.execute("BEGIN IMMEDIATE")
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()
        except (OSError, sqlite3.Error):
            raise AuthenticatorError("Authenticator 私有存储不可用，已停止自动处理。") from None

    @staticmethod
    def _identity(scope: str, gateway: str) -> str:
        if not scope or not re.fullmatch(r"[0-9]{10,12}", gateway):
            raise AuthenticatorError("缺少明确的客户归属或 GG 账号，不能托管 Authenticator 密钥。")
        return hashlib.sha256(json.dumps([scope, gateway]).encode()).hexdigest()

    def _decode(self, entry_id: str, encrypted: bytes) -> TotpParameters:
        try:
            payload = self._crypto()(self._key()).decrypt(encrypted[:12], encrypted[12:], entry_id.encode())
            return TotpParameters(**json.loads(payload))
        except AuthenticatorError:
            raise
        except Exception:
            raise AuthenticatorError("Authenticator 密钥无法解密，请恢复正确的主密钥和数据备份。") from None

    def stage(self, scope: str, gateway: str, params: TotpParameters) -> None:
        entry_id = self._identity(scope, gateway)
        with self._transaction() as db:
            previous = db.execute("SELECT * FROM entries WHERE entry_id=?", (entry_id,)).fetchone()
            if previous:
                old = self._decode(entry_id, previous["encrypted"])
                if old != params:
                    raise AuthenticatorError("此 GG 已保存其他 Authenticator 密钥，不能自动覆盖或重新绑定。")
                return
            nonce = secrets.token_bytes(12)
            payload = json.dumps({"secret": params.secret, "algorithm": params.algorithm,
                                  "digits": params.digits, "period": params.period}).encode()
            encrypted = nonce + self._crypto()(self._key()).encrypt(nonce, payload, entry_id.encode())
            db.execute("INSERT INTO entries(entry_id,encrypted,status,updated) VALUES (?,?,'pending',?)",
                       (entry_id, encrypted, time.time()))

    def activate(self, scope: str, gateway: str) -> None:
        entry_id = self._identity(scope, gateway)
        with self._transaction() as db:
            if db.execute("UPDATE entries SET status='active',updated=? WHERE entry_id=?",
                          (time.time(), entry_id)).rowcount != 1:
                raise AuthenticatorError("未找到待确认的绑定密钥，不能标记绑定成功。")

    def status(self, scope: str, gateway: str) -> str:
        if not gateway or not self.db_path.exists():
            return "missing"
        entry_id = self._identity(scope, gateway)
        with self._transaction() as db:
            row = db.execute("SELECT status FROM entries WHERE entry_id=?", (entry_id,)).fetchone()
        return str(row["status"]) if row else "missing"

    def issue(self, scope: str, gateway: str, *, pending: bool = False) -> tuple[str | None, float]:
        entry_id = self._identity(scope, gateway)
        with self._transaction() as db:
            row = db.execute("SELECT * FROM entries WHERE entry_id=?", (entry_id,)).fetchone()
            if not row or (row["status"] != "active" and not pending):
                raise AuthenticatorError("该 GG 没有已确认的托管密钥，请用原 Authenticator 人工输入。")
            params = self._decode(entry_id, row["encrypted"])
            now = time.time()
            counter = int(now // params.period)
            remaining = params.period - now % params.period
            if counter < row["counter"]:
                raise AuthenticatorError("系统时钟发生回退，请校准时间后再使用动态码。")
            if remaining < 5 or counter == row["counter"]:
                return None, remaining + 0.5
            db.execute("UPDATE entries SET counter=? WHERE entry_id=?", (counter, entry_id))
            return params.code(counter), 0
