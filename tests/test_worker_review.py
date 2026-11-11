"""Frame.io review done: webhook, quiet timer, main worker, DM. Fake clock, fake /notify, fake Frame.io worker."""

from collections.abc import Mapping
import json
from typing import Any

from t6.frameio import Failed, FrameioProjects, FrameioRequest, FrameioResult, FrameioWorker, ask
from t6.domain.events import JsonValue
from t6.notion.external_links import ExternalLinks
from t6.notion.testing import FakeNotion, external_link_properties, relation, rich_text, title
from t6.worker import QueueClient, Worker
from t6.worker.gateway import GatewayClient
from t6.worker.review import ReviewDone
from test_frameio_worker import FakeTokens
from test_server_app import (
    FRAMEIO_WORKER_SECRET,
    WORKER_SECRET,
    InProcess,
    ServerTestCase,
    comment,
    frameio_request,
)

LINKS, PROJECTS, CONTACTS = "ds-external-links", "ds-projects", "ds-contacts"
MINUTE = 60.0


class FakeNotify:
    """Stands in for the gateway's /notify: one message per idempotency key."""

    def __init__(self) -> None:
        self.sent: dict[str, dict[str, Any]] = {}

    def send(self, method: str, path: str, headers: Mapping[str, str], body: bytes) -> tuple[int, dict[str, Any]]:
        message = json.loads(body)
        self.sent.setdefault(message["idempotency_key"], message)
        return 202, {"message_ref": len(self.sent)}

    def dms(self) -> list[tuple[int, str]]:
        return [(message["to"], message["text"]) for message in self.sent.values()]


class ReviewDoneTests(ServerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.notion = FakeNotion()
        for source in (LINKS, PROJECTS, CONTACTS):
            self.notion.add_data_source(source)
        self.add_coordinator("contact-1", "1001")
        self.notion.add_page(
            PROJECTS,
            {"Name": title("Example Show"), "T6 Post Coordinator": relation("contact-1")},
            page_id="show-1",
        )
        self.link_row = self.notion.add_page(
            LINKS, external_link_properties(system="frameio", kind="project", external_id="fp1", projects=["show-1"])
        )
        client = self.notion.client()
        self.addCleanup(client.close)
        self.gateway = FakeNotify()
        self.frameio_answers: JsonValue | Exception = [
            {"id": "fp1", "name": "Example Show", "url": "https://next.frame.io/project/fp1"}
        ]
        self.frameio_asked: list[FrameioRequest] = []
        frameio = FrameioWorker(FakeTokens(), self.run_frameio, lambda text, key: None, self.clock)
        self.frameio_worker = Worker(
            QueueClient(InProcess(self.app), FRAMEIO_WORKER_SECRET), "frameio", frameio.handlers()
        )
        self.frameio_up = True
        self.queue_client = QueueClient(InProcess(self.app), WORKER_SECRET)
        review = ReviewDone(client, ExternalLinks(client, LINKS), GatewayClient(self.gateway, "secret"), self.ask)
        self.worker = Worker(self.queue_client, "main", review.handlers())

    def add_coordinator(self, page_id: str, telegram_user_id: str | None) -> None:
        self.notion.add_page(
            CONTACTS,
            {"Name": title("Coordinator One"), "TelegramUserID": rich_text(telegram_user_id)},
            page_id=page_id,
        )

    def run_frameio(self, request: FrameioRequest) -> JsonValue:
        self.frameio_asked.append(request)
        if isinstance(self.frameio_answers, Exception):
            raise self.frameio_answers
        return self.frameio_answers

    def ask(self, request: FrameioRequest) -> FrameioResult:
        def serve(seconds: float) -> None:
            if self.frameio_up:
                self.frameio_worker.run_once()

        return ask(self.queue_client, request, timeout=3, clock=iter(range(100)).__next__, sleep=serve)

    def comment(self, comment_id: str, project_id: str = "fp1") -> None:
        answer = self.app.public(frameio_request(self.clock, comment(comment_id, project_id)))
        self.assertEqual(answer.status, 200)

    def run_worker(self) -> None:
        while self.worker.run_once():
            pass

    def test_one_comment_then_fifteen_quiet_minutes_sends_one_dm_with_count_one(self) -> None:
        self.comment("c1")
        self.clock.advance(15 * MINUTE - 1)
        self.run_worker()
        self.assertEqual(self.gateway.dms(), [])
        self.clock.advance(1)
        self.run_worker()
        ((chat, text),) = self.gateway.dms()
        self.assertEqual(chat, 1001)
        self.assertIn("1 comment.", text)
        self.assertIn("Example Show", text)
        self.assertIn("https://next.frame.io/project/fp1", text)

    def test_comments_at_minute_0_10_and_20_send_one_dm_at_minute_35_with_count_three(self) -> None:
        for comment_id in ("c1", "c2", "c3"):
            self.comment(comment_id)
            self.clock.advance(10 * MINUTE)
            self.run_worker()
        self.clock.advance(5 * MINUTE - 1)  # minute 35 less a second
        self.run_worker()
        self.assertEqual(self.gateway.dms(), [])
        self.clock.advance(1)
        self.run_worker()
        self.assertEqual(len(self.gateway.dms()), 1)
        self.assertIn("3 comments.", self.gateway.dms()[0][1])

    def test_a_redelivered_webhook_does_not_send_a_second_dm_or_change_the_count(self) -> None:
        self.comment("c1")
        self.comment("c1")
        self.clock.advance(15 * MINUTE)
        self.run_worker()
        self.comment("c1")
        self.clock.advance(15 * MINUTE)
        self.run_worker()
        self.assertEqual(len(self.gateway.dms()), 1)
        self.assertIn("1 comment.", self.gateway.dms()[0][1])

    def test_a_job_that_runs_twice_sends_one_dm(self) -> None:
        self.comment("c1")
        self.clock.advance(15 * MINUTE)
        claimed = self.queue_client.claim("main")  # a worker that died before finishing
        assert claimed is not None
        self.clock.advance(2 * MINUTE)
        self.run_worker()
        self.run_worker()
        self.assertEqual(len(self.gateway.dms()), 1)

    def test_the_dm_goes_to_every_coordinator_of_the_show_the_review_belongs_to(self) -> None:
        self.add_coordinator("contact-2", "1002")
        self.add_coordinator("contact-3", None)
        self.add_coordinator("contact-9", "1009")
        self.notion.add_page(
            PROJECTS,
            {"Name": title("Second Show"), "T6 Post Coordinator": relation("contact-2", "contact-3")},
            page_id="show-2",
        )
        self.notion.add_page(
            LINKS, external_link_properties(system="frameio", kind="project", external_id="fp2", projects=["show-2"])
        )
        self.frameio_answers = [{"id": "fp2", "name": "Second Show", "url": "https://next.frame.io/project/fp2"}]
        self.comment("c1", "fp2")
        self.clock.advance(15 * MINUTE)
        self.run_worker()
        ((chat, text),) = self.gateway.dms()
        self.assertEqual(chat, 1002)
        self.assertIn("Second Show", text)

    def test_the_link_is_asked_from_the_frameio_worker_through_its_queue(self) -> None:
        self.comment("c1")
        self.clock.advance(15 * MINUTE)
        self.run_worker()
        self.assertEqual(self.frameio_asked, [FrameioProjects()])

    def test_without_the_frameio_worker_the_rows_url_is_used_and_without_either_the_job_is_tried_again(self) -> None:
        self.frameio_up = False
        self.comment("c1")
        self.clock.advance(15 * MINUTE)
        self.run_worker()
        self.assertEqual(self.gateway.dms(), [])  # no link anywhere: retried, not sent
        self.frameio_up = True
        self.clock.advance(MINUTE)
        self.run_worker()
        self.assertEqual(len(self.gateway.dms()), 1)

        self.notion.add_page(
            LINKS,
            external_link_properties(
                system="frameio", kind="project", external_id="fp3", url="https://f.io/row", projects=["show-1"]
            ),
        )
        self.frameio_answers = RuntimeError("Frame.io answered 500")
        self.comment("c2", "fp3")
        self.clock.advance(15 * MINUTE)
        self.run_worker()
        self.assertIn("https://f.io/row", self.gateway.dms()[-1][1])
        self.assertIsInstance(self.ask(FrameioProjects()), Failed)

    def test_a_review_with_no_show_or_no_coordinator_fails_with_a_reason_and_sends_nothing(self) -> None:
        self.notion.add_page(PROJECTS, {"Name": title("No Coordinator Show")}, page_id="show-3")
        self.notion.add_page(
            LINKS, external_link_properties(system="frameio", kind="project", external_id="fp3", projects=["show-3"])
        )
        for project_id, reason in (("fp-unknown", "External Links"), ("fp3", "T6 Post Coordinator")):
            with self.subTest(project_id=project_id):
                self.comment(f"c-{project_id}", project_id)
                self.clock.advance(15 * MINUTE)
                claim = self.queue.claim("main", 60)
                assert claim is not None
                self.queue.fail(claim.job_id, claim.token, "requeue", retry_in=0)
                self.run_worker()
                result = self.queue.result(claim.job_id, "main")
                assert result is not None
                self.assertEqual(result.status, "failed")
                self.assertIn(reason, str(result.value))
        self.assertEqual(self.gateway.dms(), [])
