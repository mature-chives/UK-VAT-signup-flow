import asyncio
import tempfile
import unittest
from pathlib import Path

from workbench.app import (
    CustomerRequest,
    TaskCreateRequest,
    UkVatStartRequest,
    _automation_target,
    _sync_automation_task,
    create_task,
    plugins,
    save_customer,
    start_automation,
    upload_translation,
)
from workbench.catalog import (
    Plugin,
    get_plugin,
    list_plugins,
    load_builtin_plugins,
    register,
)
from workbench.store import WorkbenchStore
from workbench.app import context


class CatalogTests(unittest.TestCase):
    def test_builtin_plugins_are_registered(self) -> None:
        load_builtin_plugins()
        ids = {item.id for item in list_plugins()}
        self.assertEqual(
            ids,
            ids
            | {
                "uk-vat-register",
                "uk-eori-register",
                "sa-vat-register",
                "translate-id",
                "translate-license",
                "translate-poa",
            },
        )
        self.assertTrue(get_plugin("sa-vat-register").placeholder)
        self.assertEqual(get_plugin("uk-vat-register").task_kind, "uk_vat")
        self.assertEqual(get_plugin("uk-eori-register").task_kind, "uk_eori")

    def test_plugins_declare_intake_for_the_workbench_wizard(self) -> None:
        load_builtin_plugins()
        uk = get_plugin("uk-vat-register").public()["intake"]
        cats = {item["category"]: item for item in uk["files"]}
        self.assertEqual(cats["identity"]["min"], 3)
        self.assertTrue(cats["authorization"].get("parse"))
        eori = get_plugin("uk-eori-register").public()["intake"]
        eori_cats = {item["category"]: item for item in eori["files"]}
        self.assertNotIn("identity", eori_cats)
        self.assertTrue(eori_cats["authorization"].get("parse"))
        translate = get_plugin("translate-id").public()["intake"]
        cats = {item["category"]: item for item in translate["files"]}
        self.assertEqual(cats["id-card-front"]["min"], 1)
        self.assertEqual(cats["id-card-front"]["max"], 1)
        self.assertEqual(cats["id-card-back"]["min"], 1)
        self.assertEqual(cats["id-card-back"]["max"], 1)
        self.assertIn("正反面", get_plugin("translate-id").description)

    def test_new_business_is_a_plugin_registration(self) -> None:
        load_builtin_plugins()
        extra = Plugin(
            id="de-vat-register",
            name="德国 VAT 注册",
            group="注册",
            description="示例：新增业务只登记插件",
            task_kind="placeholder",
            placeholder=True,
            order=99,
        )
        if get_plugin(extra.id) is None:
            register(extra)
        self.assertEqual(get_plugin("de-vat-register").name, "德国 VAT 注册")
        self.assertIn("de-vat-register", {item.id for item in list_plugins()})


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._workspace = tempfile.TemporaryDirectory()
        self.store = WorkbenchStore(Path(self._workspace.name) / "data")
        context.store = self.store

    def tearDown(self) -> None:
        self._workspace.cleanup()

    def test_customer_files_can_be_reused_by_translate_and_placeholder(self) -> None:
        customer = self.store.upsert_customer(
            customer_id=None,
            name="杭州示例",
            project_code="AB223322",
            fields={"email": "a@b.c", "business_name": "Example Ltd"},
        )
        uploaded = self.store.add_customer_file(
            customer["id"],
            filename="id.jpg",
            content=b"fake-image",
            category="identity",
        )
        translate = asyncio.run(
            create_task(
                TaskCreateRequest(
                    plugin_id="translate-id",
                    customer_id=customer["id"],
                    field_keys=["email"],
                    file_ids=[uploaded["id"]],
                ),
                username="alice",
                _=None,
            )
        )
        self.assertEqual(translate["task_kind"], "translate")
        self.assertEqual(translate["selected_fields"]["email"], "a@b.c")
        self.assertEqual(translate["selected_files"][0]["id"], uploaded["id"])

        class FakeUpload:
            filename = "id-en.pdf"

            async def read(self, _limit: int) -> bytes:
                return b"%PDF"

        delivered = asyncio.run(
            upload_translation(
                translate["id"], "translation", FakeUpload(), "alice", None
            )
        )
        self.assertEqual(delivered["kind"], "translation")
        self.assertEqual(self.store.get_task(translate["id"])["status"], "delivered")

        saudi = asyncio.run(
            create_task(
                TaskCreateRequest(
                    plugin_id="sa-vat-register",
                    customer_id=customer["id"],
                    file_ids=[uploaded["id"]],
                ),
                username="alice",
                _=None,
            )
        )
        self.assertTrue(saudi["placeholder"])
        self.assertEqual(saudi["status"], "queued")

    def test_unknown_plugin_is_rejected(self) -> None:
        customer = asyncio.run(
            save_customer(CustomerRequest(name="示例"), "alice", None)
        )
        with self.assertRaises(Exception) as caught:
            asyncio.run(
                create_task(
                    TaskCreateRequest(
                        plugin_id="not-a-plugin", customer_id=customer["id"]
                    ),
                    username="alice",
                    _=None,
                )
            )
        self.assertEqual(caught.exception.status_code, 404)

    def test_plugins_endpoint_reads_registry(self) -> None:
        payload = asyncio.run(plugins("alice"))
        names = {item["name"] for item in payload["plugins"]}
        self.assertIn("英国 VAT 注册", names)
        self.assertIn("沙特 VAT 注册", names)
        self.assertIn("翻译营业执照", names)


class PageContractTests(unittest.TestCase):
    def test_pages_keep_workbench_hooks_and_share_stylesheet(self) -> None:
        static_dir = Path(__file__).resolve().parents[1] / "workbench" / "static"
        index = (static_dir / "index.html").read_text(encoding="utf-8")
        login = (static_dir / "login.html").read_text(encoding="utf-8")
        setup = (static_dir / "setup.html").read_text(encoding="utf-8")
        css = (static_dir / "app.css").read_text(encoding="utf-8")
        for page in (index, login, setup):
            self.assertIn("/assets/app.css", page)
        for hook in (
            "plugin-list",
            "customer-select",
            "save-customer",
            "upload-file",
            "parse-file",
            "create-task",
            "start-automation",
            "automation-box",
            "otp",
            "send-code",
            "translate-task",
            "upload-translation",
            "task-list",
            "id-card-ocr",
            "id-card-fields",
            "id-card-pdf",
            "id-card-hint",
            "field-picks-title",
            "customer-extra-fields",
        ):
            self.assertIn(f'id="{hook}"', index)
        self.assertIn("核对无误，生成翻译件 PDF", index)
        self.assertIn("id-card-front", index)
        self.assertIn("id-card-back", index)
        self.assertIn("/api/login", login)
        self.assertIn("location.replace", login)
        self.assertIn("pushState", index)
        self.assertIn("/api/setup", setup)
        self.assertIn("初始化码", setup)
        self.assertIn("--header:", css)
        self.assertIn(".task-grid", css)
        self.assertIn('id="home"', index)
        self.assertIn("要做哪件事", index)
        self.assertIn("全部业务", index)
        for hook in ("desk-view", "new-task", "new-view", "job-view", "back-desk"):
            self.assertIn(f'id="{hook}"', index)

    def test_stylesheet_route_rejects_non_css(self) -> None:
        from fastapi import HTTPException

        from workbench.app import asset

        response = asyncio.run(asset("app.css"))
        self.assertEqual(response.media_type, "text/css")
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(asset("index.html"))
        self.assertEqual(caught.exception.status_code, 404)


class AutomationRoutingTests(unittest.TestCase):
    """工作台按 task_kind 把任务分派到各自的流程配置。"""

    def setUp(self) -> None:
        self._workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self._workspace.cleanup)
        base = Path(self._workspace.name)
        self._saved = (
            context.store, context.jobs, context.task_owners,
            context.uk_vat_config, context.uk_eori_config,
        )
        context.store = WorkbenchStore(base / "data")
        context.jobs = {}
        context.task_owners = {}
        context.uk_vat_config = base / "vat-config.flow.json"
        context.uk_eori_config = base / "vat-config.eori.flow.json"
        self.addCleanup(self._restore)
        load_builtin_plugins()

    def _restore(self) -> None:
        (
            context.store, context.jobs, context.task_owners,
            context.uk_vat_config, context.uk_eori_config,
        ) = self._saved

    def _customer(self) -> str:
        record = context.store.upsert_customer(
            customer_id=None,
            name="杭州示例",
            project_code="AB000001",
            fields={"business_name": "Example Ltd", "vat_number": "GB123456789"},
        )
        return record["id"]

    def _task(self, plugin_id: str) -> dict:
        return asyncio.run(
            create_task(
                TaskCreateRequest(
                    plugin_id=plugin_id,
                    customer_id=self._customer(),
                    field_keys=["business_name", "vat_number"],
                ),
                username="alice",
                _=None,
            )
        )

    def test_each_business_gets_its_own_flow_config(self) -> None:
        self._task("uk-vat-register")
        self._task("uk-eori-register")
        self.assertEqual(
            context.job_manager("uk_vat").config_path, context.uk_vat_config
        )
        self.assertEqual(
            context.job_manager("uk_eori").config_path, context.uk_eori_config
        )
        # 两个业务的 JobManager 互不共用。
        self.assertIsNot(
            context.job_manager("uk_vat"), context.job_manager("uk_eori")
        )

    def test_flow_prefix_must_match_task_kind(self) -> None:
        task = self._task("uk-eori-register")
        with self.assertRaises(Exception) as caught:
            _automation_target(task["id"], "uk-vat")
        self.assertEqual(getattr(caught.exception, "status_code", None), 404)
        _, manager = _automation_target(task["id"], "uk-eori")
        self.assertEqual(manager.config_path, context.uk_eori_config)

    def test_snapshot_is_synced_for_eori_tasks(self) -> None:
        task = self._task("uk-eori-register")

        class StubManager:
            def snapshot(self, username: str) -> dict:
                return {"status": "reviewing", "message": "等待人工核对"}

        context.jobs["uk_eori"] = StubManager()
        context.task_owners[task["id"]] = "alice"
        synced = _sync_automation_task(context.store.get_task(task["id"]))
        self.assertEqual(synced["status"], "reviewing")
        self.assertEqual(synced["automation"]["message"], "等待人工核对")

    def test_eori_start_does_not_require_identity_documents(self) -> None:
        task = self._task("uk-eori-register")
        started: dict[str, object] = {}

        class StubManager:
            identity_documents_required = 0

            def start(self, username: str, request: object) -> None:
                started["user"] = username
                started["values"] = dict(request.extracted_values)

        context.jobs["uk_eori"] = StubManager()
        result = asyncio.run(
            start_automation(
                task["id"],
                "uk-eori",
                UkVatStartRequest(extracted_confirmed=True, extracted_values={}),
                "alice",
                None,
            )
        )
        self.assertEqual(result["status"], "starting")
        self.assertEqual(started["user"], "alice")
        # 没有勾选任何身份证明文件也不会报错，资料取任务上选的客户字段。
        self.assertEqual(started["values"], {"business_name": "Example Ltd", "vat_number": "GB123456789"})


if __name__ == "__main__":
    unittest.main()
