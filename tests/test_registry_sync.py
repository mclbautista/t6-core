"""Registry changes reach Notion through the queue and a worker; Notion changes are reconciled.

One process: the server app, a worker that calls its tailnet handler, and a
fake Notion. No test opens a connection.
"""

from datetime import datetime, timezone
from typing import Any
import uuid

from test_registry_codes import CodesTestCase
from test_registry_service import READER_SECRET, WORKER_SECRET, WRITER_SECRET
from test_server_app import NOTION_TOKEN, InProcess, notion_request

from t6.notion import codec
from t6.notion.projection import ROTATION, T6_ID, ProjectProjection
from t6.notion.testing import FakeNotion, project_properties
from t6.server import Grant, Request, ServerConfig
from t6.worker import QueueClient, Worker
from t6.worker.__main__ import WorkerSettings, configured_handlers
from t6.worker.registry import RegistryClient

PROJECTS = "11111111111111111111111111111111"
OTHER_SOURCE = "22222222222222222222222222222222"


class SyncTestCase(CodesTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.config = ServerConfig(
            notion_webhook_token=NOTION_TOKEN,
            grants=(
                Grant(
                    WORKER_SECRET,
                    claim=frozenset({"main"}),
                    submit=frozenset({"main"}),
                    registry_read=True,
                    registry_sync=True,
                    actor="main-worker",
                ),
                Grant(WRITER_SECRET, registry_read=True, registry_write=True, actor="writer"),
                Grant(READER_SECRET, registry_read=True, actor="reader"),
            ),
        )
        self.start()
        self.notion = FakeNotion()
        self.notion.add_snapshot_data_source(PROJECTS, "projects")
        self.notion.add_property(PROJECTS, T6_ID, "rich_text")
        self.notion.add_property(PROJECTS, ROTATION, "select")
        self.notion.add_data_source(OTHER_SOURCE)
        self.client = self.notion.client()
        self.addCleanup(self.client.close)
        self.events = 0

    def worker(self) -> Worker:
        transport = InProcess(self.app)  # the current app, so it follows a restart
        projection = ProjectProjection(self.client, RegistryClient(transport, WORKER_SECRET), PROJECTS)
        return Worker(QueueClient(transport, WORKER_SECRET), "main", projection.handlers())

    def drain(self) -> int:
        """Run jobs until none is due. Returns how many ran."""
        worker, ran = self.worker(), 0
        while worker.run_once():
            ran += 1
        return ran

    def page(self, name: str = "Show", code: str | None = None, source: str = PROJECTS) -> str:
        properties = project_properties(name=name, project_code=code) if source == PROJECTS else {}
        return str(self.notion.add_page(source, properties, page_id=uuid.uuid4().hex)["id"])

    def shown(self, page: str) -> tuple[str | None, str | None, str | None]:
        properties = self.notion.page(page)["properties"]
        return (
            codec.rich_text(properties.get(T6_ID)),
            codec.select(properties.get("Project Code")),
            codec.select(properties.get(ROTATION)),
        )

    def edit_in_notion(self, page: str, **body: Any) -> None:
        self.client.update_page(page, body)

    def webhook(self, page: str, kind: str = "page.properties_updated") -> None:
        self.events += 1
        event = {
            "id": f"event-{self.events}",
            "timestamp": datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(),
            "type": kind,
            "entity": {"id": page, "type": "page"},
            "data": {},
        }
        answer = self.app.public(notion_request(self.clock, event))
        self.assertEqual(answer.status, 200, answer.body)
        self.assertIn("job_id", answer.body)

    def items(self) -> list[dict[str, Any]]:
        answer = self.send("GET", "/registry/review-items")
        self.assertEqual(answer.status, 200)
        found: list[dict[str, Any]] = list(answer.body["items"])  # type: ignore[arg-type]
        return found


class ProjectionTests(SyncTestCase):
    def test_an_accepted_change_is_pending_at_once_and_reaches_notion_through_the_worker(self) -> None:
        page = self.page()
        project = self.new("ABC", "in", page=page)
        self.assertEqual(project["sync"], "pending")  # accepted before Notion was touched
        self.assertEqual(self.shown(page), (None, None, None))

        self.assertEqual(self.drain(), 1)
        self.assertEqual(self.shown(page), (project["id"], "ABC", "in"))
        self.assertEqual(self.read(project)["sync"], "synced")

        self.set_code(project, "XYZ")
        self.assertEqual(self.read(project)["sync"], "pending")
        self.drain()
        self.assertEqual(self.shown(page), (project["id"], "XYZ", "in"))
        self.assertEqual(self.read(project)["sync"], "synced")

    def test_a_project_without_a_page_syncs_without_touching_notion(self) -> None:
        project = self.new("ABC")
        self.drain()
        self.assertEqual(self.read(project)["sync"], "synced")
        self.assertEqual(self.notion.requests, [])

    def test_an_old_job_applies_the_current_state_not_its_own(self) -> None:
        page = self.page()
        project = self.new("ABC", page=page)
        self.set_code(project, "XYZ")
        self.rotate(project, "in")
        worker = self.worker()
        self.assertTrue(worker.run_once())  # the job queued for revision 1
        self.assertEqual(self.shown(page), (project["id"], "XYZ", "in"))
        self.assertEqual(self.read(project)["sync"], "synced")
        self.assertEqual(self.drain(), 2)  # the later jobs change nothing
        self.assertEqual(self.shown(page), (project["id"], "XYZ", "in"))

    def test_a_change_committed_before_a_crash_is_dispatched_after_the_restart(self) -> None:
        page = self.page()
        project = self.new(page=page)
        self.drain()
        # The registry commits a change, then the server dies before it queues the job.
        self.registry.set_code(project["id"], "ABC", actor="writer", idempotency_key="crash", expected_revision=1)
        self.assertIsNone(self.queue.claim("main", 1))
        self.start()
        self.assertEqual(self.drain(), 1)
        self.assertEqual(self.shown(page), (project["id"], "ABC", "out"))

    def test_a_notion_failure_keeps_the_change_and_the_sync_is_retried(self) -> None:
        page = self.page()
        project = self.new("ABC", "in", page=page)
        self.notion.fail_next(500, code="internal_server_error", times=1)
        self.drain()
        waiting = self.read(project)
        self.assertEqual((waiting["code"], waiting["rotation"]), ("ABC", "in"))
        self.assertEqual((waiting["sync"], waiting["sync_error"]), ("pending", "internal_server_error"))
        self.clock.advance(31)
        self.drain()
        done = self.read(project)
        self.assertEqual((done["sync"], done["sync_error"]), ("synced", None))
        self.assertEqual(self.shown(page), (project["id"], "ABC", "in"))

    def test_a_sync_that_keeps_failing_is_reported_and_a_later_sync_clears_it(self) -> None:
        page = self.page()
        project = self.new("ABC", "in", page=page)
        self.notion.remove_property(PROJECTS, ROTATION)  # the projection property is missing in Notion
        for _ in range(self.queue.max_attempts):
            self.drain()
            self.clock.advance(31)
        failed = self.read(project)
        self.assertEqual((failed["sync"], failed["sync_error"]), ("failed", "validation_error"))
        self.assertEqual((failed["code"], failed["rotation"]), ("ABC", "in"))
        self.assertEqual(self.drain(), 0)

        self.notion.add_property(PROJECTS, ROTATION, "select")
        self.set_code(project, "XYZ")
        self.drain()
        self.assertEqual(self.read(project)["sync"], "synced")
        self.assertEqual(self.shown(page), (project["id"], "XYZ", "in"))


class ReconciliationTests(SyncTestCase):
    def synced(self, code: str = "ABC") -> tuple[dict[str, Any], str]:
        page = self.page()
        project = self.new(code, "in", page=page)
        self.drain()
        return project, page

    def test_an_edit_to_a_registry_owned_value_is_reported_and_put_back(self) -> None:
        project, page = self.synced()
        before = self.read(project)
        self.edit_in_notion(page, properties={"Project Code": {"select": {"name": "HACK"}}})
        self.webhook(page)
        self.drain()

        after = self.read(project)
        self.assertEqual((after["code"], after["revision"]), (before["code"], before["revision"]))
        self.assertEqual(self.shown(page), (project["id"], "ABC", "in"))
        (item,) = self.items()
        self.assertEqual((item["kind"], item["project_id"], item["page_id"]), ("projection_conflict", project["id"], page))
        self.assertEqual(item["detail"]["shown"]["code"], "HACK")
        self.assertEqual(item["detail"]["expected"]["code"], "ABC")

        self.webhook(page)  # Notion reports the write that put the value back
        self.drain()
        self.assertEqual(self.items(), [])

    def test_an_edit_to_a_descriptive_field_is_left_alone(self) -> None:
        project, page = self.synced()
        self.edit_in_notion(page, properties={"Status": {"select": {"name": "Delivered"}}})
        self.webhook(page)
        self.drain()
        self.assertEqual(self.items(), [])
        self.assertEqual(codec.select(self.notion.page(page)["properties"]["Status"]), "Delivered")
        self.assertEqual(self.read(project)["revision"], 2 - 1)

    def test_a_webhook_that_arrives_before_the_sync_is_not_a_conflict(self) -> None:
        page = self.page()
        project = self.new("ABC", "in", page=page)
        self.webhook(page)  # queued behind the sync job, but claim order is not promised
        claimed = self.queue.claim("main", 60)  # hold the sync job back
        assert claimed is not None and claimed.kind == "registry.sync"
        self.drain()
        self.assertEqual(self.items(), [])
        self.assertEqual(self.read(project)["sync"], "pending")

    def test_an_unbound_page_becomes_one_review_item_and_no_project(self) -> None:
        page = self.page(code="ABC")
        self.new("ABC", "in")  # same code, different Project: never merged by code
        self.drain()
        intents = len(self.registry.pending_sync())
        self.webhook(page, "page.created")
        self.webhook(page)
        self.drain()
        (item,) = self.items()
        self.assertEqual((item["kind"], item["project_id"]), ("unbound_page", None))
        self.assertEqual(self.send("GET", f"/registry/pages/{page}").status, 404)
        self.assertEqual(len(self.registry.pending_sync()), intents)

        bound = self.new(page=page)  # the operator registers it explicitly
        self.assertEqual(self.items(), [])
        self.drain()
        self.assertEqual(self.shown(page)[0], bound["id"])

    def test_an_unbound_page_is_offered_to_the_operator_with_its_title(self) -> None:
        page = self.page(name="Some Show")
        offered: list[tuple[str, str]] = []
        transport = InProcess(self.app)
        projection = ProjectProjection(
            self.client, RegistryClient(transport, WORKER_SECRET), PROJECTS, offer=lambda p, label: offered.append((p, label))
        )
        self.webhook(page)
        worker = Worker(QueueClient(transport, WORKER_SECRET), "main", projection.handlers())
        while worker.run_once():
            pass
        self.assertEqual(offered, [(page, "Some Show")])

    def test_a_page_outside_the_projects_data_source_is_ignored(self) -> None:
        page = self.page(source=OTHER_SOURCE)
        self.webhook(page)
        self.drain()
        self.assertEqual(self.items(), [])

    def test_a_deleted_page_flags_the_binding_and_keeps_the_project_and_its_code(self) -> None:
        project, page = self.synced()
        self.edit_in_notion(page, in_trash=True)
        self.webhook(page, "page.deleted")
        self.drain()

        gone = self.read(project)
        self.assertEqual(gone["bindings"][0]["available"], False)
        self.assertEqual((gone["id"], gone["code"], gone["rotation"], gone["revision"]), (project["id"], "ABC", "in", 1))
        (item,) = self.items()
        self.assertEqual((item["kind"], item["project_id"]), ("page_unavailable", project["id"]))
        self.assertEqual(self.registry.audit(project["id"])[-1].action, "page_unavailable")
        self.assertEqual(self.send("GET", f"/registry/pages/{page}").status, 200)  # still resolves
        rival = self.new("ABC")
        self.assertEqual(self.rotate(rival, "in").status, 409)  # the code is not freed

    def test_a_restored_page_is_reconciled_through_its_binding(self) -> None:
        project, page = self.synced()
        self.edit_in_notion(page, in_trash=True)
        self.webhook(page, "page.deleted")
        self.drain()
        self.set_code(project, "XYZ")  # accepted while the page is gone
        self.drain()
        waiting = self.read(project)
        self.assertEqual((waiting["code"], waiting["sync"], waiting["sync_error"]), ("XYZ", "failed", "page unavailable"))

        self.edit_in_notion(page, in_trash=False)
        self.webhook(page, "page.undeleted")
        self.drain()
        back = self.read(project)
        self.assertEqual((back["bindings"][0]["available"], back["sync"]), (True, "synced"))
        self.assertEqual(self.shown(page), (project["id"], "XYZ", "in"))
        self.assertEqual(self.items(), [])
        self.assertEqual(self.registry.audit(project["id"])[-1].action, "page_restored")

    def test_a_late_deletion_event_for_a_restored_page_changes_nothing(self) -> None:
        project, page = self.synced()
        self.webhook(page, "page.deleted")  # arrives after the page was already restored
        self.webhook(page, "page.deleted")
        self.drain()
        self.assertEqual(self.read(project)["bindings"][0]["available"], True)
        self.assertEqual(self.items(), [])

    def test_a_page_notion_no_longer_returns_is_unavailable(self) -> None:
        project = self.new("ABC", "in", page="33333333333333333333333333333333")  # Notion has no such page
        self.drain()
        gone = self.read(project)
        self.assertEqual((gone["bindings"][0]["available"], gone["sync"]), (False, "failed"))

    def test_an_operator_can_ask_for_a_page_to_be_reconciled(self) -> None:
        project, page = self.synced()
        self.edit_in_notion(page, properties={ROTATION: {"select": {"name": "out"}}})
        asked = self.send("POST", f"/registry/pages/{page}/reconcile", {}, key="please")
        self.assertEqual((asked.status, asked.body["created"]), (200, True))
        self.assertEqual(self.send("POST", f"/registry/pages/{page}/reconcile", {}, key="please").body["created"], False)
        self.drain()
        self.assertEqual(self.shown(page), (project["id"], "ABC", "in"))


class SyncAccessTests(SyncTestCase):
    def test_sync_routes_need_the_sync_grant(self) -> None:
        project = self.new("ABC")
        report = {"revision": 1, "ok": True}
        seen = {"available": True, "shown": None}
        for secret in (WRITER_SECRET, READER_SECRET):
            self.assertEqual(self.send("POST", f"/registry/projects/{project['id']}/sync", report, secret=secret).status, 403)
            self.assertEqual(self.send("POST", "/registry/pages/p1/observed", seen, secret=secret).status, 403)
        self.assertEqual(self.send("POST", "/registry/pages/p1/reconcile", {}, secret=WORKER_SECRET, key="k").status, 403)
        self.assertEqual(self.send("POST", "/registry/pages/p1/observed", seen, secret="wrong").status, 401)
        self.assertEqual(self.read(project)["sync"], "pending")
        self.assertEqual(self.items(), [])

    def test_the_sync_grant_changes_no_project(self) -> None:
        project = self.new("ABC")
        self.assertEqual(self.rotate(project, "in", secret=WORKER_SECRET).status, 403)
        self.assertEqual(self.send("POST", "/registry/projects", {}, secret=WORKER_SECRET, key="k").status, 403)

    def test_bad_sync_reports_are_refused(self) -> None:
        project = self.new("ABC")
        path = f"/registry/projects/{project['id']}/sync"
        self.assertEqual(self.send("POST", path, {"ok": True}, secret=WORKER_SECRET).status, 400)
        self.assertEqual(self.send("POST", "/registry/projects/t6p_nope/sync", {"revision": 1, "ok": True}, secret=WORKER_SECRET).status, 404)
        self.assertEqual(self.send("POST", "/registry/pages/p1/observed", {"shown": {}}, secret=WORKER_SECRET).status, 400)
        self.assertEqual(self.send("GET", path, secret=WORKER_SECRET).status, 405)

    def test_the_public_listener_serves_no_sync_routes(self) -> None:
        headers = {"Authorization": f"Bearer {WORKER_SECRET}"}
        answer = self.app.public(Request("POST", "/registry/pages/p1/observed", headers, b'{"available": true}'))
        self.assertEqual(answer.status, 404)
        self.assertEqual(self.registry.review_items(), ())


class WorkerRegistrationTests(SyncTestCase):
    SETTINGS = WorkerSettings("http://server.invalid:8081", WORKER_SECRET, "main")

    def test_the_main_worker_registers_the_notion_handlers_when_configured(self) -> None:
        environ = {"T6_NOTION_TOKEN": "token", "T6_NOTION_PROJECTS_DATA_SOURCE": PROJECTS}
        handlers = configured_handlers(environ, self.SETTINGS, InProcess(self.app))
        self.assertEqual(set(handlers["main"]), {"registry.sync", "notion.page_changed"})

    def test_no_handlers_without_notion_settings_or_on_another_queue(self) -> None:
        transport = InProcess(self.app)
        self.assertEqual(configured_handlers({}, self.SETTINGS, transport), {})
        frameio = WorkerSettings("http://server.invalid:8081", WORKER_SECRET, "frameio")
        environ = {"T6_NOTION_TOKEN": "token", "T6_NOTION_PROJECTS_DATA_SOURCE": PROJECTS}
        self.assertEqual(configured_handlers(environ, frameio, transport), {})
