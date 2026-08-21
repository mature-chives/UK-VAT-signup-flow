<<<<<<< Updated upstream
import asyncio
import io
import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException, Response, UploadFile
from pydantic import ValidationError
from starlette.requests import Request

from vat_automation.auth import SessionSigner, UserStore
from vat_automation.config import PageRule, Settings
from vat_automation.web import (
    CSRF_COOKIE,
    CSRF_HEADER,
    SESSION_COOKIE,
    ContinueRequest,
    FinalEditRequest,
    FinalSubmitRequest,
    JobManager,
    LoginRequest,
    RemoteEditSubmitRequest,
    SetupRequest,
    StartRequest,
    UserCreateRequest,
    UserNameRequest,
    context,
    create_user,
    current_admin,
    current_user,
    delete_user,
    final_review_edit,
    final_review_edit_submit,
    final_review_pdf,
    final_review_confirm,
    index,
    list_users,
    login,
    parse_document,
    require_csrf,
    setup,
    status,
)


def make_request(
    method: str = "GET",
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
    client_ip: str = "192.168.1.50",
) -> Request:
    raw = [(key.lower().encode(), value.encode()) for key, value in (headers or {}).items()]
    if cookies:
        joined = "; ".join(f"{key}={value}" for key, value in cookies.items())
        raw.append((b"cookie", joined.encode()))
    return Request(
        {
            "type": "http",
            "method": method,
            "headers": raw,
            "path": "/",
            "query_string": b"",
            "client": (client_ip, 40000),
        }
    )


class WebAuthContext(unittest.TestCase):
    """每个用例使用干净的账号库、签名器和任务管理器。"""

    def setUp(self) -> None:
        self._workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self._workspace.cleanup)
        base = Path(self._workspace.name)
        self._saved = (
            context.users, context.signer, context.throttle,
            context.manager, context.config_path,
        )
        context.users = UserStore(base / "users.json")
        context.signer = SessionSigner()
        context.throttle = type(context.throttle)()
        context.config_path = base / "vat-config.json"
        context.config_path.write_text("{}", encoding="utf-8")
        context.manager = JobManager(context.config_path)
        self.addCleanup(self._restore)
        context.users.add("alice", "correct-horse-battery")
        context.users.add("bob", "another-secret-value")

    def _restore(self) -> None:
        (
            context.users, context.signer, context.throttle,
            context.manager, context.config_path,
        ) = self._saved

    def login_cookies(self, username: str = "alice") -> dict[str, str]:
        return {
            SESSION_COOKIE: context.signer.issue(username),
            CSRF_COOKIE: "csrf-token-value",
        }


class AuthenticationTests(WebAuthContext):
    def test_unauthenticated_request_is_rejected(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(current_user(make_request()))
        self.assertEqual(caught.exception.status_code, 401)

    def test_valid_session_cookie_is_accepted(self) -> None:
        request = make_request(cookies=self.login_cookies())
        self.assertEqual(asyncio.run(current_user(request)), "alice")

    def test_deleted_user_session_is_invalidated(self) -> None:
        cookies = self.login_cookies()
        context.users.remove("alice")
        with self.assertRaises(HTTPException):
            asyncio.run(current_user(make_request(cookies=cookies)))

    def test_index_redirects_to_login_without_session(self) -> None:
        response = asyncio.run(index(make_request()))
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/login")

    def test_login_rejects_wrong_password_and_throttles(self) -> None:
        request = make_request(
            method="POST", headers={"origin": "https://vat.local", "host": "vat.local"}
        )
        payload = LoginRequest(username="alice", password="wrong-password-xx")
        for _ in range(5):
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(login(request, payload, Response(), None))
            self.assertEqual(caught.exception.status_code, 401)
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(login(request, payload, Response(), None))
        self.assertEqual(caught.exception.status_code, 429)

    def test_login_success_sets_cookies(self) -> None:
        request = make_request(
            method="POST", headers={"origin": "https://vat.local", "host": "vat.local"}
        )
        response = Response()
        result = asyncio.run(
            login(
                request,
                LoginRequest(username="Alice", password="correct-horse-battery"),
                response,
                None,
            )
        )
        self.assertEqual(result["user"], "alice")
        cookie_header = ",".join(
            value.decode() for key, value in response.raw_headers if key == b"set-cookie"
        )
        self.assertIn(SESSION_COOKIE, cookie_header)
        self.assertIn(CSRF_COOKIE, cookie_header)
        self.assertIn("SameSite=strict", cookie_header)
        self.assertIn("HttpOnly", cookie_header)


class CsrfTests(WebAuthContext):
    def test_post_without_origin_is_rejected(self) -> None:
        request = make_request(method="POST", cookies=self.login_cookies())
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(require_csrf(request))
        self.assertEqual(caught.exception.status_code, 403)

    def test_post_with_mismatched_origin_is_rejected(self) -> None:
        request = make_request(
            method="POST",
            headers={"origin": "https://evil.example", "host": "vat.local"},
            cookies=self.login_cookies(),
        )
        with self.assertRaises(HTTPException):
            asyncio.run(require_csrf(request))

    def test_post_without_matching_token_is_rejected(self) -> None:
        request = make_request(
            method="POST",
            headers={
                "origin": "https://vat.local",
                "host": "vat.local",
                CSRF_HEADER: "different-token",
            },
            cookies=self.login_cookies(),
        )
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(require_csrf(request))
        self.assertEqual(caught.exception.status_code, 403)

    def test_post_with_matching_token_passes(self) -> None:
        request = make_request(
            method="POST",
            headers={
                "origin": "https://vat.local",
                "host": "vat.local",
                CSRF_HEADER: "csrf-token-value",
            },
            cookies=self.login_cookies(),
        )
        asyncio.run(require_csrf(request))


class StartRequestTests(unittest.TestCase):
    def test_client_supplied_config_path_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            StartRequest(config_path="/etc/passwd", extracted_confirmed=True)


class SetupTests(WebAuthContext):
    def _empty_store(self) -> None:
        for name in list(context.users.usernames()):
            context.users.remove(name)
        context.setup_token = None

    def _origin_request(self) -> Request:
        return make_request(
            method="POST", headers={"origin": "https://vat.local", "host": "vat.local"}
        )

    def test_setup_creates_admin_with_valid_token(self) -> None:
        self._empty_store()
        token = context.ensure_setup_token()
        result = asyncio.run(
            setup(
                self._origin_request(),
                SetupRequest(
                    token=token, username="Boss", password="long-enough-password"
                ),
                Response(),
                None,
            )
        )
        self.assertEqual(result["user"], "boss")
        self.assertTrue(context.users.is_admin("boss"))

    def test_setup_rejects_wrong_token(self) -> None:
        self._empty_store()
        context.ensure_setup_token()
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(
                setup(
                    self._origin_request(),
                    SetupRequest(
                        token="wrong", username="boss", password="long-enough-password"
                    ),
                    Response(),
                    None,
                )
            )
        self.assertEqual(caught.exception.status_code, 403)
        self.assertTrue(context.users.is_empty())

    def test_setup_rejected_once_users_exist(self) -> None:
        context.setup_token = "leftover-token"
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(
                setup(
                    self._origin_request(),
                    SetupRequest(
                        token="leftover-token",
                        username="mallory",
                        password="long-enough-password",
                    ),
                    Response(),
                    None,
                )
            )
        self.assertEqual(caught.exception.status_code, 409)

    def test_index_redirects_to_setup_when_no_users(self) -> None:
        self._empty_store()
        response = asyncio.run(index(make_request()))
        self.assertEqual(response.headers["location"], "/setup")


class AdminTests(WebAuthContext):
    def test_first_user_is_admin_second_is_not(self) -> None:
        self.assertTrue(context.users.is_admin("alice"))
        self.assertFalse(context.users.is_admin("bob"))

    def test_status_reports_admin_flag(self) -> None:
        self.assertTrue(asyncio.run(status(username="alice"))["admin"])
        self.assertFalse(asyncio.run(status(username="bob"))["admin"])

    def test_non_admin_cannot_manage_users(self) -> None:
        request = make_request(cookies=self.login_cookies("bob"))
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(current_admin(request))
        self.assertEqual(caught.exception.status_code, 403)

    def test_admin_dependency_accepts_admin(self) -> None:
        request = make_request(cookies=self.login_cookies("alice"))
        self.assertEqual(asyncio.run(current_admin(request)), "alice")

    def test_admin_creates_resets_and_deletes_user(self) -> None:
        created = asyncio.run(
            create_user(
                UserCreateRequest(username="carol", password="carol-long-password"),
                admin="alice",
                _=None,
            )
        )
        self.assertEqual(created["action"], "created")
        self.assertIn(
            ("carol", False),
            [
                (item["username"], item["admin"])
                for item in asyncio.run(list_users(_admin="alice"))["users"]
            ],
        )
        reset = asyncio.run(
            create_user(
                UserCreateRequest(username="carol", password="carol-new-password-1"),
                admin="alice",
                _=None,
            )
        )
        self.assertEqual(reset["action"], "reset")
        self.assertTrue(context.users.verify("carol", "carol-new-password-1"))
        asyncio.run(
            delete_user(UserNameRequest(username="carol"), admin="alice", _=None)
        )
        self.assertFalse(context.users.exists("carol"))

    def test_last_admin_cannot_be_deleted(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(
                delete_user(UserNameRequest(username="alice"), admin="alice", _=None)
            )
        self.assertEqual(caught.exception.status_code, 400)
        self.assertTrue(context.users.exists("alice"))

    def test_deleting_missing_user_returns_404(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(
                delete_user(UserNameRequest(username="ghost"), admin="alice", _=None)
            )
        self.assertEqual(caught.exception.status_code, 404)


class JobManagerTests(WebAuthContext):
    def test_identity_documents_are_isolated_per_user(self) -> None:
        manager = context.jobs()
        manager.store_identity_documents(
            "alice", [("one.pdf", b"one"), ("two.png", b"two"), ("three.txt", b"three")]
        )
        self.assertEqual(manager.snapshot("alice")["identity_documents"], 3)
        self.assertEqual(manager.snapshot("bob")["identity_documents"], 0)
        alice_dir = manager.session("alice").upload_dir
        bob_dir = manager.session("bob").upload_dir
        self.assertNotEqual(alice_dir, bob_dir)
        self.assertEqual(alice_dir.stat().st_mode & 0o777, 0o700)

    def test_identity_files_are_private_and_provided_in_order(self) -> None:
        manager = context.jobs()
        manager.store_identity_documents(
            "alice", [("one.pdf", b"one"), ("two.png", b"two"), ("three.txt", b"three")]
        )
        session = manager.session("alice")
        session.active_identity_documents = list(session.identity_documents)
        paths = [
            Path(asyncio.run(manager._identity_file(session, "document")))
            for _ in range(3)
        ]
        self.assertEqual(
            [path.read_bytes() for path in paths], [b"one", b"two", b"three"]
        )
        self.assertTrue(all(path.stat().st_mode & 0o777 == 0o600 for path in paths))
        self.assertFalse(asyncio.run(manager._identity_files_remaining(session)))

    def test_invalid_second_file_cleans_partial_write(self) -> None:
        manager = context.jobs()
        session = manager.session("alice")
        before = set(session.upload_dir.iterdir())
        with self.assertRaisesRegex(ValueError, "不支持"):
            manager.store_identity_documents(
                "alice",
                [("one.pdf", b"one"), ("bad.exe", b"bad"), ("three.txt", b"three")],
            )
        self.assertEqual(set(session.upload_dir.iterdir()), before)

    def test_start_rejects_unconfirmed_extraction(self) -> None:
        with self.assertRaisesRegex(ValueError, "检查并确认"):
            context.jobs().start("alice", StartRequest(extracted_confirmed=False))

    def test_start_reports_other_users_running_task(self) -> None:
        manager = context.jobs()
        manager.store_identity_documents(
            "bob", [("one.pdf", b"one"), ("two.png", b"two"), ("three.txt", b"three")]
        )
        with manager._lock:
            manager._busy_with = "alice"
        with self.assertRaisesRegex(RuntimeError, "alice"):
            manager.start("bob", StartRequest(extracted_confirmed=True))
        self.assertEqual(manager.snapshot("bob")["busy_with"], "alice")
        self.assertEqual(manager.snapshot("alice")["busy_with"], "")

    def test_pause_checkpoint_keeps_job_alive_and_returns_updated_values(self) -> None:
        manager = context.jobs()
        session = manager.session("alice")
        with manager._lock:
            session.state["status"] = "running"
        manager.pause("alice")

        async def scenario() -> dict[str, str]:
            task = asyncio.create_task(
                manager._pause_checkpoint(
                    session,
                    "https://example.test/register-for-vat/telephone-number",
                    "What is your telephone number?",
                )
            )
            try:
                for _ in range(50):
                    await asyncio.sleep(0.005)
                    if manager.snapshot("alice")["status"] == "paused":
                        break
                self.assertEqual(manager.snapshot("alice")["status"], "paused")
                manager.continue_after_pause(
                    "alice",
                    ContinueRequest(
                        extracted_values={"phone": "8613592850576"},
                        extracted_confirmed=True,
                    ),
                )
                return await task
            finally:
                # 断言失败时也要放行等待线程，否则 asyncio.run 退出时
                # 会 join 线程池，把一次失败放大成 30 分钟挂起。
                session.resume_event.set()
                task.cancel()

        updates = asyncio.run(scenario())
        self.assertEqual(updates, {"phone": "8613592850576"})
        self.assertEqual(manager.snapshot("alice")["status"], "running")

    def test_final_review_waits_for_confirmation_and_exposes_documents(self) -> None:
        manager = context.jobs()
        session = manager.session("alice")
        artifact = Path(self._workspace.name) / "final.pdf"
        artifact.write_bytes(b"%PDF-1.4 fake")

        async def scenario() -> None:
            task = asyncio.create_task(
                manager._final_review(
                    session,
                    {
                        "url": "https://example.test/check-your-answers",
                        "heading": "Check your answers",
                        "pdf": str(artifact),
                        "screenshot": "",
                    },
                )
            )
            try:
                for _ in range(50):
                    await asyncio.sleep(0.005)
                    if manager.snapshot("alice")["status"] == "reviewing":
                        break
                state = manager.snapshot("alice")
                self.assertEqual(state["status"], "reviewing")
                self.assertTrue(state["final_review"]["available"])
                self.assertTrue(state["final_review"]["pdf"])
                manager.confirm_final_review("alice")
                self.assertEqual(await task, {"action": "submit", "target": ""})
            finally:
                # 同 pause 测试：失败时也放行等待线程，避免挂起整套测试。
                session.review_event.set()
                task.cancel()

        asyncio.run(scenario())
        self.assertEqual(manager.final_review_document("alice", "pdf"), artifact)
        # 其他用户拿不到不属于自己的最终复核文件。
        self.assertIsNone(manager.final_review_document("bob", "pdf"))

    def test_final_review_edit_selects_remote_change_target(self) -> None:
        manager = context.jobs()
        session = manager.session("alice")

        async def scenario() -> None:
            task = asyncio.create_task(
                manager._final_review(
                    session,
                    {
                        "url": "https://example.test/check-your-answers",
                        "heading": "Check your answers",
                        "pdf": "",
                        "screenshot": "",
                        "editable": True,
                        "changes": [
                            {"id": "change-4", "label": "Business email address"}
                        ],
                    },
                )
            )
            try:
                for _ in range(50):
                    await asyncio.sleep(0.005)
                    if manager.snapshot("alice")["status"] == "reviewing":
                        break
                manager.request_final_review_edit("alice", "change-4")
                self.assertEqual(
                    await task, {"action": "edit", "target": "change-4"}
                )
                state = manager.snapshot("alice")
                self.assertEqual(state["status"], "editing")
                self.assertTrue(state["final_review"]["editable"])
            finally:
                session.review_event.set()
                task.cancel()

        asyncio.run(scenario())

    def test_remote_edit_form_waits_for_web_answers(self) -> None:
        manager = context.jobs()
        session = manager.session("alice")

        async def scenario() -> None:
            task = asyncio.create_task(
                manager._remote_edit(
                    session,
                    {
                        "url": "https://example.test/business-email",
                        "heading": "What is the business email address?",
                        "fields": [
                            {
                                "key": "businessEmailAddress",
                                "kind": "email",
                                "label": "Email address",
                                "value": "old@example.test",
                            }
                        ],
                        "actions": ["Save and continue"],
                        "errors": [],
                    },
                )
            )
            try:
                for _ in range(50):
                    await asyncio.sleep(0.005)
                    edit = manager.snapshot("alice")["final_review"]["edit"]
                    if edit.get("available"):
                        break
                manager.submit_remote_edit(
                    "alice",
                    RemoteEditSubmitRequest(
                        answers={"businessEmailAddress": "new@example.test"},
                        action="Save and continue",
                    ),
                )
                self.assertEqual(
                    await task,
                    {
                        "answers": {"businessEmailAddress": "new@example.test"},
                        "action": "Save and continue",
                    },
                )
            finally:
                session.edit_event.set()
                task.cancel()

        asyncio.run(scenario())

    def test_final_review_endpoint_rejects_user_without_document(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(final_review_pdf(username="bob"))
        self.assertEqual(caught.exception.status_code, 404)

    def test_final_submit_requires_explicit_confirmation(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(
                final_review_confirm(
                    FinalSubmitRequest(confirmed=False), username="alice", _=None
                )
            )
        self.assertEqual(caught.exception.status_code, 400)

    def test_final_review_edit_endpoint_requires_reviewing_state(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(
                final_review_edit(
                    FinalEditRequest(target="change-0"), username="alice", _=None
                )
            )
        self.assertEqual(caught.exception.status_code, 409)

    def test_remote_edit_skip_action_does_not_require_fields(self) -> None:
        manager = context.jobs()
        session = manager.session("alice")
        with manager._lock:
            session.state["status"] = "editing"
            session.final_review_edit = {
                "available": True,
                "fields": [
                    {
                        "key": "utr",
                        "kind": "text",
                        "label": "What is your Corporation Tax UTR?",
                        "required": True,
                    }
                ],
                "actions": [
                    "I do not have the company's UTR number",
                    "Continue",
                ],
            }
        manager.submit_remote_edit(
            "alice",
            RemoteEditSubmitRequest(
                answers={"utr": ""},
                action="I do not have the company's UTR number",
            ),
        )
        self.assertEqual(
            session.pending_edit_action,
            "I do not have the company's UTR number",
        )

    def test_remote_edit_submit_endpoint_requires_editing_state(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(
                final_review_edit_submit(
                    RemoteEditSubmitRequest(
                        answers={}, action="Save and continue"
                    ),
                    username="alice",
                    _=None,
                )
            )
        self.assertEqual(caught.exception.status_code, 409)

    def test_identity_upload_page_continues_when_files_present(self) -> None:
        manager = context.jobs()
        session = manager.session("alice")
        session.active_identity_documents = [
            Path(self._workspace.name) / "a.pdf",
            Path(self._workspace.name) / "b.pdf",
            Path(self._workspace.name) / "c.pdf",
        ]
        settings = Settings(
            start_url="https://example.test",
            profile_dir=Path(self._workspace.name) / "profile",
            artifacts_dir=Path(self._workspace.name) / "artifacts",
            answers={},
            pages=[
                PageRule(
                    path_contains="/file-upload/upload-document",
                    action="stop",
                )
            ],
        )
        manager._enable_identity_uploads(session, settings)
        self.assertEqual(
            settings.action_for(
                "https://example.test/register-for-vat/file-upload/upload-document",
                "Upload a document",
            ),
            "continue",
        )


class EndpointTests(WebAuthContext):
    def test_status_returns_only_own_state(self) -> None:
        manager = context.jobs()
        # session() 内部会拿 manager._lock（非重入锁），必须在持锁前取出。
        session = manager.session("alice")
        with manager._lock:
            session.state["heading"] = "Alice page"
        self.assertEqual(asyncio.run(status(username="alice"))["heading"], "Alice page")
        self.assertEqual(asyncio.run(status(username="bob"))["heading"], "")

    def test_parse_document_endpoint(self) -> None:
        result = asyncio.run(
            parse_document(
                UploadFile(
                    filename="vat.txt",
                    file=io.BytesIO(b"Business name: Endpoint Ltd"),
                ),
                username="alice",
                _=None,
            )
        )
        self.assertEqual(result["values"]["business_name"], "Endpoint Ltd")

    def test_home_page_contains_review_and_login_flow(self) -> None:
        static_dir = Path(__file__).resolve().parents[1] / "vat_automation" / "static"
        html = (static_dir / "index.html").read_text(encoding="utf-8")
        self.assertIn("最终核对", html)
        self.assertIn("/api/final-review/pdf", html)
        self.assertIn("确认并提交到 HMRC", html)
        self.assertIn("review-submit-consent", html)
        self.assertIn("远程修改所选信息", html)
        self.assertIn("/api/final-review/edit", html)
        self.assertIn("/api/final-review/edit/submit", html)
        self.assertIn("{confirmed:true}", html)
        self.assertIn("下载完整 PDF 存档", html)
        self.assertNotIn("review-frame", html)
        self.assertNotIn("review-print", html)
        self.assertIn("X-CSRF-Token", html)
        self.assertIn("配置文件（由服务端启动参数固定）", html)
        self.assertNotIn("config_path", html)
        self.assertIn("账号管理", html)
        self.assertIn("/api/users", html)
        login_html = (static_dir / "login.html").read_text(encoding="utf-8")
        self.assertIn("/api/login", login_html)
        setup_html = (static_dir / "setup.html").read_text(encoding="utf-8")
        self.assertIn("/api/setup", setup_html)
        self.assertIn("初始化码", setup_html)
        self.assertIn("至少 12 个字符", setup_html)


if __name__ == "__main__":
    unittest.main()
=======
import asyncio
import io
import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException, Response, UploadFile
from pydantic import ValidationError
from starlette.requests import Request

from vat_automation.auth import SessionSigner, UserStore
from vat_automation.config import PageRule, Settings
from vat_automation.web import (
    CSRF_COOKIE,
    CSRF_HEADER,
    SESSION_COOKIE,
    ContinueRequest,
    FinalSubmitRequest,
    JobManager,
    LoginRequest,
    SetupRequest,
    StartRequest,
    UserCreateRequest,
    UserNameRequest,
    context,
    create_user,
    current_admin,
    current_user,
    delete_user,
    final_review_pdf,
    final_review_confirm,
    index,
    list_users,
    login,
    parse_document,
    require_csrf,
    setup,
    status,
)


def make_request(
    method: str = "GET",
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
    client_ip: str = "192.168.1.50",
) -> Request:
    raw = [(key.lower().encode(), value.encode()) for key, value in (headers or {}).items()]
    if cookies:
        joined = "; ".join(f"{key}={value}" for key, value in cookies.items())
        raw.append((b"cookie", joined.encode()))
    return Request(
        {
            "type": "http",
            "method": method,
            "headers": raw,
            "path": "/",
            "query_string": b"",
            "client": (client_ip, 40000),
        }
    )


class WebAuthContext(unittest.TestCase):
    """每个用例使用干净的账号库、签名器和任务管理器。"""

    def setUp(self) -> None:
        self._workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self._workspace.cleanup)
        base = Path(self._workspace.name)
        self._saved = (
            context.users, context.signer, context.throttle,
            context.manager, context.config_path,
        )
        context.users = UserStore(base / "users.json")
        context.signer = SessionSigner()
        context.throttle = type(context.throttle)()
        context.config_path = base / "vat-config.json"
        context.config_path.write_text("{}", encoding="utf-8")
        context.manager = JobManager(context.config_path)
        self.addCleanup(self._restore)
        context.users.add("alice", "correct-horse-battery")
        context.users.add("bob", "another-secret-value")

    def _restore(self) -> None:
        (
            context.users, context.signer, context.throttle,
            context.manager, context.config_path,
        ) = self._saved

    def login_cookies(self, username: str = "alice") -> dict[str, str]:
        return {
            SESSION_COOKIE: context.signer.issue(username),
            CSRF_COOKIE: "csrf-token-value",
        }


class AuthenticationTests(WebAuthContext):
    def test_unauthenticated_request_is_rejected(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(current_user(make_request()))
        self.assertEqual(caught.exception.status_code, 401)

    def test_valid_session_cookie_is_accepted(self) -> None:
        request = make_request(cookies=self.login_cookies())
        self.assertEqual(asyncio.run(current_user(request)), "alice")

    def test_deleted_user_session_is_invalidated(self) -> None:
        cookies = self.login_cookies()
        context.users.remove("alice")
        with self.assertRaises(HTTPException):
            asyncio.run(current_user(make_request(cookies=cookies)))

    def test_index_redirects_to_login_without_session(self) -> None:
        response = asyncio.run(index(make_request()))
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/login")

    def test_login_rejects_wrong_password_and_throttles(self) -> None:
        request = make_request(
            method="POST", headers={"origin": "https://vat.local", "host": "vat.local"}
        )
        payload = LoginRequest(username="alice", password="wrong-password-xx")
        for _ in range(5):
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(login(request, payload, Response(), None))
            self.assertEqual(caught.exception.status_code, 401)
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(login(request, payload, Response(), None))
        self.assertEqual(caught.exception.status_code, 429)

    def test_login_success_sets_cookies(self) -> None:
        request = make_request(
            method="POST", headers={"origin": "https://vat.local", "host": "vat.local"}
        )
        response = Response()
        result = asyncio.run(
            login(
                request,
                LoginRequest(username="Alice", password="correct-horse-battery"),
                response,
                None,
            )
        )
        self.assertEqual(result["user"], "alice")
        cookie_header = ",".join(
            value.decode() for key, value in response.raw_headers if key == b"set-cookie"
        )
        self.assertIn(SESSION_COOKIE, cookie_header)
        self.assertIn(CSRF_COOKIE, cookie_header)
        self.assertIn("SameSite=strict", cookie_header)
        self.assertIn("HttpOnly", cookie_header)


class CsrfTests(WebAuthContext):
    def test_post_without_origin_is_rejected(self) -> None:
        request = make_request(method="POST", cookies=self.login_cookies())
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(require_csrf(request))
        self.assertEqual(caught.exception.status_code, 403)

    def test_post_with_mismatched_origin_is_rejected(self) -> None:
        request = make_request(
            method="POST",
            headers={"origin": "https://evil.example", "host": "vat.local"},
            cookies=self.login_cookies(),
        )
        with self.assertRaises(HTTPException):
            asyncio.run(require_csrf(request))

    def test_post_without_matching_token_is_rejected(self) -> None:
        request = make_request(
            method="POST",
            headers={
                "origin": "https://vat.local",
                "host": "vat.local",
                CSRF_HEADER: "different-token",
            },
            cookies=self.login_cookies(),
        )
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(require_csrf(request))
        self.assertEqual(caught.exception.status_code, 403)

    def test_post_with_matching_token_passes(self) -> None:
        request = make_request(
            method="POST",
            headers={
                "origin": "https://vat.local",
                "host": "vat.local",
                CSRF_HEADER: "csrf-token-value",
            },
            cookies=self.login_cookies(),
        )
        asyncio.run(require_csrf(request))


class StartRequestTests(unittest.TestCase):
    def test_client_supplied_config_path_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            StartRequest(config_path="/etc/passwd", extracted_confirmed=True)


class SetupTests(WebAuthContext):
    def _empty_store(self) -> None:
        for name in list(context.users.usernames()):
            context.users.remove(name)
        context.setup_token = None

    def _origin_request(self) -> Request:
        return make_request(
            method="POST", headers={"origin": "https://vat.local", "host": "vat.local"}
        )

    def test_setup_creates_admin_with_valid_token(self) -> None:
        self._empty_store()
        token = context.ensure_setup_token()
        result = asyncio.run(
            setup(
                self._origin_request(),
                SetupRequest(
                    token=token, username="Boss", password="long-enough-password"
                ),
                Response(),
                None,
            )
        )
        self.assertEqual(result["user"], "boss")
        self.assertTrue(context.users.is_admin("boss"))

    def test_setup_rejects_wrong_token(self) -> None:
        self._empty_store()
        context.ensure_setup_token()
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(
                setup(
                    self._origin_request(),
                    SetupRequest(
                        token="wrong", username="boss", password="long-enough-password"
                    ),
                    Response(),
                    None,
                )
            )
        self.assertEqual(caught.exception.status_code, 403)
        self.assertTrue(context.users.is_empty())

    def test_setup_rejected_once_users_exist(self) -> None:
        context.setup_token = "leftover-token"
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(
                setup(
                    self._origin_request(),
                    SetupRequest(
                        token="leftover-token",
                        username="mallory",
                        password="long-enough-password",
                    ),
                    Response(),
                    None,
                )
            )
        self.assertEqual(caught.exception.status_code, 409)

    def test_index_redirects_to_setup_when_no_users(self) -> None:
        self._empty_store()
        response = asyncio.run(index(make_request()))
        self.assertEqual(response.headers["location"], "/setup")


class AdminTests(WebAuthContext):
    def test_first_user_is_admin_second_is_not(self) -> None:
        self.assertTrue(context.users.is_admin("alice"))
        self.assertFalse(context.users.is_admin("bob"))

    def test_status_reports_admin_flag(self) -> None:
        self.assertTrue(asyncio.run(status(username="alice"))["admin"])
        self.assertFalse(asyncio.run(status(username="bob"))["admin"])

    def test_non_admin_cannot_manage_users(self) -> None:
        request = make_request(cookies=self.login_cookies("bob"))
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(current_admin(request))
        self.assertEqual(caught.exception.status_code, 403)

    def test_admin_dependency_accepts_admin(self) -> None:
        request = make_request(cookies=self.login_cookies("alice"))
        self.assertEqual(asyncio.run(current_admin(request)), "alice")

    def test_admin_creates_resets_and_deletes_user(self) -> None:
        created = asyncio.run(
            create_user(
                UserCreateRequest(username="carol", password="carol-long-password"),
                admin="alice",
                _=None,
            )
        )
        self.assertEqual(created["action"], "created")
        self.assertIn(
            ("carol", False),
            [
                (item["username"], item["admin"])
                for item in asyncio.run(list_users(_admin="alice"))["users"]
            ],
        )
        reset = asyncio.run(
            create_user(
                UserCreateRequest(username="carol", password="carol-new-password-1"),
                admin="alice",
                _=None,
            )
        )
        self.assertEqual(reset["action"], "reset")
        self.assertTrue(context.users.verify("carol", "carol-new-password-1"))
        asyncio.run(
            delete_user(UserNameRequest(username="carol"), admin="alice", _=None)
        )
        self.assertFalse(context.users.exists("carol"))

    def test_last_admin_cannot_be_deleted(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(
                delete_user(UserNameRequest(username="alice"), admin="alice", _=None)
            )
        self.assertEqual(caught.exception.status_code, 400)
        self.assertTrue(context.users.exists("alice"))

    def test_deleting_missing_user_returns_404(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(
                delete_user(UserNameRequest(username="ghost"), admin="alice", _=None)
            )
        self.assertEqual(caught.exception.status_code, 404)


class JobManagerTests(WebAuthContext):
    def test_identity_documents_are_isolated_per_user(self) -> None:
        manager = context.jobs()
        manager.store_identity_documents(
            "alice", [("one.pdf", b"one"), ("two.png", b"two"), ("three.txt", b"three")]
        )
        self.assertEqual(manager.snapshot("alice")["identity_documents"], 3)
        self.assertEqual(manager.snapshot("bob")["identity_documents"], 0)
        alice_dir = manager.session("alice").upload_dir
        bob_dir = manager.session("bob").upload_dir
        self.assertNotEqual(alice_dir, bob_dir)
        self.assertEqual(alice_dir.stat().st_mode & 0o777, 0o700)

    def test_identity_files_are_private_and_provided_in_order(self) -> None:
        manager = context.jobs()
        manager.store_identity_documents(
            "alice", [("one.pdf", b"one"), ("two.png", b"two"), ("three.txt", b"three")]
        )
        session = manager.session("alice")
        session.active_identity_documents = list(session.identity_documents)
        paths = [
            Path(asyncio.run(manager._identity_file(session, "document")))
            for _ in range(3)
        ]
        self.assertEqual(
            [path.read_bytes() for path in paths], [b"one", b"two", b"three"]
        )
        self.assertTrue(all(path.stat().st_mode & 0o777 == 0o600 for path in paths))
        self.assertFalse(asyncio.run(manager._identity_files_remaining(session)))

    def test_invalid_second_file_cleans_partial_write(self) -> None:
        manager = context.jobs()
        session = manager.session("alice")
        before = set(session.upload_dir.iterdir())
        with self.assertRaisesRegex(ValueError, "不支持"):
            manager.store_identity_documents(
                "alice",
                [("one.pdf", b"one"), ("bad.exe", b"bad"), ("three.txt", b"three")],
            )
        self.assertEqual(set(session.upload_dir.iterdir()), before)

    def test_start_rejects_unconfirmed_extraction(self) -> None:
        with self.assertRaisesRegex(ValueError, "检查并确认"):
            context.jobs().start("alice", StartRequest(extracted_confirmed=False))

    def test_start_reports_other_users_running_task(self) -> None:
        manager = context.jobs()
        manager.store_identity_documents(
            "bob", [("one.pdf", b"one"), ("two.png", b"two"), ("three.txt", b"three")]
        )
        with manager._lock:
            manager._busy_with = "alice"
        with self.assertRaisesRegex(RuntimeError, "alice"):
            manager.start("bob", StartRequest(extracted_confirmed=True))
        self.assertEqual(manager.snapshot("bob")["busy_with"], "alice")
        self.assertEqual(manager.snapshot("alice")["busy_with"], "")

    def test_pause_checkpoint_keeps_job_alive_and_returns_updated_values(self) -> None:
        manager = context.jobs()
        session = manager.session("alice")
        with manager._lock:
            session.state["status"] = "running"
        manager.pause("alice")

        async def scenario() -> dict[str, str]:
            task = asyncio.create_task(
                manager._pause_checkpoint(
                    session,
                    "https://example.test/register-for-vat/telephone-number",
                    "What is your telephone number?",
                )
            )
            try:
                for _ in range(50):
                    await asyncio.sleep(0.005)
                    if manager.snapshot("alice")["status"] == "paused":
                        break
                self.assertEqual(manager.snapshot("alice")["status"], "paused")
                manager.continue_after_pause(
                    "alice",
                    ContinueRequest(
                        extracted_values={"phone": "8613592850576"},
                        extracted_confirmed=True,
                    ),
                )
                return await task
            finally:
                # 断言失败时也要放行等待线程，否则 asyncio.run 退出时
                # 会 join 线程池，把一次失败放大成 30 分钟挂起。
                session.resume_event.set()
                task.cancel()

        updates = asyncio.run(scenario())
        self.assertEqual(updates, {"phone": "8613592850576"})
        self.assertEqual(manager.snapshot("alice")["status"], "running")

    def test_final_review_waits_for_confirmation_and_exposes_documents(self) -> None:
        manager = context.jobs()
        session = manager.session("alice")
        artifact = Path(self._workspace.name) / "final.pdf"
        artifact.write_bytes(b"%PDF-1.4 fake")

        async def scenario() -> None:
            task = asyncio.create_task(
                manager._final_review(
                    session,
                    {
                        "url": "https://example.test/check-your-answers",
                        "heading": "Check your answers",
                        "pdf": str(artifact),
                        "screenshot": "",
                    },
                )
            )
            try:
                for _ in range(50):
                    await asyncio.sleep(0.005)
                    if manager.snapshot("alice")["status"] == "reviewing":
                        break
                state = manager.snapshot("alice")
                self.assertEqual(state["status"], "reviewing")
                self.assertTrue(state["final_review"]["available"])
                self.assertTrue(state["final_review"]["pdf"])
                manager.confirm_final_review("alice")
                await task
            finally:
                # 同 pause 测试：失败时也放行等待线程，避免挂起整套测试。
                session.review_event.set()
                task.cancel()

        asyncio.run(scenario())
        self.assertEqual(manager.final_review_document("alice", "pdf"), artifact)
        # 其他用户拿不到不属于自己的最终复核文件。
        self.assertIsNone(manager.final_review_document("bob", "pdf"))

    def test_final_review_endpoint_rejects_user_without_document(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(final_review_pdf(username="bob"))
        self.assertEqual(caught.exception.status_code, 404)

    def test_final_submit_requires_explicit_confirmation(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(
                final_review_confirm(
                    FinalSubmitRequest(confirmed=False), username="alice", _=None
                )
            )
        self.assertEqual(caught.exception.status_code, 400)

    def test_reinjected_rules_do_not_accumulate(self) -> None:
        manager = context.jobs()
        session = manager.session("alice")
        settings = Settings(
            start_url="https://example.test",
            profile_dir=Path(self._workspace.name) / "profile",
            artifacts_dir=Path(self._workspace.name) / "artifacts",
            answers={},
            pages=[
                PageRule(
                    path_contains="/register-for-vat/application-reference",
                    answers={"value": "OLD-REFERENCE"},
                )
            ],
        )
        values = {
            "application_reference": "AB223322-UK-Example Ltd",
            "email": "person@example.test",
        }
        manager._apply_extracted_values(session, settings, values)
        first_count = len(settings.pages)
        manager._apply_extracted_values(session, settings, values)
        self.assertEqual(len(settings.pages), first_count)
        self.assertEqual(
            settings.answer_for(
                "value",
                "https://example.test/register-for-vat/application-reference",
                "Choose an application reference",
            ),
            (True, "AB223322-UK-Example Ltd"),
        )


class EndpointTests(WebAuthContext):
    def test_status_returns_only_own_state(self) -> None:
        manager = context.jobs()
        # session() 内部会拿 manager._lock（非重入锁），必须在持锁前取出。
        session = manager.session("alice")
        with manager._lock:
            session.state["heading"] = "Alice page"
        self.assertEqual(asyncio.run(status(username="alice"))["heading"], "Alice page")
        self.assertEqual(asyncio.run(status(username="bob"))["heading"], "")

    def test_parse_document_endpoint(self) -> None:
        result = asyncio.run(
            parse_document(
                UploadFile(
                    filename="vat.txt",
                    file=io.BytesIO(b"Business name: Endpoint Ltd"),
                ),
                username="alice",
                _=None,
            )
        )
        self.assertEqual(result["values"]["business_name"], "Endpoint Ltd")

    def test_home_page_contains_review_and_login_flow(self) -> None:
        static_dir = Path(__file__).resolve().parents[1] / "vat_automation" / "static"
        html = (static_dir / "index.html").read_text(encoding="utf-8")
        self.assertIn("最终核对", html)
        self.assertIn("/api/final-review/pdf", html)
        self.assertIn("确认并提交到 HMRC", html)
        self.assertIn("review-submit-consent", html)
        self.assertIn("{confirmed:true}", html)
        self.assertIn("下载完整 PDF 存档", html)
        self.assertNotIn("review-frame", html)
        self.assertNotIn("review-print", html)
        self.assertIn("X-CSRF-Token", html)
        self.assertIn("配置文件（由服务端启动参数固定）", html)
        self.assertNotIn("config_path", html)
        self.assertIn("账号管理", html)
        self.assertIn("/api/users", html)
        login_html = (static_dir / "login.html").read_text(encoding="utf-8")
        self.assertIn("/api/login", login_html)
        setup_html = (static_dir / "setup.html").read_text(encoding="utf-8")
        self.assertIn("/api/setup", setup_html)
        self.assertIn("初始化码", setup_html)
        self.assertIn("至少 12 个字符", setup_html)


if __name__ == "__main__":
    unittest.main()
>>>>>>> Stashed changes
