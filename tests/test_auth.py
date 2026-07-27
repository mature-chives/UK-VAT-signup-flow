import tempfile
import time
import unittest
from pathlib import Path

from vat_automation.auth import (
    LoginThrottle,
    SessionSigner,
    UserStore,
    normalize_username,
)


class UserStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self._workspace.cleanup)
        self.store = UserStore(Path(self._workspace.name) / "users.json")

    def test_add_and_verify_password(self) -> None:
        self.store.add("Alice", "correct-horse-battery")
        self.assertTrue(self.store.verify("alice", "correct-horse-battery"))
        self.assertFalse(self.store.verify("alice", "wrong-password-xx"))

    def test_unknown_user_is_rejected_with_comparable_cost(self) -> None:
        self.store.add("alice", "correct-horse-battery")

        def timed(username: str) -> float:
            begin = time.perf_counter()
            self.store.verify(username, "wrong-password-xx")
            return time.perf_counter() - begin

        known = min(timed("alice") for _ in range(3))
        unknown = min(timed("nobody") for _ in range(3))
        # 未知用户也要走一次 scrypt，两者耗时应在同一量级（放宽到 5 倍容忍抖动）。
        self.assertLess(unknown, known * 5)
        self.assertLess(known, unknown * 5)

    def test_users_file_is_private(self) -> None:
        self.store.add("alice", "correct-horse-battery")
        mode = self.store.path.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_username_and_password_rules(self) -> None:
        with self.assertRaisesRegex(ValueError, "用户名"):
            self.store.add("Bad Name!", "correct-horse-battery")
        with self.assertRaisesRegex(ValueError, "密码"):
            self.store.add("alice", "short")

    def test_remove_and_normalization(self) -> None:
        self.store.add("Alice", "correct-horse-battery")
        self.assertEqual(normalize_username(" Alice "), "alice")
        self.assertTrue(self.store.exists("ALICE"))
        self.assertTrue(self.store.remove("alice"))
        self.assertFalse(self.store.remove("alice"))
        self.assertTrue(self.store.is_empty())

    def test_reload_after_external_change(self) -> None:
        self.store.add("alice", "correct-horse-battery")
        other = UserStore(self.store.path)
        other.add("bob", "another-secret-value")
        self.assertEqual(self.store.usernames(), ["alice", "bob"])

    def test_first_account_becomes_admin(self) -> None:
        self.store.add("alice", "correct-horse-battery")
        self.store.add("bob", "another-secret-value")
        self.assertTrue(self.store.is_admin("alice"))
        self.assertFalse(self.store.is_admin("bob"))
        self.assertEqual(self.store.admin_count(), 1)

    def test_password_reset_keeps_admin_role(self) -> None:
        self.store.add("alice", "correct-horse-battery")
        self.store.add("bob", "another-secret-value")
        self.store.add("alice", "new-password-value-1")
        self.assertTrue(self.store.is_admin("alice"))
        self.assertTrue(self.store.verify("alice", "new-password-value-1"))

    def test_records_expose_roles_without_secrets(self) -> None:
        self.store.add("alice", "correct-horse-battery")
        self.store.add("bob", "another-secret-value", admin=True)
        records = self.store.records()
        self.assertEqual(
            [(item["username"], item["admin"]) for item in records],
            [("alice", True), ("bob", True)],
        )
        for item in records:
            self.assertNotIn("hash", item)
            self.assertNotIn("salt", item)


class SessionSignerTests(unittest.TestCase):
    def test_issue_and_verify(self) -> None:
        signer = SessionSigner()
        token = signer.issue("alice")
        self.assertEqual(signer.verify(token), "alice")

    def test_tampered_token_is_rejected(self) -> None:
        signer = SessionSigner()
        token = signer.issue("alice")
        self.assertIsNone(signer.verify(token[:-2] + "xx"))
        self.assertIsNone(signer.verify("garbage"))
        self.assertIsNone(signer.verify(""))

    def test_expired_token_is_rejected(self) -> None:
        signer = SessionSigner(ttl=60)
        token = signer.issue("alice")
        self.assertIsNone(signer.verify(token, now=time.time() + 61))

    def test_restart_invalidates_all_sessions(self) -> None:
        first = SessionSigner()
        second = SessionSigner()
        self.assertIsNone(second.verify(first.issue("alice")))


class LoginThrottleTests(unittest.TestCase):
    def test_lockout_after_threshold(self) -> None:
        throttle = LoginThrottle(threshold=3, base_lockout=10)
        for _ in range(2):
            throttle.record_failure("ip:1.2.3.4")
        self.assertEqual(throttle.retry_after("ip:1.2.3.4"), 0.0)
        throttle.record_failure("ip:1.2.3.4")
        self.assertGreater(throttle.retry_after("ip:1.2.3.4"), 0.0)

    def test_lockout_backs_off_and_resets(self) -> None:
        throttle = LoginThrottle(threshold=1, base_lockout=10, max_lockout=40)
        throttle.record_failure("user:alice")
        first = throttle.retry_after("user:alice")
        throttle.record_failure("user:alice")
        second = throttle.retry_after("user:alice")
        self.assertGreater(second, first)
        throttle.reset("user:alice")
        self.assertEqual(throttle.retry_after("user:alice"), 0.0)

    def test_keys_are_independent(self) -> None:
        throttle = LoginThrottle(threshold=1, base_lockout=10)
        throttle.record_failure("ip:1.2.3.4")
        self.assertEqual(throttle.retry_after("ip:5.6.7.8"), 0.0)
        self.assertGreater(throttle.retry_after("ip:1.2.3.4", "ip:5.6.7.8"), 0.0)


if __name__ == "__main__":
    unittest.main()
