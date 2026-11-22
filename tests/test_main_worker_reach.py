"""The main worker reaches the registry through the API, and the API's own jobs reach the worker (#358).

The VPS server runs in edge mode: it serves `/health` and `/queue/*` and answers 404 to everything
else. The API on Sixto holds the registry. The main worker polls the edge queue and the internal job
table in the t6 database, and sends every registry call to `T6_API_URL`. No test opens a connection.
"""

from collections.abc import Mapping
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
from typing import Any
import unittest
from unittest.mock import patch

from test_operator import GATEWAY_SECRET, OPERATOR, PAGE_A, FakeGateway, OperatorTestCase
from test_registry_service import WORKER_SECRET
from test_registry_sync import PROJECTS, SyncTestCase
from test_server_app import TAP_SECRET, InProcess
from test_worker_review import FakeNotify

from t6.domain.events import JsonValue
from t6.deliverable import DeliverableStore
from t6.job_order import JobOrderStore
from t6.notion import codec
from t6.notion.generated import DeliverablesRow
from t6.operator import ActionStore, OperatorService
from t6.preview import PreviewTokens
from t6.project_writes import ProjectWrites
from t6.registry import Registered, SqliteProjectRegistry
from t6.server import Grant, JobQueue, Request, ServerConfig, WebhookApp
from t6.server.testing import FakeClock
from t6.verbs import Done, Preview
from t6.worker import QueueClient, Worker
from t6.worker.__main__ import ConfigError, WorkerSettings, configured_handlers, main
from t6.worker.client import WorkerJob
from t6.worker.gateway import GatewayClient
from t6.worker.internal_queue import InternalJobQueue, InternalQueueClient
from t6.worker.operator import CardDelivery, OperatorClient

EDGE_URL = "http://edge.invalid:8081"
API_URL = "http://api.invalid:8081"
GATEWAY_URL = "http://gateway.invalid"
SYNC_KINDS = ("registry.sync", "deliverable.sync", "jo.sync", "project.status_sync")


class Recording:
    """A transport that remembers every path it was asked for."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.paths: list[str] = []

    def send(self, method: str, path: str, headers: Mapping[str, str], body: bytes) -> tuple[int, dict[str, Any]]:
        self.paths.append(path)
        return self.inner.send(method, path, headers, body)  # type: ignore[no-any-return]


class Reach:
    """Edge server, API and main worker on one fake clock. Mixed into a registry test case."""

    clock: FakeClock
    directory: tempfile.TemporaryDirectory[str]
    app: WebhookApp
    path: Path
    client: Any
    notion: Any

    def build_edge(self) -> None:
        main_queue = frozenset({"main"})
        config = ServerConfig(edge_routes=(), grants=(Grant(WORKER_SECRET, claim=main_queue, submit=main_queue),))
        self.edge_queue = JobQueue(Path(self.directory.name) / "edge.sqlite3", self.clock)
        self.edge = Recording(InProcess(WebhookApp(self.edge_queue, config, self.clock)))
        self.api = Recording(InProcess(self.app))
        self.gateway_notify = FakeNotify()

    def transport_for(self, url: str, *rest: Any, **named: Any) -> Any:
        return {EDGE_URL: self.edge, API_URL: self.api, GATEWAY_URL: self.gateway_notify}[url]

    def environ(self, **extra: str) -> dict[str, str]:
        return {
            "T6_SERVER_URL": EDGE_URL,
            "T6_API_URL": API_URL,
            "T6_DATABASE": str(self.path),
            "T6_WORKER_SECRET": WORKER_SECRET,
            "T6_WORKER_QUEUE": "main",
            "T6_NOTION_TOKEN": "fake",
            "T6_NOTION_PROJECTS_DATA_SOURCE": PROJECTS,
            "T6_NOTION_JOB_ORDERS_DATA_SOURCE": "job-orders",
            "T6_NOTION_DELIVERABLES_DATA_SOURCE": "deliverables",
            **extra,
        }

    def handlers(self, **extra: str) -> dict[str, Any]:
        environ = self.environ(**extra)
        with patch("t6.worker.__main__.HttpTransport", side_effect=self.transport_for), patch(
            "t6.notion.NotionClient", return_value=self.client
        ):
            built = configured_handlers(environ, WorkerSettings.from_env(environ), self.edge)
        return dict(built["main"])

    def main_worker(self, **extra: str) -> Worker:
        """What `main` builds for the `main` queue: the edge queue, the internal job table and the configured handlers."""
        internal = InternalQueueClient(InternalJobQueue(self.path, self.clock))
        return Worker(QueueClient(self.edge, WORKER_SECRET), "main", self.handlers(**extra), internal_client=internal)

    def drain_main(self, **extra: str) -> int:
        worker, ran = self.main_worker(**extra), 0
        while worker.run_once():
            ran += 1
            self.clock.advance(60)
        return ran

    def assert_edge_saw_only_queue_calls(self) -> None:
        self.assertEqual([p for p in self.edge.paths if not p.startswith("/queue/")], [])  # type: ignore[attr-defined]


class EdgeReachTests(Reach, SyncTestCase):
    """The API has no `inside` queue here: this is what the worker does with the edge it polls."""

    def setUp(self) -> None:
        super().setUp()
        self.build_edge()

    def test_a_notion_job_claimed_from_the_edge_changes_the_registry(self) -> None:
        page = self.page()
        project = self.new("ABC", "in", page=page)
        self.drain()  # the sync job the API queued, worked off over the API
        self.edit_in_notion(page, properties={"Project Code": {"select": {"name": "HACK"}}})
        self.edge_queue.submit(
            "main",
            "notion.page_changed",
            "notion:event:e1",
            {"page_id": page, "updated_properties": [], "event_id": "e1", "event_type": "page.properties_updated"},
        )
        self.assertEqual(self.drain_main(), 1)
        (item,) = self.items()
        self.assertEqual((item["kind"], item["project_id"], item["page_id"]), ("projection_conflict", project["id"], page))
        self.assert_edge_saw_only_queue_calls()
        self.assertTrue(any(p.startswith("/registry/") for p in self.api.paths))

    def test_no_registry_or_operator_call_goes_to_the_queue_server(self) -> None:
        handlers = self.handlers(T6_GATEWAY_URL=GATEWAY_URL, T6_GATEWAY_SECRET="g", T6_REVIEW_CHAT="-100")
        for kind in (*SYNC_KINDS, "operator.deliver", "notion.page_changed", "frameio.review_done"):
            self.assertIn(kind, handlers, kind)
        jobs: dict[str, dict[str, JsonValue]] = {
            "registry.sync": {"project_id": "t6p_none", "revision": 1},
            "deliverable.sync": {"intent_id": 1},
            "jo.sync": {"intent_id": 1},
            "project.status_sync": {"intent_id": 1},
            "operator.deliver": {"token": "none", "version": 1},
            "notion.page_changed": {"page_id": "p1", "updated_properties": []},
        }
        for number, (kind, payload) in enumerate(jobs.items(), 1):
            job = WorkerJob(number, "main", kind, f"{kind}:1", payload, "token", 3)
            with contextlib.suppress(Exception):  # the answer does not matter, only where the call went
                with patch("t6.notion.deliverable_projection.projection_id", return_value="x"):
                    handlers[kind](job)
        self.assert_edge_saw_only_queue_calls()
        self.assertTrue(any(p.startswith("/registry/") for p in self.api.paths))
        self.assertTrue(any(p.startswith("/operator/") for p in self.api.paths))

    def test_a_queued_notion_state_flagged_job_is_drained_without_a_gateway_message(self) -> None:
        """Flagged Notion edits no longer ping Sixto (#548); a job queued before that still completes."""
        job = WorkerJob(1, "main", "notion.state_flagged", "e1:page:Status", {"page_id": "p", "property": "Status", "value": "Done", "editor": "Ada", "reason": "no"}, "token", 3)
        handlers = self.handlers(T6_GATEWAY_URL=GATEWAY_URL, T6_GATEWAY_SECRET="g", T6_REVIEW_CHAT="-100", T6_OPERATOR_IDS="123")
        self.assertIn("notion.state_flagged", handlers)  # the same workers that delivered it before still end the old jobs
        handlers["notion.state_flagged"](job)
        self.assertEqual(self.gateway_notify.sent, {})

    def test_an_unbound_notion_page_is_not_offered_to_the_operator_chat(self) -> None:
        page = self.page(name="Some Show")
        self.edge_queue.submit("main", "notion.page_changed", "notion:event:e1",
                               {"page_id": page, "updated_properties": [], "event_id": "e1", "event_type": "page.created"})
        self.assertEqual(self.drain_main(T6_GATEWAY_URL=GATEWAY_URL, T6_GATEWAY_SECRET="g", T6_REVIEW_CHAT="-100", T6_OPERATOR_IDS="123"), 1)
        (item,) = self.items()
        self.assertEqual(item["kind"], "unbound_page")  # still recorded for the review list
        self.assertEqual([p for p in self.api.paths if p.startswith("/operator/")], [])
        self.assertEqual(self.gateway_notify.sent, {})

    def test_chat_reply_has_a_handler_only_with_the_gateway_and_sends_to_the_topic(self) -> None:
        self.assertNotIn("chat.reply", self.handlers())
        handlers = self.handlers(T6_GATEWAY_URL=GATEWAY_URL, T6_GATEWAY_SECRET="g", T6_REVIEW_CHAT="-100")
        sent: list[Any] = []
        from t6.worker.operator import ChatReply
        class Transport:
            def send(self, method: str, path: str, headers: Any, body: bytes) -> tuple[int, dict[str, Any]]:
                sent.append((method, path, json.loads(body)))
                return 202, {"message_ref": 5}
        ChatReply(GatewayClient(Transport(), "g")).deliver(WorkerJob(1, "main", "chat.reply", "chat.reply:k", {"chat": -100, "topic": 12, "text": "Usage: /link CODE", "key": "k"}, "token", 3))
        self.assertEqual([(m, p, b["to"], b["topic"], b["text"], b["idempotency_key"]) for m, p, b in sent], [("POST", "/notify", -100, 12, "Usage: /link CODE", "cmd-reply:k")])
        self.assertIn("chat.reply", handlers)

    def test_the_review_message_gets_its_version_and_chat_links_from_the_api(self) -> None:
        registry = self.registry
        registered = registry.register(actor="test", idempotency_key="register", notion_page_id="page-1", code="SHOW")
        assert isinstance(registered, Registered)
        project = registered.project
        from t6.media import MediaStore

        store = MediaStore(registry, PreviewTokens("secret"))
        values = {"owner": project.id, "kind": "frameio-project", "ref": "fp1", "role": "frameio-project"}
        preview = store.write("link", values, actor="person", idempotency_key="link-fp1")
        store.write("link", values, actor="person", idempotency_key="link-fp1", confirm=preview.token)  # type: ignore[union-attr]
        with registry._transaction() as db:
            db.execute("INSERT INTO chat_links VALUES (?, ?, ?, ?, ?)", ("link", project.id, -200, 9, "active"))
            db.execute("INSERT INTO deliverables VALUES ('t6v_1', ?, 'EP01', NULL, NULL, NULL)", (project.id,))
            db.execute("INSERT INTO versions VALUES ('t6r_1', 't6v_1', 'f1', 'stack', NULL, 'rough cut')")
        handlers = self.handlers(T6_GATEWAY_URL=GATEWAY_URL, T6_GATEWAY_SECRET="g", T6_REVIEW_CHAT="-100")
        payload: dict[str, JsonValue] = {"count": 1, "first_at": 1.0, "last_at": 2.0, "file_id": "f1", "frameio_project_id": "fp1", "user_name": "Ana"}
        handlers["frameio.review_done"](WorkerJob(1, "main", "frameio.review_done", "k", payload, "t", 3))
        (message,) = self.gateway_notify.sent.values()
        self.assertEqual((message["to"], message.get("topic")), (-200, 9))
        self.assertIn("Version: EP01, rough cut, t6r_1", message["text"])
        self.assert_edge_saw_only_queue_calls()


class InsideJobsTests(Reach, SyncTestCase):
    """The API submits the `main` jobs it starts to the internal job table."""

    def start(self, **registry_options: Any) -> None:
        self.registry = SqliteProjectRegistry(self.path, self.clock, **registry_options)
        self.inside = InternalJobQueue(self.registry, self.clock)
        self.app = WebhookApp(self.queue, self.config, self.clock, registry=self.registry, inside=self.inside)

    def setUp(self) -> None:
        super().setUp()
        self.build_edge()

    def waiting(self) -> list[tuple[str, str]]:
        with self.registry._transaction() as db:
            return [(r[0], r[1]) for r in db.execute("SELECT kind, dedupe_key FROM internal_jobs WHERE state = 'queued' AND kind != 'privacy_registry.rebuild' ORDER BY id")]  # the rebuild job has its own tests

    def test_a_registry_write_leaves_a_registry_sync_job_in_the_internal_table_and_the_worker_syncs_it(self) -> None:
        page = self.page()
        project = self.new("ABC", "in", page=page)
        self.assertEqual(self.read(project)["sync"], "pending")
        (intent,) = self.registry.pending_sync()
        self.assertEqual(self.waiting(), [("registry.sync", f"registry.sync:{intent.id}")])
        self.assertIsNone(self.queue.claim("main", 60))  # nothing in the API's queue file
        self.assertEqual(self.drain_main(), 1)
        self.assertEqual(self.shown(page), (project["id"], "ABC", "in"))
        self.assertEqual(self.read(project)["sync"], "synced")
        self.assert_edge_saw_only_queue_calls()

    def test_project_status_sync_is_claimed_from_the_internal_table(self) -> None:
        page = self.page()
        project = self.new("ABC", "in", page=page)
        self.drain_main()
        self.notion.add_option(PROJECTS, "Status", "Standby")
        writes = ProjectWrites(self.registry, PreviewTokens("secret"))
        preview = writes.run("hold", project["id"], "paused", actor="operator", idempotency_key="hold")
        assert isinstance(preview, Preview)
        self.assertIsInstance(
            writes.run("hold", project["id"], "paused", actor="operator", idempotency_key="hold", confirm=preview.token), Done
        )
        self.start()  # a restart dispatches what was committed
        self.assertIn("project.status_sync", [kind for kind, _ in self.waiting()])
        self.assertIsNone(self.queue.claim("main", 60))
        self.drain_main()
        self.assertEqual(codec.select(self.notion.page(page)["properties"].get("Status")), "Standby")

    def test_deliverable_sync_is_claimed_from_the_internal_table(self) -> None:
        project = self.new("SHOW", page=self.page())
        self.drain_main()
        self.notion.add_snapshot_data_source("deliverables", "deliverables")
        self.notion.add_property("deliverables", "T6 ID", "rich_text")
        property_id = self.notion._data_sources["deliverables"]["properties"]["T6 ID"]["id"]
        store = DeliverableStore(self.registry, PreviewTokens("secret"))
        values = {"template": "film", "project": project["id"], "code": "Ep01", "episode": None, "due": "2026-12-01", "air": None}
        preview = store.define(values, actor="alice", idempotency_key="define")
        assert isinstance(preview, Preview)
        assert isinstance(store.define(values, actor="alice", idempotency_key="define", confirm=preview.token), Done)
        self.start()
        self.assertIn("deliverable.sync", [kind for kind, _ in self.waiting()])
        self.assertIsNone(self.queue.claim("main", 60))
        with patch("t6.notion.deliverable_projection.projection_id", return_value=property_id):
            self.drain_main()
        (row,) = self.client.query_data_source("deliverables")
        self.assertEqual(DeliverablesRow.from_page(row).name, "Ep01")

    def test_jo_sync_is_claimed_from_the_internal_table(self) -> None:
        page = self.page()
        self.new("SHOW", page=page)
        self.drain_main()
        self.notion.add_snapshot_data_source("job-orders", "job_orders")
        self.notion.add_property("job-orders", "T6 ID", "rich_text")
        self.notion.add_property("job-orders", "T6 Notes", "rich_text")
        store = JobOrderStore(self.registry, PreviewTokens("secret"))
        values = {"fields": {"project_id": page, "name": "JO"}}
        preview = store.write("raise", values, actor="alice", idempotency_key="raise")
        assert isinstance(preview, Preview)
        assert isinstance(store.write("raise", values, actor="alice", idempotency_key="raise", confirm=preview.token), Done)
        self.start()
        self.assertIn("jo.sync", [kind for kind, _ in self.waiting()])
        self.assertIsNone(self.queue.claim("main", 60))
        self.drain_main()
        self.assertEqual(len(self.client.query_data_source("job-orders")), 1)

    def test_a_registry_write_through_a_verb_queues_its_sync_job_before_the_request_answers(self) -> None:
        from t6.person import People
        from t6.server.verbs import VerbAPI
        project = self.new("ABC", "in", page=self.page())
        self.drain_main()
        person = "t6n_" + "a" * 32
        with self.registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES (?, 'staff', '[]', '[]', 1, 0, 1)", (person,))
            db.execute("INSERT INTO person_permissions VALUES (?, 'operator')", (person,))
        api = VerbAPI(People(self.registry, PreviewTokens("test")), self.queue, {"person": person}, lambda _: None, self.app)
        headers = {"Authorization": "Bearer person"}
        values = {"project": project["id"], "reason": "paused", "idempotency_key": "hold"}
        with patch.dict("os.environ", {"T6_PREVIEW_SIGNING_KEY": "test"}):
            preview = api(Request("POST", "/v1/project/hold", headers, json.dumps(values).encode()))
            self.assertEqual(preview.body["outcome"], "preview", preview.body)
            done = api(Request("POST", "/v1/project/hold", headers, json.dumps({**values, "confirm": preview.body["token"]}).encode()))
        self.assertEqual(done.body["outcome"], "done", done.body)
        # no restart and no other call: the job is in the table when the request has answered
        self.assertIn("project.status_sync", [kind for kind, _ in self.waiting()])

    def verb_api(self) -> Any:
        """The /v1 API on this registry and app, for an operator and coordinator Person; the server is never restarted."""
        from t6.person import People
        from t6.server.verbs import VerbAPI
        person = "t6n_" + "a" * 32
        with self.registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES (?, 'staff', '[]', '[]', 1, 0, 1)", (person,))
            for permission in ("operator", "coordinator"):
                db.execute("INSERT INTO person_permissions VALUES (?, ?)", (person, permission))
        return VerbAPI(People(self.registry, PreviewTokens("test")), self.queue, {"person": person}, lambda _: None, self.app)

    def confirmed(self, api: Any, path: str, values: dict[str, Any]) -> Any:
        headers = {"Authorization": "Bearer person"}
        with patch.dict("os.environ", {"T6_PREVIEW_SIGNING_KEY": "test"}):
            preview = api(Request("POST", path, headers, json.dumps(values).encode()))
            self.assertEqual(preview.body["outcome"], "preview", preview.body)
            done = api(Request("POST", path, headers, json.dumps({**values, "confirm": preview.body["token"]}).encode()))
        self.assertEqual(done.body["outcome"], "done", done.body)
        return done.body

    def test_a_deliverable_define_through_a_verb_queues_its_sync_job_before_the_request_answers(self) -> None:
        project = self.new("SHOW", page=self.page())
        self.drain_main()
        values = {"template": "film", "project": project["id"], "code": "Ep01", "due": "2026-12-01", "idempotency_key": "define"}
        self.confirmed(self.verb_api(), "/v1/deliverable/define", values)
        self.assertIn("deliverable.sync", [kind for kind, _ in self.waiting()])  # no restart and no other call

    def test_a_job_order_write_through_a_verb_queues_its_sync_job_before_the_request_answers(self) -> None:
        page = self.page()
        self.new("SHOW", page=page)
        self.drain_main()
        store = JobOrderStore(self.registry, PreviewTokens("secret"))
        values = {"fields": {"project_id": page, "name": "JO"}}
        preview = store.write("raise", values, actor="alice", idempotency_key="raise")
        assert isinstance(preview, Preview)
        raised = store.write("raise", values, actor="alice", idempotency_key="raise", confirm=preview.token)
        assert isinstance(raised, Done)
        self.start()  # the raise is queued by the restart; what follows must not need one
        before = [kind for kind, _ in self.waiting()].count("jo.sync")
        self.confirmed(self.verb_api(), "/v1/jo/note", {"jo": raised.result["jo_id"], "text": "a note", "idempotency_key": "note"})
        self.assertEqual([kind for kind, _ in self.waiting()].count("jo.sync"), before + 1)

    def test_a_reconcile_request_becomes_an_internal_job(self) -> None:
        self.new("ABC", page=self.page())
        self.drain_main()
        answer = self.send("POST", "/registry/pages/p1/reconcile", {}, key="again")
        self.assertEqual(answer.status, 200, answer.body)
        self.assertEqual(self.waiting(), [("notion.page_changed", "registry.reconcile:again")])
        self.assertIsNone(self.queue.claim("main", 60))

    def test_dispatching_twice_queues_one_job_per_intent(self) -> None:
        self.new("ABC", page=self.page())
        self.assertEqual([kind for kind, _ in self.waiting()], ["registry.sync"])
        self.start()
        self.assertEqual(len(self.waiting()), 1)


class InsideOperatorTests(OperatorTestCase):
    """Operator cards and taps the API starts are claimed by the main worker."""

    def start(self, **registry_options: Any) -> None:
        self.registry = SqliteProjectRegistry(self.path, self.clock, **registry_options)
        self.inside = InternalJobQueue(self.registry, self.clock)
        self.store = ActionStore(str(self.path.with_name("operator.sqlite3")), self.clock)
        ids = self.config.operator_ids  # the base setUp builds the app once before this class's config exists
        service = OperatorService(self.registry, self.store, ids, clock=self.clock) if ids else None
        self.app = WebhookApp(self.queue, self.config, self.clock, registry=self.registry, operator=service, inside=self.inside)

    def worker(self, gateway: FakeGateway | None = None) -> Worker:
        transport = InProcess(self.app)
        delivery = CardDelivery(OperatorClient(transport, WORKER_SECRET), GatewayClient(gateway or self.gateway, GATEWAY_SECRET))
        internal = InternalQueueClient(InternalJobQueue(self.path, self.clock))
        return Worker(QueueClient(transport, WORKER_SECRET), "main", delivery.handlers(), internal_client=internal)

    def test_operator_deliver_is_claimed_by_the_main_worker(self) -> None:
        self.offer(page_id=PAGE_A)
        self.assertIsNone(self.queue.claim("main", 60))
        self.assertEqual(self.drain(), 1)
        self.assertEqual(self.gateway.sends, 1)

    def test_a_tap_job_is_claimed_by_the_main_worker(self) -> None:
        project = self.new("ABC", "in", page=PAGE_A)
        body = {"kind": "tap", "data": f"approve:{PAGE_A}", "from": {"id": OPERATOR}, "chat": 1, "message_id": 2}
        headers = {"Authorization": f"Bearer {TAP_SECRET}", "Idempotency-Key": "k"}
        self.assertEqual(self.app.tailnet(Request("POST", "/tap", headers, json.dumps(body).encode())).status, 200)
        self.assertIsNone(self.queue.claim("main", 60))
        seen: list[WorkerJob] = []
        worker = Worker(
            QueueClient(InProcess(self.app), WORKER_SECRET), "main", {"tap": lambda job: seen.append(job)},
            internal_client=InternalQueueClient(InternalJobQueue(self.path, self.clock)),
        )
        while worker.run_once():  # the Project's own registry.sync job has no handler here and fails
            pass
        (tap,) = seen
        self.assertEqual(tap.payload["project_id"], project["id"])


class HandoverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.clock = FakeClock()
        self.queue = JobQueue(Path(self.directory.name) / "q.sqlite3", self.clock)
        self.registry = SqliteProjectRegistry(Path(self.directory.name) / "t6.sqlite3", self.clock)
        self.inside = InternalJobQueue(self.registry, self.clock)
        self.log: list[str] = []

    def hand_over(self) -> dict[str, int]:
        from t6.server.handover import hand_over_inside_jobs

        return hand_over_inside_jobs(self.queue, self.inside, self.log.append)

    def internal_count(self) -> int:
        with self.registry._transaction() as db:
            return int(db.execute("SELECT COUNT(*) FROM internal_jobs").fetchone()[0])

    def test_waiting_sync_jobs_move_with_their_kind_dedupe_key_and_payload(self) -> None:
        for kind in SYNC_KINDS:
            self.queue.submit("main", kind, f"{kind}:7", {"intent_id": 7})
        moved = self.hand_over()
        self.assertEqual(moved, {kind: 1 for kind in SYNC_KINDS})
        self.assertIsNone(self.queue.claim("main", 60))
        claimed = []
        while (claim := self.inside.claim("main", 60)) is not None:
            claimed.append((claim.kind, claim.dedupe_key, dict(claim.payload)))
        self.assertEqual(sorted(claimed), sorted((kind, f"{kind}:7", {"intent_id": 7}) for kind in SYNC_KINDS))

    def test_it_is_safe_to_run_twice(self) -> None:
        self.queue.submit("main", "registry.sync", "registry.sync:1", {"project_id": "t6p_1", "revision": 1})
        self.hand_over()
        self.assertEqual(self.hand_over(), {})
        self.assertEqual(self.internal_count(), 1)

    def test_a_job_the_internal_table_already_has_is_not_doubled_and_leaves_the_file(self) -> None:
        self.inside.submit("main", "jo.sync", "jo.sync:3", {"intent_id": 3})
        self.queue.submit("main", "jo.sync", "jo.sync:3", {"intent_id": 3})
        self.hand_over()
        self.assertIsNone(self.queue.claim("main", 60))
        self.assertEqual(self.internal_count(), 1)

    def test_other_waiting_kinds_stay_in_the_file_and_are_counted_in_the_log_by_kind(self) -> None:
        self.queue.submit("main", "operator.deliver", "d1", {"token": "a", "version": 1})
        self.queue.submit("main", "operator.deliver", "d2", {"token": "b", "version": 1})
        self.queue.submit("main", "tap", "t1", {"data": "x"})
        self.queue.submit("frameio", "jo.sync", "other-queue", {})  # not a main job
        self.assertEqual(self.hand_over(), {})
        text = "\n".join(self.log)
        self.assertIn("operator.deliver: 2", text)
        self.assertIn("tap: 1", text)
        claimed = [self.queue.claim("main", 60), self.queue.claim("main", 60), self.queue.claim("main", 60)]
        self.assertEqual(sorted(c.kind for c in claimed if c), ["operator.deliver", "operator.deliver", "tap"])
        self.assertIsNotNone(self.queue.claim("frameio", 60))
        self.assertEqual(self.internal_count(), 0)

    def test_jobs_already_finished_are_left_alone(self) -> None:
        self.queue.submit("main", "registry.sync", "registry.sync:1", {})
        claim = self.queue.claim("main", 60)
        assert claim is not None
        self.queue.ack(claim.job_id, claim.token)
        self.assertEqual(self.hand_over(), {})
        self.assertEqual(self.internal_count(), 0)


class ServerStartTests(unittest.TestCase):
    def test_the_full_server_moves_waiting_sync_jobs_and_starts_its_own_jobs_in_the_internal_table(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            queue_path, registry_path = Path(directory) / "q.sqlite3", Path(directory) / "t6.sqlite3"
            old = JobQueue(queue_path)
            old.submit("main", "registry.sync", "registry.sync:41", {"project_id": "t6p_old", "revision": 1})
            old.submit("main", "operator.deliver", "operator.deliver:z:1", {"token": "z", "version": 1})
            registry = SqliteProjectRegistry(registry_path)
            registry.register(actor="test", idempotency_key="k", notion_page_id="page-1", code="ABC")  # an undispatched intent
            env = {"T6_SERVER_MODE": "full", "T6_QUEUE_DB": str(queue_path), "T6_REGISTRY_DB": str(registry_path)}
            from t6.server.__main__ import main as server_main

            stderr = io.StringIO()
            with patch.dict(os.environ, env, clear=True), patch("t6.server.__main__.serve"), patch(
                "t6.server.__main__.make_server"
            ), patch("t6.server.__main__.threading.Thread"), contextlib.redirect_stderr(stderr):
                self.assertEqual(server_main(), 0)
            inside = InternalJobQueue(registry_path)
            kinds = []
            while (claim := inside.claim("main", 60)) is not None:
                kinds.append((claim.kind, claim.dedupe_key))
            self.assertIn(("registry.sync", "registry.sync:41"), kinds)
            self.assertEqual(sum(1 for kind, _ in kinds if kind == "registry.sync"), 2)  # the old one and the new intent
            left = JobQueue(queue_path).claim("main", 60)
            assert left is not None
            self.assertEqual(left.kind, "operator.deliver")
            self.assertIn("operator.deliver: 1", stderr.getvalue())


class WorkerStartTests(unittest.TestCase):
    ENVIRON = {"T6_SERVER_URL": EDGE_URL, "T6_WORKER_SECRET": WORKER_SECRET, "T6_WORKER_QUEUE": "main"}

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.transport = InProcess(WebhookApp(JobQueue(Path(self.directory.name) / "q.sqlite3"), ServerConfig()))

    def test_the_main_worker_with_a_database_and_no_api_address_refuses_to_start(self) -> None:
        environ = {**self.ENVIRON, "T6_DATABASE": str(Path(self.directory.name) / "t6.sqlite3")}
        with self.assertRaisesRegex(ConfigError, "T6_API_URL"):
            configured_handlers(environ, WorkerSettings.from_env(environ), self.transport)
        stop = unittest.mock.MagicMock()
        stop.is_set.return_value = True
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            code = main(environ, None, stop=stop)
        self.assertEqual(code, 2)
        self.assertIn("T6_API_URL", errors.getvalue())

    def test_a_worker_without_a_database_keeps_using_the_queue_server_for_the_registry(self) -> None:
        # One-server deployments (no edge) have no T6_DATABASE; their registry is the queue server.
        environ = {**self.ENVIRON, "T6_NOTION_TOKEN": "x", "T6_NOTION_PROJECTS_DATA_SOURCE": PROJECTS}
        handlers = configured_handlers(environ, WorkerSettings.from_env(environ), self.transport)
        self.assertIn("registry.sync", handlers["main"])


if __name__ == "__main__":
    unittest.main()
