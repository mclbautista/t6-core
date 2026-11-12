"""Webhook server, tap endpoint and worker, end to end in one process on a fake clock."""

from collections.abc import Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import hmac
import json
from pathlib import Path
import socket
import tempfile
from typing import Any
import unittest
from unittest import mock

from t6.server import Grant, JobQueue, Request, ServerConfig, WebhookApp
from t6.server.http import make_server
from t6.server.signatures import notion_signature
from t6.server.testing import FakeClock
from t6.worker import QueueClient, QueueRejected, Worker

FRAMEIO_SECRET = "frameio-secret"
NOTION_TOKEN = "notion-token"
TAP_SECRET = "tap-secret"
WORKER_SECRET = "worker-secret"
FRAMEIO_WORKER_SECRET = "frameio-worker-secret"


def frameio_request(clock: FakeClock, body: dict[str, Any], *, secret: str = FRAMEIO_SECRET, age: float = 0) -> Request:
    raw = json.dumps(body).encode()
    stamp = str(int(clock() - age))
    digest = hmac.new(secret.encode(), b"v0:" + stamp.encode() + b":" + raw, hashlib.sha256).hexdigest()
    headers = {"X-Frameio-Request-Timestamp": stamp, "X-Frameio-Signature": "v0=" + digest}
    return Request("POST", "/webhook", headers, raw)


def comment(comment_id: str = "c1", project_id: str = "fp1") -> dict[str, Any]:
    return {"type": "comment.created", "resource": {"id": comment_id}, "project": {"id": project_id}}


def notion_request(clock: FakeClock, event: dict[str, Any], *, token: str = NOTION_TOKEN) -> Request:
    raw = json.dumps(event).encode()
    return Request("POST", "/webhook/notion", {"X-Notion-Signature": notion_signature(token, raw)}, raw)


def notion_event(clock: FakeClock, event_id: str = "e1", age: float = 0) -> dict[str, Any]:
    when = datetime.fromtimestamp(clock() - age, timezone.utc).isoformat()
    return {
        "id": event_id,
        "timestamp": when,
        "type": "page.properties_updated",
        "entity": {"id": "page-1", "type": "page"},
        "data": {"updated_properties": ["abc"]},
    }


def tap_request(key: str = "tap-7", secret: str = TAP_SECRET) -> Request:
    body = {"kind": "tap", "data": "approve:p1", "from": {"id": 42}, "chat": -100, "message_id": 9, "update_id": 7}
    return Request(
        "POST", "/tap", {"Authorization": f"Bearer {secret}", "Idempotency-Key": key}, json.dumps(body).encode()
    )


class InProcess:
    """A worker transport that calls the tailnet handler directly. No sockets."""

    def __init__(self, app: WebhookApp) -> None:
        self.app = app

    def send(self, method: str, path: str, headers: Mapping[str, str], body: bytes) -> tuple[int, dict[str, Any]]:
        answer = self.app.tailnet(Request(method, path, dict(headers), body))
        return answer.status, json.loads(json.dumps(dict(answer.body)))


class ServerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.clock = FakeClock()
        self.queue = JobQueue(Path(self.directory.name) / "q.sqlite3", self.clock)
        main, frameio = frozenset({"main"}), frozenset({"frameio"})
        config = ServerConfig(
            frameio_webhook_secret=FRAMEIO_SECRET,
            notion_webhook_token=NOTION_TOKEN,
            tap_secret=TAP_SECRET,
            grants=(
                Grant(WORKER_SECRET, claim=main, submit=main | frameio),
                Grant(FRAMEIO_WORKER_SECRET, claim=frameio),
            ),
        )
        self.tokens: list[str] = []
        self.app = WebhookApp(self.queue, config, self.clock, self.tokens.append)

    def pending(self, queue: str = "main") -> int:
        count = 0
        while self.queue.claim(queue, 1):
            count += 1
        return count


class SignatureTests(ServerTestCase):
    def test_bad_frameio_signature_is_rejected_and_queues_nothing(self) -> None:
        request = frameio_request(self.clock, comment(), secret="wrong")
        self.assertEqual(self.app.public(request).status, 401)
        self.clock.advance(10_000)
        self.assertEqual(self.pending(), 0)

    def test_stale_frameio_timestamp_is_rejected_even_when_signed(self) -> None:
        stale = frameio_request(self.clock, comment(), age=301)
        self.assertEqual(self.app.public(stale).status, 401)
        future = frameio_request(self.clock, comment(), age=-301)
        self.assertEqual(self.app.public(future).status, 401)
        fresh = frameio_request(self.clock, comment(), age=299)
        self.assertEqual(self.app.public(fresh).status, 200)

    def test_body_changed_after_signing_is_rejected(self) -> None:
        request = frameio_request(self.clock, comment("c1"))
        tampered = Request(request.method, request.path, request.headers, request.body.replace(b"c1", b"c2"))
        self.assertEqual(self.app.public(tampered).status, 401)

    def test_bad_notion_signature_is_rejected_and_queues_nothing(self) -> None:
        request = notion_request(self.clock, notion_event(self.clock), token="wrong")
        self.assertEqual(self.app.public(request).status, 401)
        self.assertEqual(self.pending(), 0)

    def test_stale_notion_event_is_rejected(self) -> None:
        old = notion_request(self.clock, notion_event(self.clock, age=26 * 3600))
        self.assertEqual(self.app.public(old).status, 401)
        retry = notion_request(self.clock, notion_event(self.clock, "e2", age=23 * 3600))
        self.assertEqual(self.app.public(retry).status, 200)  # a late retry is legitimate

    def test_unsigned_request_is_rejected(self) -> None:
        self.assertEqual(self.app.public(Request("POST", "/webhook", {}, b"{}")).status, 401)

    def test_unconfigured_secret_fails_closed(self) -> None:
        app = WebhookApp(self.queue, ServerConfig(), self.clock)
        self.assertEqual(app.public(frameio_request(self.clock, comment())).status, 503)

    def test_notion_setup_token_is_handed_over_once(self) -> None:
        request = Request("POST", "/webhook", {}, json.dumps({"verification_token": "secret_abc"}).encode())
        self.assertEqual(self.app.public(request).status, 200)
        self.assertEqual(self.tokens, ["secret_abc"])
        self.assertEqual(self.pending(), 0)


class DedupeTests(ServerTestCase):
    def test_same_notion_event_twice_is_one_job(self) -> None:
        request = notion_request(self.clock, notion_event(self.clock))
        first, second = self.app.public(request), self.app.public(request)
        self.assertEqual((first.body["created"], second.body["created"]), (True, False))
        claim = self.queue.claim("main", 60)
        assert claim is not None
        self.assertEqual(claim.kind, "notion.page_changed")
        self.assertEqual(claim.payload["page_id"], "page-1")
        self.assertEqual(self.pending(), 0)

    def test_same_frameio_comment_twice_counts_once(self) -> None:
        self.app.public(frameio_request(self.clock, comment("c1")))
        self.app.public(frameio_request(self.clock, comment("c1")))
        self.app.public(frameio_request(self.clock, comment("c2")))
        self.clock.advance(15 * 60)
        claim = self.queue.claim("main", 60)
        assert claim is not None
        self.assertEqual(claim.kind, "frameio.review_done")
        self.assertEqual(dict(claim.payload), {"frameio_project_id": "fp1", "count": 2})

    def test_review_timer_waits_for_quiet(self) -> None:
        self.app.public(frameio_request(self.clock, comment("c1")))
        self.clock.advance(14 * 60)
        self.app.public(frameio_request(self.clock, comment("c2")))
        self.clock.advance(14 * 60)
        self.assertEqual(self.pending(), 0)
        self.clock.advance(60)
        self.assertEqual(self.pending(), 1)

    def test_quiet_time_comes_from_the_environment(self) -> None:
        environ = {"T6_FRAMEIO_WEBHOOK_SECRET": FRAMEIO_SECRET, "T6_REVIEW_QUIET_SECONDS": "120"}
        queue = JobQueue(Path(self.directory.name) / "env.sqlite3", self.clock)
        app = WebhookApp(queue, ServerConfig.from_env(environ), self.clock)
        app.public(frameio_request(self.clock, comment("c1")))
        self.clock.advance(119)
        self.assertIsNone(queue.claim("main", 60))
        self.clock.advance(1)
        claim = queue.claim("main", 60)
        assert claim is not None
        self.assertEqual(claim.kind, "frameio.review_done")

    def test_quiet_time_defaults_to_fifteen_minutes_and_rejects_bad_values(self) -> None:
        self.assertEqual(ServerConfig.from_env({}).review_quiet_seconds, 15 * 60.0)
        self.assertEqual(ServerConfig.from_env({"T6_REVIEW_QUIET_SECONDS": ""}).review_quiet_seconds, 15 * 60.0)
        self.assertEqual(ServerConfig.from_env({"T6_REVIEW_QUIET_SECONDS": "90.5"}).review_quiet_seconds, 90.5)
        for bad in ("abc", "0", "-5", "nan", "inf"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ServerConfig.from_env({"T6_REVIEW_QUIET_SECONDS": bad})

    def test_other_event_types_are_acknowledged_and_ignored(self) -> None:
        body = {"type": "file.ready", "resource": {"id": "f1"}, "project": {"id": "fp1"}}
        self.assertEqual(self.app.public(frameio_request(self.clock, body)).status, 200)
        self.clock.advance(3600)
        self.assertEqual(self.pending(), 0)


class TapTests(ServerTestCase):
    def test_tap_with_the_secret_becomes_one_job(self) -> None:
        answer = self.app.tailnet(tap_request())
        self.assertEqual((answer.status, answer.body["created"]), (200, True))
        claim = self.queue.claim("main", 60)
        assert claim is not None
        self.assertEqual(claim.kind, "tap")
        self.assertEqual(claim.payload["data"], "approve:p1")
        self.assertEqual(claim.payload["from_id"], 42)

    def test_same_tap_twice_is_one_job(self) -> None:
        self.app.tailnet(tap_request())
        again = self.app.tailnet(tap_request())
        self.assertEqual((again.status, again.body["created"]), (200, False))
        self.assertEqual(self.pending(), 1)

    def test_wrong_or_missing_secret_is_rejected(self) -> None:
        self.assertEqual(self.app.tailnet(tap_request(secret="wrong")).status, 401)
        bare = Request("POST", "/tap", {"Idempotency-Key": "k"}, b"{}")
        self.assertEqual(self.app.tailnet(bare).status, 401)
        self.assertEqual(self.pending(), 0)

    def test_tap_without_an_idempotency_key_is_rejected(self) -> None:
        request = Request("POST", "/tap", {"Authorization": f"Bearer {TAP_SECRET}"}, b"{}")
        self.assertEqual(self.app.tailnet(request).status, 400)

    def test_tap_and_queue_routes_are_not_on_the_public_path(self) -> None:
        self.assertEqual(self.app.public(tap_request()).status, 404)
        claim = Request("POST", "/queue/claim", {"Authorization": f"Bearer {WORKER_SECRET}"}, b"{}")
        self.assertEqual(self.app.public(claim).status, 404)
        self.assertEqual(self.app.public(Request("POST", "/webhookx", {}, b"")).status, 404)
        self.assertEqual(self.pending(), 0)

    def test_webhook_path_is_not_on_the_tailnet_listener(self) -> None:
        self.assertEqual(self.app.tailnet(frameio_request(self.clock, comment())).status, 404)


class WorkerTests(ServerTestCase):
    def worker_client(self, secret: str = WORKER_SECRET) -> QueueClient:
        return QueueClient(InProcess(self.app), secret)

    def test_worker_pulls_a_job_end_to_end_and_the_server_never_connects_out(self) -> None:
        handled: list[str] = []

        def handler(job: Any) -> Any:
            handled.append(job.payload["page_id"])
            return {"seen": job.payload["page_id"]}

        worker = Worker(self.worker_client(), "main", {"notion.page_changed": handler})
        with self.no_outbound_connections():
            self.app.public(notion_request(self.clock, notion_event(self.clock)))
            self.assertTrue(worker.run_once())
            self.assertFalse(worker.run_once())
        self.assertEqual(handled, ["page-1"])
        # Acked: it does not come back after its lease would have run out.
        self.clock.advance(3600)
        self.assertFalse(worker.run_once())

    @contextmanager
    def no_outbound_connections(self):  # type: ignore[no-untyped-def]
        def refuse(*args: object, **kwargs: object) -> None:
            raise AssertionError("the server opened a connection")

        with mock.patch.object(socket.socket, "connect", refuse), mock.patch.object(
            socket, "create_connection", refuse
        ):
            yield

    def test_worker_that_dies_loses_its_lease_and_another_run_gets_the_job(self) -> None:
        self.app.tailnet(tap_request())
        dead = self.worker_client()
        self.assertIsNotNone(dead.claim("main", lease_seconds=60))  # then never acks
        self.clock.advance(61)
        done: list[int] = []
        Worker(self.worker_client(), "main", {"tap": lambda job: done.append(job.attempt)}).run_once()
        self.assertEqual(done, [2])

    def test_failing_handler_retries_later(self) -> None:
        self.app.tailnet(tap_request())
        calls: list[int] = []

        def flaky(job: Any) -> Any:
            calls.append(job.attempt)
            if job.attempt == 1:
                raise RuntimeError("boom")
            return None

        worker = Worker(self.worker_client(), "main", {"tap": flaky})
        worker.run_once()
        self.assertFalse(worker.run_once())  # waiting out the retry delay
        self.clock.advance(31)
        worker.run_once()
        self.assertEqual(calls, [1, 2])

    def test_worker_of_one_queue_cannot_claim_from_another(self) -> None:
        self.app.tailnet(tap_request())  # a main-queue job
        frameio_worker = self.worker_client(FRAMEIO_WORKER_SECRET)
        self.assertIsNone(frameio_worker.claim("frameio"))
        with self.assertRaises(QueueRejected) as refused:
            frameio_worker.claim("main")
        self.assertEqual(refused.exception.status, 403)
        # And it cannot finish a main-queue job by naming its own queue.
        claim = self.worker_client().claim("main")
        assert claim is not None
        with self.assertRaises(QueueRejected):
            frameio_worker.ack(claim)

    def test_unknown_secret_is_rejected(self) -> None:
        with self.assertRaises(QueueRejected) as refused:
            self.worker_client("nope").claim("main")
        self.assertEqual(refused.exception.status, 401)

    def test_request_and_result_round_trip_on_the_frameio_queue(self) -> None:
        caller = self.worker_client()  # the main worker asks for Frame.io data
        job_id = caller.submit("frameio", "frameio.request", "req-1", {"want": "share_links"})
        self.assertIsNone(caller.result("frameio", job_id))
        self.assertIsNone(self.worker_client().claim("main"))  # main workers never see it
        frameio = self.worker_client(FRAMEIO_WORKER_SECRET)
        job = frameio.claim("frameio")
        assert job is not None
        frameio.ack(job, {"links": ["https://f.io/x"]})
        self.assertEqual(caller.result("frameio", job_id), ("ok", {"links": ["https://f.io/x"]}))

    def test_failed_request_reports_a_failed_result(self) -> None:
        caller = self.worker_client()
        job_id = caller.submit("frameio", "frameio.request", "req-2")
        frameio = self.worker_client(FRAMEIO_WORKER_SECRET)
        job = frameio.claim("frameio")
        assert job is not None
        frameio.fail(job, "token expired")
        self.assertEqual(caller.result("frameio", job_id), ("failed", "token expired"))

    def test_frameio_worker_cannot_submit_or_read(self) -> None:
        with self.assertRaises(QueueRejected):
            self.worker_client(FRAMEIO_WORKER_SECRET).submit("main", "x", "k")


class HttpTests(ServerTestCase):
    def test_real_sockets_serve_both_listeners(self) -> None:
        import threading
        import urllib.error
        import urllib.request

        public = make_server(("127.0.0.1", 0), self.app.public)
        tailnet = make_server(("127.0.0.1", 0), self.app.tailnet)
        for server in (public, tailnet):
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        tap = tap_request()
        post = urllib.request.Request(
            f"http://127.0.0.1:{tailnet.server_port}/tap", tap.body, dict(tap.headers), method="POST"
        )
        with urllib.request.urlopen(post) as answer:
            self.assertEqual(answer.status, 200)
        post_public = urllib.request.Request(
            f"http://127.0.0.1:{public.server_port}/tap", tap.body, dict(tap.headers), method="POST"
        )
        with self.assertRaises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(post_public)
        self.assertEqual(refused.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
