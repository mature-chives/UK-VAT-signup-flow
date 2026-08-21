from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SALT_BYTES = 16
SESSION_TTL_SECONDS = 8 * 3600
USERNAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")
MIN_PASSWORD_LENGTH = 12
FAILURE_THRESHOLD = 5
BASE_LOCKOUT_SECONDS = 300
MAX_LOCKOUT_SECONDS = 3600
# 用户名不存在时也走一次等价的 scrypt，抹平响应时间差异，
# 否则可以用登录耗时枚举出哪些账号真实存在。
_DUMMY_SALT = bytes(SALT_BYTES)


def normalize_username(value: str) -> str:
    return value.strip().casefold()


def _derive(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
    )


def _write_private(path: Path, text: str) -> None:
    """以 0600 创建并写入，避免 write_text 后再 chmod 的暴露窗口。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        stream = os.fdopen(descriptor, "w", encoding="utf-8")
    except BaseException:
        os.close(descriptor)
        raise
    with stream:
        stream.write(text)
    # 文件已存在时 O_CREAT 的 mode 会被忽略，仍需显式收紧权限。
    path.chmod(0o600)


class UserStore:
    """本地 JSON 账号库。每次读取按 mtime 判断是否需要重新载入。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._cache: dict[str, dict[str, str]] = {}
        self._cached_stamp: tuple[int, int] | None = None

    def _load(self) -> dict[str, dict[str, str]]:
        with self._lock:
            try:
                stat = self.path.stat()
            except OSError:
                self._cache = {}
                self._cached_stamp = None
                return {}
            stamp = (stat.st_mtime_ns, stat.st_size)
            if stamp != self._cached_stamp:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                users = raw.get("users", {})
                self._cache = users if isinstance(users, dict) else {}
                self._cached_stamp = stamp
            return dict(self._cache)

    def _store(self, users: dict[str, dict[str, str]]) -> None:
        payload = json.dumps(
            {"version": 1, "users": users}, ensure_ascii=False, indent=2
        )
        _write_private(self.path, payload + "\n")
        with self._lock:
            self._cache = dict(users)
            self._cached_stamp = None

    def usernames(self) -> list[str]:
        return sorted(self._load())

    def records(self) -> list[dict[str, Any]]:
        """返回不含口令材料的账号列表，供管理界面展示。"""
        users = self._load()
        return [
            {
                "username": name,
                "admin": bool(record.get("admin", False)),
                "created": record.get("created", ""),
            }
            for name, record in sorted(users.items())
        ]

    def is_empty(self) -> bool:
        return not self._load()

    def exists(self, username: str) -> bool:
        return normalize_username(username) in self._load()

    def is_admin(self, username: str) -> bool:
        record = self._load().get(normalize_username(username))
        return bool(record and record.get("admin", False))

    def admin_count(self) -> int:
        return sum(
            1 for record in self._load().values() if record.get("admin", False)
        )

    def add(self, username: str, password: str, *, admin: bool = False) -> None:
        name = normalize_username(username)
        if not USERNAME_PATTERN.match(name):
            raise ValueError(
                "用户名只能使用小写字母、数字、点、下划线和短横线，长度 2-32。"
            )
        if len(password) < MIN_PASSWORD_LENGTH:
            raise ValueError(f"密码至少需要 {MIN_PASSWORD_LENGTH} 个字符。")
        salt = secrets.token_bytes(SALT_BYTES)
        users = self._load()
        existing = users.get(name, {})
        users[name] = {
            "salt": salt.hex(),
            "hash": _derive(password, salt).hex(),
            "created": existing.get("created", datetime.now(UTC).isoformat()),
            # 第一个账号自动成为管理员；重设密码时保留原有角色。
            "admin": bool(
                admin or existing.get("admin", False) or not users
            ),
        }
        self._store(users)

    def remove(self, username: str) -> bool:
        users = self._load()
        if normalize_username(username) not in users:
            return False
        del users[normalize_username(username)]
        self._store(users)
        return True

    def verify(self, username: str, password: str) -> bool:
        name = normalize_username(username)
        record = self._load().get(name)
        if record is None:
            # 走一次等价成本的派生，再返回失败，保持与真实账号相同的耗时。
            _derive(password, _DUMMY_SALT)
            return False
        try:
            salt = bytes.fromhex(record["salt"])
            expected = bytes.fromhex(record["hash"])
        except (KeyError, ValueError):
            _derive(password, _DUMMY_SALT)
            return False
        return hmac.compare_digest(_derive(password, salt), expected)


class SessionSigner:
    """HMAC 签名的会话令牌。密钥每次启动随机生成，重启即全部失效。"""

    def __init__(self, secret: bytes | None = None, ttl: int = SESSION_TTL_SECONDS) -> None:
        self.secret = secret or secrets.token_bytes(32)
        self.ttl = ttl

    def _sign(self, body: str) -> str:
        digest = hmac.new(self.secret, body.encode("ascii"), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

    def issue(self, username: str, *, now: float | None = None) -> str:
        moment = time.time() if now is None else now
        payload = f"{username}|{int(moment + self.ttl)}|{secrets.token_urlsafe(12)}"
        body = (
            base64.urlsafe_b64encode(payload.encode("utf-8"))
            .decode("ascii")
            .rstrip("=")
        )
        return f"{body}.{self._sign(body)}"

    def verify(self, token: str, *, now: float | None = None) -> str | None:
        body, separator, signature = token.partition(".")
        if not separator or not body or not signature:
            return None
        if not hmac.compare_digest(signature, self._sign(body)):
            return None
        try:
            payload = base64.urlsafe_b64decode(
                body + "=" * (-len(body) % 4)
            ).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None
        username, separator, rest = payload.partition("|")
        if not separator:
            return None
        expiry_text, _, _ = rest.partition("|")
        try:
            expiry = int(expiry_text)
        except ValueError:
            return None
        if (time.time() if now is None else now) >= expiry:
            return None
        return username


class LoginThrottle:
    """按 IP 和用户名双维度限速，超过阈值后锁定时间指数退避。"""

    def __init__(
        self,
        threshold: int = FAILURE_THRESHOLD,
        base_lockout: int = BASE_LOCKOUT_SECONDS,
        max_lockout: int = MAX_LOCKOUT_SECONDS,
    ) -> None:
        self.threshold = threshold
        self.base_lockout = base_lockout
        self.max_lockout = max_lockout
        self._lock = threading.Lock()
        self._failures: dict[str, tuple[int, float]] = {}

    def _now(self) -> float:
        return time.monotonic()

    def retry_after(self, *keys: str) -> float:
        """返回还需等待的秒数，0 表示当前允许尝试。"""
        moment = self._now()
        remaining = 0.0
        with self._lock:
            for key in keys:
                entry = self._failures.get(key)
                if entry is None:
                    continue
                _, locked_until = entry
                remaining = max(remaining, locked_until - moment)
        return max(0.0, remaining)

    def record_failure(self, *keys: str) -> None:
        moment = self._now()
        with self._lock:
            for key in keys:
                count, _ = self._failures.get(key, (0, 0.0))
                count += 1
                if count >= self.threshold:
                    exponent = count - self.threshold
                    lockout = min(
                        self.base_lockout * (2**exponent), self.max_lockout
                    )
                    self._failures[key] = (count, moment + lockout)
                else:
                    self._failures[key] = (count, 0.0)

    def reset(self, *keys: str) -> None:
        with self._lock:
            for key in keys:
                self._failures.pop(key, None)


def _prompt_password() -> str:
    password = getpass.getpass("请输入新密码：")
    if password != getpass.getpass("请再次输入确认："):
        raise ValueError("两次输入的密码不一致。")
    return password


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="管理 VAT 自动化 Web UI 的登录账号"
    )
    result.add_argument(
        "--users",
        type=Path,
        default=Path("users.json"),
        help="账号文件路径，默认 users.json",
    )
    commands = result.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add", help="新增账号或重设已有账号的密码")
    add.add_argument("username")
    add.add_argument(
        "--admin", action="store_true", help="授予账号管理权限（首个账号自动获得）"
    )
    remove = commands.add_parser("remove", help="删除账号")
    remove.add_argument("username")
    commands.add_parser("list", help="列出全部账号")
    return result


def main() -> int:
    args = parser().parse_args()
    store = UserStore(args.users)
    try:
        if args.command == "add":
            existed = store.exists(args.username)
            store.add(args.username, _prompt_password(), admin=args.admin)
            action = "已重设密码" if existed else "已创建账号"
            print(f"{action}：{normalize_username(args.username)}")
        elif args.command == "remove":
            if not store.remove(args.username):
                print(f"账号不存在：{normalize_username(args.username)}")
                return 1
            print(f"已删除账号：{normalize_username(args.username)}")
        else:
            records = store.records()
            if not records:
                print(f"{args.users} 中还没有任何账号。")
            for record in records:
                suffix = "（管理员）" if record["admin"] else ""
                print(f"{record['username']}{suffix}")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"操作失败：{exc}")
        return 1
    except KeyboardInterrupt:
        print()
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
