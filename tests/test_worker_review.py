"""Per-user Frame.io review message: webhook, Frame.io worker, review timer, main worker, /notify.

Fake clock, fake Frame.io V4, fake gateway.
"""

from collections.abc import Mapping
from types import SimpleNamespace
from pathlib import Path
import json
from typing import Any

from t6.frameio import FrameioRequest, FrameioResult, FrameioWorker, ask
from t6.frameio.api import FrameioApi
from t6.frameio.reads import FrameioReads
from t6.frameio.testing import FakeFrameio, StaticTokens
from t6.worker import QueueClient, Worker
from t6.worker.__main__ import ConfigError, WorkerSettings, configured_handlers
from t6.worker.gateway import GatewayClient
from t6.worker.review import ReviewMessage
from t6.worker.review_timer import ReviewTimerClient
from t6.worker.review_target import ReviewTarget
from t6.worker.registry import RegistryClient
from t6.registry import Registered
from t6.registry.sqlite import SqliteProjectRegistry
from t6.registry import Registered
from t6.server.app import Grant, ServerConfig, WebhookApp
from test_frameio_worker import FakeTokens
from test_server_app import (
    FRAMEIO_WORKER_SECRET,
    WORKER_SECRET,
    InProcess,
    ServerTestCase,
    comment,
    frameio_request,
)

MINUTE = 60.0
CHAT = -1003261335368


class FakeNotify:
    """Stands in for the gateway's /notify: one message per idempotency key."""

    def __init__(self) -> None:
        self.sent: dict[str, dict[str, Any]] = {}

    def send(self, method: str, path: str, headers: Mapping[str, str], body: bytes) -> tuple[int, dict[str, Any]]:
        message = json.loads(body)
        self.sent.setdefault(message["idempotency_key"], message)
        return 202, {"message_ref": len(self.sent)}

    def texts(self) -> list[str]:
        return [message["text"] for message in self.sent.values()]


class ReviewMessageTests(ServerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.fake = FakeFrameio()
        self.fake.add_project("fp1", "Example Show")
        self.fake.add_file("f1", "Cut 1")
        self.fake.add_share("fp1", "s1", "Cut 1 share", file_ids=("f1",), created_at="2026-11-01T00:00:00Z")
        self.gateway = FakeNotify()
        self.reads = FrameioReads(FrameioApi(StaticTokens(), self.fake.fetch))
        reads = self.reads
        timers = ReviewTimerClient(InProcess(self.app), FRAMEIO_WORKER_SECRET)
        frameio = FrameioWorker(FakeTokens(), reads, lambda text, key: None, self.clock, arm_review_timer=timers.arm)
        self.frameio_worker = Worker(
            QueueClient(InProcess(self.app), FRAMEIO_WORKER_SECRET), "frameio", frameio.handlers()
        )
        self.queue_client = QueueClient(InProcess(self.app), WORKER_SECRET)
        self.review = ReviewMessage(GatewayClient(self.gateway, "secret"), CHAT, None, self.ask)
        self.worker = Worker(self.queue_client, "main", self.review.handlers())

    def ask(self, request: FrameioRequest) -> FrameioResult:
        def serve(seconds: float) -> None:
            self.frameio_worker.run_once()

        return ask(self.queue_client, request, timeout=3, clock=iter(range(100)).__next__, sleep=serve)

    def comment(self, comment_id: str, author: str, author_id: str | None, at: str, file_id: str = "f1") -> None:
        """Frame.io has the comment; its webhook arrives and the Frame.io worker handles it."""
        self.fake.add_comment(file_id, comment_id, "text", author, author_id=author_id, created_at=at)
        self.webhook(comment_id)

    def webhook(self, comment_id: str) -> None:
        self.assertEqual(self.app.public(frameio_request(self.clock, comment(comment_id))).status, 200)
        while self.frameio_worker.run_once():
            pass

    def run_worker(self) -> None:
        while self.worker.run_once():
            pass

    def test_two_comments_from_one_person_and_one_from_another_send_two_messages(self) -> None:
        self.comment("c1", "Ana Reyes", "u-a", "2026-11-12T10:00:00Z")
        self.clock.advance(5 * MINUTE)
        self.comment("c2", "Ana Reyes", "u-a", "2026-11-12T10:05:00Z")
        self.comment("c3", "Ben Cruz", "u-b", "2026-11-12T10:06:00Z")
        self.clock.advance(15 * MINUTE - 1)  # A's last comment was 5 minutes in
        self.run_worker()
        self.assertEqual(self.gateway.texts(), [])
        self.clock.advance(1)
        self.run_worker()
        ana, ben = self.gateway.texts()
        self.assertIn("<b>Ana Reyes</b>", ana)
        self.assertIn("Started: 12 Nov 2026, 18:00 (Manila)", ana)  # 10:00 UTC
        self.assertIn("Stopped: 12 Nov 2026, 18:05 (Manila)", ana)
        self.assertIn("2 comments", ana)
        self.assertIn("<b>Ben Cruz</b>", ben)
        self.assertIn("1 comment ", ben)

    def test_guest_comments_on_one_file_send_one_message_from_a_guest(self) -> None:
        self.comment("c1", "", None, "2026-11-12T10:00:00Z")
        self.clock.advance(5 * MINUTE)
        self.comment("c2", "", None, "2026-11-12T10:05:00Z")
        self.clock.advance(15 * MINUTE - 1)
        self.run_worker()
        self.assertEqual(self.gateway.texts(), [])
        self.clock.advance(1)
        self.run_worker()
        (guest,) = self.gateway.texts()
        self.assertIn("<b>A guest</b>", guest)
        self.assertIn("Started: 12 Nov 2026, 18:00 (Manila)", guest)
        self.assertIn("Stopped: 12 Nov 2026, 18:05 (Manila)", guest)
        self.assertIn("2 comments", guest)

    def test_a_guest_and_a_person_on_the_same_file_have_separate_messages(self) -> None:
        self.comment("c1", "Ana Reyes", "u-a", "2026-11-12T10:00:00Z")
        self.comment("c2", "", None, "2026-11-12T10:01:00Z")
        self.clock.advance(15 * MINUTE)
        self.run_worker()
        ana, guest = self.gateway.texts()
        self.assertIn("<b>Ana Reyes</b>", ana)
        self.assertIn("1 comment ", ana)
        self.assertIn("<b>A guest</b>", guest)
        self.assertIn("1 comment ", guest)

    def comment_at_frame(self, comment_id: str, author_id: str, frame: int, at: str = "2026-11-12T10:00:00Z") -> None:
        self.fake.add_comment("f1", comment_id, "text", "Ana Reyes", author_id=author_id, created_at=at, timestamp=frame)
        self.webhook(comment_id)

    def test_the_wait_is_the_rest_of_the_clip_plus_eight_minutes(self) -> None:
        self.fake.set_clip("f1", 166.792, 23.976)
        self.comment_at_frame("c1", "u-a", 2108)  # 1:27.9 in: 78.87 s left, so 558.87 s
        self.clock.advance(558)
        self.run_worker()
        self.assertEqual(self.gateway.texts(), [])
        self.clock.advance(1)
        self.run_worker()
        (ana,) = self.gateway.texts()
        self.assertIn("<b>Ana Reyes</b>", ana)

    def test_a_comment_earlier_in_the_clip_does_not_bring_the_message_forward(self) -> None:
        self.fake.set_clip("f1", 600.0, 25.0)
        self.comment_at_frame("c1", "u-a", 25 * 60)  # 1:00 in: 540 s left, so 1020 s
        self.clock.advance(60)
        self.comment_at_frame("c2", "u-a", 25 * 500, at="2026-11-12T10:01:00Z")  # 8:20 in: 100 s left, so 580 s
        self.clock.advance(1019 - 60)
        self.run_worker()
        self.assertEqual(self.gateway.texts(), [])
        self.clock.advance(1)
        self.run_worker()
        (ana,) = self.gateway.texts()
        self.assertIn("2 comments", ana)

    def test_without_the_clip_length_the_fixed_fifteen_minutes_apply(self) -> None:
        self.comment_at_frame("c1", "u-a", 2108)  # no Duration or Frame Rate on the file
        self.clock.advance(15 * MINUTE - 1)
        self.run_worker()
        self.assertEqual(self.gateway.texts(), [])
        self.clock.advance(1)
        self.run_worker()
        self.assertEqual(len(self.gateway.texts()), 1)

    def sent_share_line(self) -> str:
        self.comment("c1", "Ana Reyes", "u-a", "2026-11-12T10:00:00Z")
        self.clock.advance(15 * MINUTE)
        self.run_worker()
        (text,) = self.gateway.texts()
        return next(line for line in text.splitlines() if line.startswith("Share link:"))

    def test_a_share_of_the_files_version_stack_gives_its_link(self) -> None:
        self.fake.add_file("f1b", "Cut 1 v2")
        self.fake.add_version_stack("vs1", ["f1", "f1b"])  # f1 sits in the stack, so its parent is the stack
        self.fake.shares["fp1"].clear()
        self.fake.add_share("fp1", "s-stack", "Stack share", file_ids=("vs1",), created_at="2026-11-01T00:00:00Z")
        self.assertIn("https://f.io/s-stack", self.sent_share_line())

    def test_a_share_of_the_files_folder_gives_its_link(self) -> None:
        self.fake.shares["fp1"].clear()
        self.fake.add_share("fp1", "s-folder", "Folder share", file_ids=("folder-1",), created_at="2026-11-01T00:00:00Z")
        self.assertIn("https://f.io/s-folder", self.sent_share_line())

    def test_no_live_share_holds_the_file_after_all_were_checked(self) -> None:
        self.fake.shares["fp1"].clear()
        self.fake.add_share("fp1", "s-other", "Other", file_ids=("f-other",), created_at="2026-11-01T00:00:00Z")
        self.assertEqual(self.sent_share_line(), "Share link: no share link")

    def test_shares_past_the_scan_limit_make_the_answer_unknown(self) -> None:
        self.reads._share_scan_limit = 1  # the test reads are built in setUp
        self.fake.shares["fp1"].clear()
        self.fake.add_share("fp1", "s-new", "New", file_ids=("f-other",), created_at="2026-11-09T00:00:00Z")
        self.fake.add_share("fp1", "s-old", "Old", file_ids=("f1",), created_at="2026-11-01T00:00:00Z")
        self.assertEqual(self.sent_share_line(), "Share link: unknown")

    def test_a_new_comment_before_the_quiet_time_restarts_only_that_persons_timer(self) -> None:
        self.comment("c1", "Ana Reyes", "u-a", "2026-11-12T10:00:00Z")
        self.comment("c2", "Ben Cruz", "u-b", "2026-11-12T10:00:30Z")
        self.clock.advance(10 * MINUTE)
        self.comment("c3", "Ana Reyes", "u-a", "2026-11-12T10:10:00Z")
        self.clock.advance(5 * MINUTE)
        self.run_worker()
        (ben,) = self.gateway.texts()
        self.assertIn("Ben Cruz", ben)
        self.clock.advance(10 * MINUTE)
        self.run_worker()
        self.assertIn("2 comments", self.gateway.texts()[1])

    def test_comments_on_other_files_have_their_own_timers(self) -> None:
        self.fake.add_file("f2", "Cut 2")
        self.comment("c1", "Ana Reyes", "u-a", "2026-11-12T10:00:00Z")
        self.comment("c2", "Ana Reyes", "u-a", "2026-11-12T10:01:00Z", file_id="f2")
        self.clock.advance(15 * MINUTE)
        self.run_worker()
        self.assertEqual(len(self.gateway.texts()), 2)

    def test_a_redelivered_webhook_changes_nothing(self) -> None:
        self.comment("c1", "Ana Reyes", "u-a", "2026-11-12T10:00:00Z")
        self.webhook("c1")
        self.clock.advance(15 * MINUTE)
        self.run_worker()
        self.webhook("c1")
        self.clock.advance(15 * MINUTE)
        self.run_worker()
        (text,) = self.gateway.texts()
        self.assertIn("1 comment ", text)

    def test_a_job_that_runs_twice_sends_one_message(self) -> None:
        self.comment("c1", "Ana Reyes", "u-a", "2026-11-12T10:00:00Z")
        self.clock.advance(15 * MINUTE)
        claimed = self.queue_client.claim("main")  # a worker that died before finishing
        assert claimed is not None
        self.clock.advance(2 * MINUTE)
        self.run_worker()
        self.run_worker()
        self.assertEqual(len(self.gateway.texts()), 1)

    def test_the_message_goes_to_the_review_chat_and_to_its_topic_only_when_one_is_set(self) -> None:
        self.comment("c1", "Ana Reyes", "u-a", "2026-11-12T10:00:00Z")
        self.clock.advance(15 * MINUTE)
        self.run_worker()
        (message,) = self.gateway.sent.values()
        self.assertEqual(message["to"], CHAT)
        self.assertNotIn("topic", message)  # General: Telegram rejects message_thread_id 1
        self.assertEqual(message["format"], "html")
        gateway = FakeNotify()
        in_topic = Worker(self.queue_client, "main", ReviewMessage(GatewayClient(gateway, "s"), CHAT, 7, self.ask).handlers())
        self.comment("c2", "Ben Cruz", "u-b", "2026-11-12T11:00:00Z")
        self.clock.advance(15 * MINUTE)
        while in_topic.run_once():
            pass
        (message,) = gateway.sent.values()
        self.assertEqual(message["topic"], 7)

    def test_the_message_names_the_version_of_a_known_file(self) -> None:
        registry = SqliteProjectRegistry(Path(self.directory.name) / "registry.sqlite3")
        registered = registry.register(actor="test", idempotency_key="register", notion_page_id="page-1", code="SHOW")
        assert isinstance(registered, Registered)
        with registry._transaction() as db:
            db.execute("INSERT INTO deliverables VALUES ('t6v_1', ?, 'EP01', NULL, NULL, NULL)", (registered.project.id,))
            db.execute("INSERT INTO versions VALUES ('t6r_1', 't6v_1', 'f1', 'stack', NULL, 'rough <cut>')")
        granted = WebhookApp(self.queue, ServerConfig(grants=(Grant(WORKER_SECRET, registry_read=True),)), self.clock, registry=registry)
        lookup = RegistryClient(InProcess(granted), WORKER_SECRET).review_version
        self.assertIsNone(lookup("other-file"))
        self.review = ReviewMessage(GatewayClient(self.gateway, "secret"), CHAT, None, self.ask, version=lookup)
        self.worker = Worker(self.queue_client, "main", self.review.handlers())
        self.comment("c1", "Ana", "u-a", "2026-11-12T10:00:00Z")
        self.clock.advance(15 * MINUTE)
        self.run_worker()
        self.assertIn("Version: EP01, rough &lt;cut&gt;, t6r_1\n", self.gateway.texts()[-1])
        with registry._transaction() as db:
            db.execute("INSERT INTO version_locks VALUES ('t6v_1', 't6r_1')")
        self.comment("c2", "Ben", "u-b", "2026-11-12T11:00:00Z")
        self.clock.advance(15 * MINUTE)
        self.run_worker()
        self.assertIn("t6r_1 (locked)\n", self.gateway.texts()[-1])
        ungranted = WebhookApp(self.queue, ServerConfig(grants=(Grant(WORKER_SECRET),)), self.clock, registry=registry)
        self.assertIsNone(RegistryClient(InProcess(ungranted), WORKER_SECRET).review_version("f1"))

    def test_a_failed_version_lookup_is_retried_then_sent_without_the_line(self) -> None:
        def down(file_id: str) -> dict[str, Any] | None:
            raise OSError("server down")
        review = ReviewMessage(GatewayClient(self.gateway, "secret"), CHAT, None, self.ask, version=down)
        self.assertEqual(review._version_line("f1", 3), "")
        with self.assertRaises(RuntimeError):
            review._version_line("f1", 1)

    def test_linked_topic_and_general_and_inactive_fallback(self) -> None:
        registry = SqliteProjectRegistry(Path(self.directory.name) / "registry.sqlite3")
        registered = registry.register(actor="test", idempotency_key="register", notion_page_id="page-1", code="SHOW")
        assert isinstance(registered, Registered)
        project = registered.project
        config = ServerConfig(grants=(Grant(WORKER_SECRET, registry_read=True),))
        app = WebhookApp(self.queue, config, self.clock, registry=registry)
        registry_client = RegistryClient(InProcess(app), WORKER_SECRET)
        from t6.media import MediaStore
        from t6.preview import PreviewTokens
        store = MediaStore(registry, PreviewTokens("secret"))
        values = {"owner": project.id, "kind": "frameio-project", "ref": "fp1", "role": "frameio-project"}
        preview = store.write("link", values, actor="person", idempotency_key="link-fp1")
        store.write("link", values, actor="person", idempotency_key="link-fp1", confirm=preview.token)  # type: ignore[union-attr]
        target = ReviewTarget(registry_client, registry_client)
        self.review = ReviewMessage(GatewayClient(self.gateway, "secret"), CHAT, None, self.ask, target=target)
        self.worker = Worker(self.queue_client, "main", self.review.handlers())
        with registry._transaction() as db:
            db.execute("INSERT INTO chat_links VALUES (?, ?, ?, ?, ?)", ("link", project.id, -200, 9, "active"))
        self.comment("c1", "Ana", "u-a", "2026-11-12T10:00:00Z")
        self.clock.advance(15 * MINUTE)
        self.run_worker()
        self.assertEqual([(m["to"], m.get("topic")) for m in self.gateway.sent.values()], [(-200, 9)])
        with registry._transaction() as db:
            db.execute("INSERT INTO chat_links VALUES (?, ?, ?, ?, ?)", ("named", project.id, -200, 12, "active"))
            db.execute("INSERT INTO linked_chat_topics VALUES (-200, 9, 'General talk'), (-200, 12, 'show - Edit')")
        self.assertEqual(target("fp1"), (-200, 12))
        with registry._transaction() as db:
            db.execute("DELETE FROM chat_links WHERE id = 'named'")
            db.execute("UPDATE chat_links SET topic_id = NULL")
        self.comment("c2", "Ben", "u-b", "2026-11-12T10:01:00Z")
        self.clock.advance(15 * MINUTE)
        self.run_worker()
        self.assertEqual([(m["to"], m.get("topic")) for m in self.gateway.sent.values()][-1], (-200, None))
        with registry._transaction() as db:
            db.execute("UPDATE chat_links SET state = 'inactive'")
        self.comment("c3", "Cam", "u-c", "2026-11-12T10:02:00Z")
        self.clock.advance(15 * MINUTE)
        self.run_worker()
        self.assertEqual([(m["to"], m.get("topic")) for m in self.gateway.sent.values()][-1], (CHAT, None))
        self.assertIsNone(target("unknown"))

    def test_media_locations_route_serves_a_reader_with_registry_read_and_nobody_else(self) -> None:
        registry = SqliteProjectRegistry(Path(self.directory.name) / "registry.sqlite3")
        registered = registry.register(actor="test", idempotency_key="register", notion_page_id="page-1", code="SHOW")
        assert isinstance(registered, Registered)
        from t6.media import MediaStore
        from t6.preview import PreviewTokens
        store = MediaStore(registry, PreviewTokens("secret"))
        for kind, ref, role in (("frameio-project", "fp1", "frameio-project"), ("resolve-project", "db/show", "working")):
            values = {"owner": registered.project.id, "kind": kind, "ref": ref, "role": role}
            preview = store.write("link", values, actor="person", idempotency_key="link-" + kind)
            store.write("link", values, actor="person", idempotency_key="link-" + kind, confirm=preview.token)  # type: ignore[union-attr]
        config = ServerConfig(grants=(Grant(WORKER_SECRET, registry_read=True), Grant("other-secret")))
        app = WebhookApp(self.queue, config, self.clock, registry=registry)
        links = RegistryClient(InProcess(app), WORKER_SECRET).media_links("frameio-project")
        self.assertEqual([(l.ref, l.role, l.project_id, l.pages) for l in links],
                         [("fp1", "frameio-project", registered.project.id, ("page-1",))])
        self.assertEqual(RegistryClient(InProcess(app), WORKER_SECRET).media_links("resolve-project", "backup"), ())
        with self.assertRaises(Exception):
            RegistryClient(InProcess(app), "other-secret").media_links("frameio-project")

    def test_the_full_mode_app_serves_media_locations_to_the_registry_read_secret_and_edge_mode_does_not(self) -> None:
        registry = SqliteProjectRegistry(Path(self.directory.name) / "registry.sqlite3")
        registered = registry.register(actor="test", idempotency_key="register", notion_page_id="page-1", code="SHOW")
        assert isinstance(registered, Registered)
        from t6.media import MediaStore
        from t6.preview import PreviewTokens
        store = MediaStore(registry, PreviewTokens("secret"))
        values = {"owner": registered.project.id, "kind": "frameio-project", "ref": "fp1", "role": "frameio-project"}
        preview = store.write("link", values, actor="person", idempotency_key="link-fp1")
        store.write("link", values, actor="person", idempotency_key="link-fp1", confirm=preview.token)  # type: ignore[union-attr]
        config = ServerConfig.from_env({"T6_WORKER_SECRET": WORKER_SECRET, "T6_FRAMEIO_WORKER_SECRET": FRAMEIO_WORKER_SECRET,
                                        "T6_REGISTRY_READ_SECRET": "registry-read-secret"})
        self.assertEqual([g.actor for g in config.grants if g.secret == "registry-read-secret"], ["registry-reader"])
        full = WebhookApp(self.queue, config, self.clock, registry=registry)
        links = RegistryClient(InProcess(full), "registry-read-secret").media_links("frameio-project")
        self.assertEqual([(l.ref, l.project_id, l.pages) for l in links], [("fp1", registered.project.id, ("page-1",))])
        with self.assertRaises(Exception):  # the Frame.io worker's own secret has no registry read
            RegistryClient(InProcess(full), FRAMEIO_WORKER_SECRET).media_links("frameio-project")
        edge = WebhookApp(self.queue, config, self.clock)
        self.assertEqual(InProcess(edge).send("GET", "/registry/media-links/frameio-project",
                                              {"Authorization": "Bearer registry-read-secret"}, b"")[0], 503)

    def test_a_404_from_the_media_locations_route_is_an_error_not_no_links(self) -> None:
        from t6.worker.registry import RegistryRejected
        config = ServerConfig(edge_routes=(), grants=(Grant(WORKER_SECRET, registry_read=True),))
        edge = WebhookApp(self.queue, config, self.clock)
        self.assertEqual(InProcess(edge).send("GET", "/registry/media-links/frameio-project",
                                              {"Authorization": f"Bearer {WORKER_SECRET}"}, b"")[0], 404)
        with self.assertRaises(RegistryRejected) as caught:
            RegistryClient(InProcess(edge), WORKER_SECRET).media_links("frameio-project")
        self.assertEqual(caught.exception.status, 404)
        with self.assertRaises(RegistryRejected):
            RegistryClient(InProcess(edge), WORKER_SECRET).media_links("frameio-project", "review")

    def test_the_main_worker_reads_media_locations_from_its_database_not_over_http(self) -> None:
        from t6.media import MediaStore
        from t6.preview import PreviewTokens
        from t6.worker.__main__ import review_media_links
        registry = SqliteProjectRegistry(Path(self.directory.name) / "registry.sqlite3")
        registered = registry.register(actor="test", idempotency_key="register", notion_page_id="page-1", code="SHOW")
        assert isinstance(registered, Registered)
        store = MediaStore(registry, PreviewTokens("secret"))
        values = {"owner": registered.project.id, "kind": "frameio-project", "ref": "fp1", "role": "frameio-project"}
        preview = store.write("link", values, actor="person", idempotency_key="link-fp1")
        store.write("link", values, actor="person", idempotency_key="link-fp1", confirm=preview.token)  # type: ignore[union-attr]
        # The queue server it polls is an edge server: no registry, so the HTTP route cannot answer.
        edge_client = RegistryClient(InProcess(WebhookApp(self.queue, ServerConfig(grants=(Grant(WORKER_SECRET, registry_read=True),)), self.clock)), WORKER_SECRET)
        with self.assertRaises(Exception):
            edge_client.media_links("frameio-project")

        class Chats:
            def review_chat_links(self, page_id: str) -> list[dict[str, Any]]:
                return [{"chat": -200, "topic": 9}] if page_id == "page-1" else []

        links = review_media_links({"T6_DATABASE": str(Path(self.directory.name) / "registry.sqlite3")}, edge_client)
        self.assertEqual(ReviewTarget(links, Chats())("fp1"), (-200, 9))
        self.assertIsNone(ReviewTarget(links, Chats())("unknown"))
        self.assertIs(review_media_links({}, edge_client), edge_client)

    def test_names_are_escaped_for_telegram_html(self) -> None:
        self.comment("c1", "<b>Tom & Jerry", "u-a", "2026-11-12T10:00:00Z")
        self.clock.advance(15 * MINUTE)
        self.run_worker()
        (text,) = self.gateway.texts()
        self.assertIn("<b>&lt;b&gt;Tom &amp; Jerry</b>", text)

    def test_an_author_with_no_name_is_called_someone(self) -> None:
        self.fake.add_comment("f1", "c1", "text", author_id="u-a", created_at="2026-11-12T10:00:00Z")
        self.fake.comments["f1"][0]["owner"].pop("name")
        self.webhook("c1")
        self.clock.advance(15 * MINUTE)
        self.run_worker()
        self.assertIn("<b>Someone</b>", self.gateway.texts()[0])

    def test_when_frameio_cannot_be_read_the_comment_is_tried_again_not_lost(self) -> None:
        self.fake.add_comment("f1", "c1", "text", "Ana Reyes", author_id="u-a", created_at="2026-11-12T10:00:00Z")
        self.fake.refuse_next(500)
        self.webhook("c1")
        self.clock.advance(15 * MINUTE)
        self.run_worker()
        self.assertEqual(self.gateway.texts(), [])  # nothing counted yet
        while self.frameio_worker.run_once():  # the retry comes due
            pass
        self.clock.advance(30)
        while self.frameio_worker.run_once():
            pass
        self.clock.advance(15 * MINUTE)
        self.run_worker()
        self.assertEqual(len(self.gateway.texts()), 1)

    def test_a_comment_frameio_does_not_have_counts_nothing(self) -> None:
        self.webhook("c-missing")  # Frame.io answers 404; the job waits to be retried
        self.assertIsNone(self.queue.claim("frameio", 60))
        self.clock.advance(15 * MINUTE)
        self.run_worker()
        self.assertEqual(self.gateway.texts(), [])

    def one_message(self) -> str:
        self.clock.advance(15 * MINUTE)
        self.run_worker()
        (text,) = self.gateway.texts()
        return text

    def test_the_message_has_the_title_the_asset_link_and_the_total_from_everyone(self) -> None:
        self.comment("c1", "Ana Reyes", "u-a", "2026-11-12T10:00:00Z")
        self.fake.add_comment("f1", "c9", "text", "Ben Cruz", author_id="u-b")
        text = self.one_message()
        self.assertIn("finished commenting on <b>Cut 1</b>", text)
        self.assertIn('<a href="https://next.frame.io/file/f1">open in Frame.io (sign in)</a>', text)
        self.assertIn("1 comment from them, 2 on the file in total", text)

    def test_the_title_and_the_link_are_escaped(self) -> None:
        self.fake.files["f1"]["name"] = "Cut <1> & more"
        self.fake.files["f1"]["view_url"] = 'https://x.example/?a=1&b="2"'
        self.comment("c1", "Ana Reyes", "u-a", "2026-11-12T10:00:00Z")
        text = self.one_message()
        self.assertIn("<b>Cut &lt;1&gt; &amp; more</b>", text)
        self.assertIn('<a href="https://x.example/?a=1&amp;b=&quot;2&quot;">', text)

    def test_the_share_link_is_the_newest_live_share_that_holds_the_file(self) -> None:
        add = self.fake.add_share
        add("fp1", "s2", "newer", file_ids=("f1", "f2"), created_at="2026-11-05T00:00:00Z")
        add("fp1", "s3", "off", enabled=False, file_ids=("f1",), created_at="2026-11-09T00:00:00Z")
        add("fp1", "s4", "expired", expiration="2020-01-01T00:00:00Z", file_ids=("f1",), created_at="2026-11-09T00:00:00Z")
        add("fp1", "s5", "other file", file_ids=("f2",), created_at="2026-11-10T00:00:00Z")
        self.comment("c1", "Ana Reyes", "u-a", "2026-11-12T10:00:00Z")
        text = self.one_message()
        self.assertIn('Share link: <a href="https://f.io/s2">https://f.io/s2</a>', text)
        self.assertEqual(text.count("https://f.io/"), 2)  # one link: its href and its label

    def test_with_no_matching_share_the_message_says_so_and_keeps_the_asset_link(self) -> None:
        self.fake.shares["fp1"].clear()
        self.comment("c1", "Ana Reyes", "u-a", "2026-11-12T10:00:00Z")
        text = self.one_message()
        self.assertIn("Share link: no share link", text)
        self.assertIn("https://next.frame.io/file/f1", text)

    def test_when_frameio_cannot_answer_the_job_is_tried_again_and_the_last_try_sends_unknown(self) -> None:
        self.comment("c1", "Ana Reyes", "u-a", "2026-11-12T10:00:00Z")
        self.clock.advance(15 * MINUTE)
        self.fake.refuse_next(500, times=30)
        self.run_worker()
        self.assertEqual(self.gateway.texts(), [])  # first try: retried, not sent
        self.clock.advance(31)
        self.run_worker()
        self.assertEqual(self.gateway.texts(), [])
        self.clock.advance(31)
        self.run_worker()
        (text,) = self.gateway.texts()
        self.assertIn("finished commenting on <b>unknown</b>", text)
        self.assertIn("Share link: unknown", text)
        self.assertIn("Frame.io link: unknown", text)
        self.assertIn("unknown on the file in total", text)
        self.assertIn("<b>Ana Reyes</b>", text)

    def test_the_second_try_sends_the_whole_message_when_frameio_is_back(self) -> None:
        self.comment("c1", "Ana Reyes", "u-a", "2026-11-12T10:00:00Z")
        self.clock.advance(15 * MINUTE)
        self.fake.refuse_next(500, times=3)
        self.run_worker()
        self.assertEqual(self.gateway.texts(), [])
        self.clock.advance(31)
        self.run_worker()
        (text,) = self.gateway.texts()
        self.assertIn("<b>Cut 1</b>", text)
        self.assertNotIn("unknown", text)

    def test_the_review_job_needs_its_times_and_count(self) -> None:
        self.queue.submit("main", "frameio.review_done", "bad", {"file_id": "f1"})
        self.run_worker()
        result = self.queue.result(1, "main")
        assert result is not None
        self.assertEqual(result.status, "failed")
        self.assertEqual(self.gateway.texts(), [])


class ReviewChatConfigTests(ServerTestCase):
    ENVIRON = {
        "T6_SERVER_URL": "http://x",
        "T6_WORKER_SECRET": "s",
        "T6_WORKER_QUEUE": "main",
        "T6_GATEWAY_URL": "http://gw",
        "T6_GATEWAY_SECRET": "g",
    }

    def handlers(self, **extra: str) -> dict[str, dict[str, Any]]:
        environ = {**self.ENVIRON, **extra}
        return configured_handlers(environ, WorkerSettings.from_env(environ), InProcess(self.app))

    def test_a_main_worker_with_a_gateway_needs_the_review_chat(self) -> None:
        with self.assertRaisesRegex(ConfigError, "T6_REVIEW_CHAT"):
            self.handlers()
        with self.assertRaisesRegex(ConfigError, "T6_REVIEW_CHAT"):
            self.handlers(T6_REVIEW_CHAT="the team chat")
        with self.assertRaisesRegex(ConfigError, "T6_REVIEW_TOPIC"):
            self.handlers(T6_REVIEW_CHAT="-100", T6_REVIEW_TOPIC="general")

    def test_the_review_handler_is_registered_with_a_chat_and_an_optional_topic(self) -> None:
        self.assertIn("frameio.review_done", self.handlers(T6_REVIEW_CHAT="-100")["main"])
        self.assertIn("frameio.review_done", self.handlers(T6_REVIEW_CHAT="-100", T6_REVIEW_TOPIC="")["main"])
        self.assertIn("frameio.review_done", self.handlers(T6_REVIEW_CHAT="-100", T6_REVIEW_TOPIC="5")["main"])
