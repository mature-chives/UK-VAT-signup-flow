"""独立邮箱账号池：0600 配置、SQLite 原子分配、仅本机 CLI 管理。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .skymail import MailError, SkyMailClient


# 依据本机 Your_email_confirmation_code.eml 样例核对，不包含实际验证码。
VAT_PERSONAL_EMAIL_RULE = {
    "purpose": "vat_personal_email",
    "sender": "noreply@tax.service.gov.uk",
    "subject_contains": "Your email confirmation code",
    "code_prefix": "Your code is",
    "charset": "uppercase_letters",
    "length": 6,
}


def code_rules_for_purpose(rules: list[dict[str, Any]], purpose: str) -> list[dict[str, Any]]:
    """按页面用途隔离模板；已核对的 VAT 模板内置，现有 GG 配置不变。"""
    if purpose not in {"gg_signup", "vat_personal_email"}:
        raise MailError("不支持此邮箱验证用途。")
    matched = [dict(rule) for rule in rules if rule.get("purpose") == purpose]
    if not matched and purpose == "vat_personal_email":
        return [dict(VAT_PERSONAL_EMAIL_RULE)]
    return matched


def default_pool_dir() -> Path:
    return Path(__file__).resolve().parents[1] / ".mail-pool"


def private_json(path: Path, value: Any) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".mail-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_private(path: Path) -> Any:
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise MailError("邮箱配置必须是本机普通文件且权限为 0600，请先修正权限。")
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (ValueError, UnicodeError):
        raise MailError("邮箱配置 JSON 格式错误。") from None


def code_rule_format(rule: dict[str, Any]) -> tuple[int, str]:
    """兼容旧 digits 模板；新模板明确验证码长度与字符集。"""
    length = rule.get("length", rule.get("digits"))
    charset = rule.get("charset", "digits")
    if (type(length) is not int or not 4 <= length <= 10
            or charset not in {"digits", "uppercase_letters"}
            or ("digits" in rule and (rule["digits"] != length or charset != "digits"))):
        raise MailError("验证码模板需指定 4–10 位长度，以及 digits 或 uppercase_letters 字符集。")
    return length, "[0-9]" if charset == "digits" else "[A-Z]"


def validate_config(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise MailError("邮箱配置必须是 JSON 对象。")
    url = urlparse(str(data.get("base_url", "")))
    if (url.scheme != "https" or not url.hostname or url.username or url.password
            or url.query or url.fragment or url.path not in {"", "/"}):
        raise MailError("邮箱服务地址必须是没有路径和凭据的 HTTPS 根地址。")
    accounts = data.get("accounts")
    if not isinstance(accounts, list) or not 1 <= len(accounts) <= 500:
        raise MailError("请配置 1 至 500 个邮箱账号。")
    seen: set[str] = set()
    for account in accounts:
        if not isinstance(account, dict):
            raise MailError("每项邮箱配置必须包含 email 和 password。")
        email, password = account.get("email"), account.get("password")
        if (not isinstance(email, str) or email != email.strip() or email.count("@") != 1
                or any(c.isspace() for c in email) or not all(email.split("@"))
                or not isinstance(password, str) or not password):
            raise MailError("邮箱地址或密码缺失/无效，请在本地检查导入文件。")
        key = email.casefold()
        if key in seen:
            raise MailError("导入清单有重复邮箱，请先去重。")
        seen.add(key)
    rules = data.get("code_rules", [])
    if not isinstance(rules, list):
        raise MailError("code_rules 必须是数组。")
    for rule in rules:
        if (not isinstance(rule, dict) or rule.get("purpose") not in ("gg_signup", "vat_personal_email")
                or any(not isinstance(rule.get(k), str) or not rule[k].strip()
                       or len(rule[k]) > 200 for k in ("sender", "subject_contains", "code_prefix"))):
            raise MailError("验证码模板必须明确用途、发件地址、主题和代码前缀。")
        code_rule_format(rule)
    return data


class MailPool:
    def __init__(self, directory: Path | None = None) -> None:
        self.directory = (directory or default_pool_dir()).absolute()
        self.config_path = self.directory / "config.json"
        self.db_path = self.directory / "pool.sqlite3"

    def config(self) -> dict[str, Any]:
        try:
            return validate_config(read_private(self.config_path))
        except OSError:
            raise MailError("邮箱池未配置或配置文件不可读取。") from None

    def _prepare(self) -> None:
        if self.directory.is_symlink():
            raise MailError("邮箱私有目录不能是符号链接。")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.directory.stat().st_mode & 0o077:
            raise MailError("邮箱私有目录权限必须为 0700。")
        if not self.db_path.exists():
            try:
                os.close(os.open(self.db_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
            except FileExistsError:
                pass
        if self.db_path.is_symlink() or self.db_path.stat().st_mode & 0o077:
            raise MailError("邮箱调度数据库权限必须为 0600，且不能是符号链接。")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self._prepare()
        db = sqlite3.connect(self.db_path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS accounts (
                    email TEXT PRIMARY KEY, position INTEGER NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1, checked INTEGER NOT NULL DEFAULT 0,
                    account_id INTEGER, user_id INTEGER, lease TEXT NOT NULL DEFAULT '',
                    cooldown REAL NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS bindings (binding TEXT PRIMARY KEY, email TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS attempted (
                    email TEXT NOT NULL, email_id INTEGER NOT NULL,
                    PRIMARY KEY(email, email_id));
            """)
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def initialize(self) -> None:
        with self.transaction():
            if self.config_path.exists() or (self.directory / "accounts.json").exists():
                raise MailError("邮箱池配置或导入模板已存在，不会覆盖。")
            private_json(self.directory / "accounts.json", {
                "base_url": "https://neo.dpdns.org", "code_rules": [],
                "accounts": [{"email": "", "password": ""} for _ in range(50)],
            })

    def import_accounts(self, source: Path) -> int:
        data = validate_config(read_private(source))
        with self.transaction() as db:
            if db.execute("SELECT 1 FROM accounts WHERE lease != ''").fetchone():
                raise MailError("仍有邮箱被任务占用，不能导入或修改配置。")
            # 先停用，跨文件写入失败也不能让运行端使用半更新的配置。
            data["enabled"] = False
            data["revision"] = uuid.uuid4().hex
            private_json(self.config_path, data)
            db.execute("UPDATE accounts SET enabled=0, checked=0")
            for index, account in enumerate(data["accounts"]):
                db.execute("""INSERT INTO accounts(email,position) VALUES (?,?)
                    ON CONFLICT(email) DO UPDATE SET position=excluded.position, enabled=1, checked=0""",
                           (account["email"].casefold(), index))
            db.execute("INSERT OR REPLACE INTO meta VALUES ('revision',?)", (data["revision"],))
        return len(data["accounts"])

    @staticmethod
    def _check_revision(db: sqlite3.Connection, config: dict[str, Any]) -> None:
        row = db.execute("SELECT value FROM meta WHERE key='revision'").fetchone()
        if not row or row["value"] != config.get("revision"):
            raise MailError("邮箱配置与调度版本不一致，请重新导入并检查；不会使用半更新配置。")

    def status(self) -> dict[str, Any]:
        if not self.config_path.exists():
            return {"configured": False, "enabled": False, "ready": 0, "total": 0, "auto_code": False}
        config = self.config()
        with self.transaction() as db:
            self._check_revision(db, config)
            rows = db.execute("SELECT * FROM accounts WHERE enabled=1 ORDER BY position").fetchall()
        return {
            "configured": True, "enabled": config.get("enabled") is True,
            "total": len(rows), "ready": sum(bool(row["checked"]) for row in rows),
            "busy": sum(bool(row["lease"]) for row in rows),
            "busy_indices": [row["position"] + 1 for row in rows if row["lease"]],
            "auto_code": bool(code_rules_for_purpose(config.get("code_rules", []), "gg_signup")),
            "vat_auto_code": bool(code_rules_for_purpose(config.get("code_rules", []), "vat_personal_email")),
        }

    async def check(self, index: int) -> None:
        config = self.config()
        if not 1 <= index <= len(config["accounts"]):
            raise MailError("邮箱序号超出导入清单范围。")
        account = config["accounts"][index - 1]
        with self.transaction() as db:
            self._check_revision(db, config)
            if db.execute("SELECT 1 FROM accounts WHERE lease != ''").fetchone():
                raise MailError("仍有活动邮箱任务，请结束后再做连接检查。")
            db.execute("UPDATE accounts SET checked=0 WHERE email=?", (account["email"].casefold(),))
        async with SkyMailClient(config["base_url"], account["email"], account["password"]) as client:
            account_id, user_id = await client.identity()
            await client.messages(account_id, size=1, full=False)
        with self.transaction() as db:
            # 防止检查期间导入的新密码被误标记为已验证。
            if self.config() != config:
                raise MailError("检查期间配置发生变化，请重试。")
            db.execute("UPDATE accounts SET checked=1,account_id=?,user_id=? WHERE email=?",
                       (account_id, user_id, account["email"].casefold()))

    def enable(self, enabled: bool) -> None:
        with self.transaction() as db:
            config = self.config()
            self._check_revision(db, config)
            if enabled and db.execute("SELECT 1 FROM accounts WHERE enabled=1 AND checked=0").fetchone():
                raise MailError("请先完成全部邮箱的只读连接检查。")
            config["enabled"] = enabled
            private_json(self.config_path, config)

    def acquire(self, binding: str, lease: str, preferred_email: str = "") -> dict[str, Any]:
        with self.transaction() as db:
            config = self.config()
            self._check_revision(db, config)
            if config.get("enabled") is not True:
                raise MailError("邮箱池未启用，请先由管理员完成导入和检查。")
            previous = db.execute("SELECT email FROM bindings WHERE binding=?", (binding,)).fetchone()
            if previous and preferred_email and previous["email"] != preferred_email.casefold():
                raise MailError("当前保存邮箱与原申请绑定不一致，请先人工核对。")
            if previous or preferred_email:
                email = previous["email"] if previous else preferred_email.casefold()
                row = db.execute("SELECT * FROM accounts WHERE email=? AND enabled=1 AND checked=1",
                                 (email,)).fetchone()
                if not row:
                    raise MailError("原申请邮箱已停用，请检查配置；不会自动更换开户邮箱。")
                if row["lease"]:
                    raise MailError("原申请邮箱仍被占用；异常退出后须管理员核实并解除占用。")
                if row["cooldown"] > time.time():
                    raise MailError("原申请邮箱正在冷却，请稍后继续；不会更换邮箱。")
                if not previous:
                    db.execute("INSERT INTO bindings VALUES (?,?)", (binding, row["email"]))
            else:
                cursor = db.execute("SELECT value FROM meta WHERE key='cursor'").fetchone()
                position = int(cursor["value"]) if cursor else -1
                row = db.execute("""SELECT * FROM accounts WHERE enabled=1 AND checked=1
                    AND lease='' AND cooldown<=? ORDER BY (position<=?),position LIMIT 1""",
                                 (time.time(), position)).fetchone()
                if row is None:
                    raise MailError("邮箱池暂无可用邮箱，请等待当前任务结束或冷却完成后重试。")
                db.execute("INSERT INTO bindings VALUES (?,?)", (binding, row["email"]))
                db.execute("INSERT OR REPLACE INTO meta VALUES ('cursor',?)", (str(row["position"]),))
            account = next((item for item in config["accounts"] if item["email"].casefold() == row["email"]), None)
            if account is None:
                raise MailError("邮箱配置与调度记录不一致，请重新导入。")
            db.execute("UPDATE accounts SET lease=? WHERE email=?", (lease, row["email"]))
            return {**dict(row), **account, "base_url": config["base_url"],
                    "code_rules": config.get("code_rules", []), "lease": lease}

    def release(self, lease: str) -> None:
        with self.transaction() as db:
            db.execute("UPDATE accounts SET lease='',cooldown=? WHERE lease=?", (time.time() + 300, lease))

    def release_index(self, index: int) -> None:
        with self.transaction() as db:
            row = db.execute("SELECT lease FROM accounts WHERE enabled=1 AND position=?", (index - 1,)).fetchone()
            if not row:
                raise MailError("邮箱序号不存在。")
            db.execute("UPDATE accounts SET lease='',cooldown=? WHERE enabled=1 AND position=?",
                       (time.time() + 300, index - 1))

    def mark_attempted(self, email: str, email_id: int) -> bool:
        with self.transaction() as db:
            return db.execute("INSERT OR IGNORE INTO attempted VALUES (?,?)",
                              (email.casefold(), email_id)).rowcount == 1


def binding_key(*parts: str) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description="本机邮箱池管理（不创建、发送或删除邮件）")
    parser.add_argument("--directory", type=Path, default=default_pool_dir())
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    importing = commands.add_parser("import")
    importing.add_argument("source", type=Path)
    checking = commands.add_parser("check")
    checking.add_argument("--index", type=int, default=1)
    checking.add_argument("--all", action="store_true")
    for name in ("status", "enable", "disable"):
        commands.add_parser(name)
    release = commands.add_parser("release")
    release.add_argument("--index", type=int, required=True)
    release.add_argument("--confirm-stopped", action="store_true", required=True)
    args = parser.parse_args()
    pool = MailPool(args.directory)
    try:
        if args.command == "init":
            pool.initialize()
            print(f"已创建 0600 私有导入模板：{pool.directory / 'accounts.json'}（50 项，尚未启用）")
        elif args.command == "import":
            print(f"已导入 {pool.import_accounts(args.source)} 个账号；检查通过后需显式 enable。")
        elif args.command == "check":
            async def checks() -> None:
                indices = range(1, len(pool.config()["accounts"]) + 1) if args.all else [args.index]
                for index in indices:
                    await pool.check(index)
                    print(f"邮箱 #{index}：登录身份与只读收件检查通过。")
                    if args.all:
                        await asyncio.sleep(1)
            asyncio.run(checks())
        elif args.command in {"enable", "disable"}:
            pool.enable(args.command == "enable")
            print("邮箱池设置已更新。")
        elif args.command == "release":
            pool.release_index(args.index)
            print("已解除占用；保留申请绑定，冷却 5 分钟后可用。")
        else:
            print(json.dumps(pool.status(), ensure_ascii=False))
    except (MailError, OSError, sqlite3.Error) as error:
        # 不输出第三方异常和配置原文；MailError 的固定文案可以安全显示。
        print(str(error) if isinstance(error, MailError) else "邮箱私有文件或调度数据库操作失败。", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
