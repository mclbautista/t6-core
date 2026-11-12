"""Per-user Frame.io review message: webhook, Frame.io worker, review timer, main worker, /notify.

Fake clock, fake Frame.io V4, fake gateway.
"""

from collections.abc import Mapping
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
        reads = FrameioReads(FrameioApi(StaticTokens(), self.fake.fetch))
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

    def comment(self, comment_id: str, author: str, author_id: str, at: str, file_id: str = "f1") -> None:
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
