"""The Frame.io worker and its callers, end to end through the in-process queue.

A fake Frame.io API, a fake token store and a fake clock. No real client data.
"""

from datetime import datetime, timezone
from pathlib import Path
import contextlib
import html
import io
import json
import subprocess
import sys
import tempfile
import threading
from typing import Any
import unittest
from unittest import mock
import urllib.parse

from t6.domain.events import JsonValue
from t6.frameio import (
    Approvals,
    BriefingFeed,
    Comments,
    CreateProjectFolders,
    Failed,
    FrameioRequest,
    FrameioWorker,
    Ok,
    FrameioProjects,
    ShareLinks,
    SignInRequired,
    Versions,
    ask,
)
from t6.frameio.model import decode
from t6.frameio.requests import from_job, to_payload
from t6.frameio.testing import FakeFrameio as FakeFrameioApi
from t6.frameio.worker import REFRESH_RETRY_SECONDS, REFRESH_SECONDS, SIGN_IN_NEEDED
from t6.server import JobQueue, Request, ServerConfig, WebhookApp
from t6.server.testing import FakeClock
from t6.worker import QueueClient, Worker
from t6.worker.__main__ import ConfigError, frameio_backend, frameio_worker, main
from test_frameio_tokens import FakeAdobe
from test_server_app import FRAMEIO_WORKER_SECRET, WORKER_SECRET, InProcess, ServerTestCase

SINCE = datetime(2026, 11, 1, 16, 0, tzinfo=timezone.utc)
REQUESTS: tuple[FrameioRequest, ...] = (
    FrameioProjects(),
    ShareLinks(frameio_project_id="fp1"),
    Comments(file_id="f1"),
    Versions(file_id="f1"),
    Approvals(file_id="f1"),
    BriefingFeed(since=SINCE),
    CreateProjectFolders(project_id="t6p_1", name="Show One"),
)


class FakeFrameio:
    """Stands in for the Frame.io API: answers every request with its own name."""

    def __init__(self) -> None:
        self.seen: list[FrameioRequest] = []
        self.error: Exception | None = None

    def __call__(self, request: FrameioRequest) -> JsonValue:
        self.seen.append(request)
        if self.error is not None:
            raise self.error
        return {"answered": request.kind}


class FakeTokens:
    """Stands in for the token store and Adobe sign-in."""

    def __init__(self) -> None:
        self.token = "access-token-0"
        self.refreshes = 0
        self.refresh_error: Exception | None = None
        self.exchanged: list[tuple[str, str]] = []

    def sign_in_url(self) -> str:
        return "https://sign-in.example/authorize?state=s1"

    def exchange(self, code: str, state: str) -> None:
        if state != "s1":
            raise SignInRequired("state does not match")
        self.exchanged.append((code, state))
        self.token = f"access-token-from-{code}"

    def refresh(self) -> None:
        if self.refresh_error is not None:
            raise self.refresh_error
        self.refreshes += 1
        self.token = f"access-token-{self.refreshes}"


class FrameioWorkerTestCase(ServerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.frameio = FakeFrameio()
        self.token_store = FakeTokens()
        self.alerts: list[tuple[str, str]] = []
        self.notify_error: Exception | None = None
        self.frameio_worker = FrameioWorker(self.token_store, self.frameio, self.notify, self.clock)
        self.worker = Worker(
            QueueClient(InProcess(self.app), FRAMEIO_WORKER_SECRET), "frameio", self.frameio_worker.handlers()
        )
        self.caller = QueueClient(InProcess(self.app), WORKER_SECRET)

    def notify(self, text: str, key: str) -> None:
        if self.notify_error is not None:
            raise self.notify_error
        self.alerts.append((text, key))

    def serve(self, seconds: float) -> None:
        """What the worker's loop does while a caller sleeps."""
        self.frameio_worker.tick()
        self.worker.run_once()
        self.clock.advance(seconds)

    def ask(self, request: FrameioRequest, timeout: float = 30.0) -> Ok | Failed:
        return ask(self.caller, request, timeout=timeout, clock=self.clock, sleep=self.serve)

    def sign_in(self, code: str = "code-1", state: str = "s1") -> None:
        answer = self.app.public(Request("GET", f"/frameio/callback?code={code}&state={state}", {}, b""))
        self.assertEqual(answer.status, 200)
        self.worker.run_once()


class RequestTests(FrameioWorkerTestCase):
    def test_a_caller_can_import_t6_frameio_before_anything_else(self) -> None:
        done = subprocess.run([sys.executable, "-c", "import t6.frameio"], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_every_request_survives_the_trip_through_a_job_payload(self) -> None:
        for request in REQUESTS:
            with self.subTest(kind=request.kind):
                self.assertEqual(from_job(request.kind, to_payload(request)), request)

    def test_every_request_round_trips_through_the_frameio_queue_with_an_ok_result(self) -> None:
        for request in REQUESTS:
            with self.subTest(kind=request.kind):
                self.assertEqual(self.ask(request), Ok({"answered": request.kind}))
        self.assertEqual(self.frameio.seen, list(REQUESTS))

    def test_blank_fields_and_times_without_a_zone_are_not_requests(self) -> None:
        with self.assertRaises(ValueError):
            ShareLinks(frameio_project_id=" ")
        with self.assertRaises(ValueError):
            BriefingFeed(since=datetime(2026, 11, 1))

    def test_a_frameio_error_is_a_failed_result_and_is_not_retried(self) -> None:
        self.frameio.error = RuntimeError("Frame.io answered 500")
        self.assertEqual(self.ask(FrameioProjects()), Failed("RuntimeError: Frame.io answered 500"))
        self.clock.advance(3600)
        self.assertFalse(self.worker.run_once())
        self.assertEqual(len(self.frameio.seen), 1)

    def test_a_job_that_is_not_a_request_is_a_failed_result(self) -> None:
        job_id = self.caller.submit("frameio", "frameio.share_links", "bad", {"frameio_project_id": 7})
        self.worker.run_once()
        result = self.caller.result("frameio", job_id)
        assert result is not None
        self.assertEqual(result[0], "failed")
        self.assertIn("bad request", str(result[1]))
        self.assertEqual(self.frameio.seen, [])


class CallerTests(FrameioWorkerTestCase):
    def test_a_timeout_is_a_failed_result_not_an_exception(self) -> None:
        result = ask(self.caller, FrameioProjects(), timeout=10, clock=self.clock, sleep=self.clock.advance)
        self.assertEqual(result, Failed("no answer from the Frame.io worker within 10 seconds"))

    def test_a_refused_queue_call_is_a_failed_result_not_an_exception(self) -> None:
        stranger = QueueClient(InProcess(self.app), "not-a-secret")
        result = ask(stranger, FrameioProjects(), clock=self.clock, sleep=self.clock.advance)
        self.assertIsInstance(result, Failed)

    def test_a_malformed_queue_answer_is_a_failed_result_not_an_exception(self) -> None:
        class Broken:
            def send(self, method: str, path: str, headers: object, body: bytes) -> tuple[int, dict[str, object]]:
                return 200, {}

        result = ask(QueueClient(Broken(), "s"), FrameioProjects(), clock=self.clock, sleep=self.clock.advance)
        self.assertIsInstance(result, Failed)

    def test_asking_twice_asks_frameio_twice(self) -> None:
        self.ask(FrameioProjects())
        self.ask(FrameioProjects())
        self.assertEqual(len(self.frameio.seen), 2)

    def test_a_stuck_frameio_request_does_not_block_the_main_queue(self) -> None:
        stuck = self.caller.submit("frameio", FrameioProjects.kind, "stuck")
        claimed = QueueClient(InProcess(self.app), FRAMEIO_WORKER_SECRET).claim("frameio")
        assert claimed is not None  # held by the Frame.io worker and never finished
        self.caller.submit("main", "x", "main-job")
        handled: list[str] = []
        main_worker = Worker(self.caller, "main", {"x": lambda job: handled.append(job.dedupe_key)})
        self.assertTrue(main_worker.run_once())
        self.assertEqual(handled, ["main-job"])
        self.assertIsNone(self.caller.result("frameio", stuck))

    def test_a_caller_secret_may_ask_but_not_claim_or_touch_the_main_queue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            clock = FakeClock()
            environ = {"T6_FRAMEIO_CALLER_SECRET": "caller", "T6_FRAMEIO_WORKER_SECRET": "worker"}
            app = WebhookApp(JobQueue(Path(directory) / "q.sqlite3", clock), ServerConfig.from_env(environ), clock)
            caller = QueueClient(InProcess(app), "caller")
            job_id = caller.submit("frameio", FrameioProjects.kind, "k")
            self.assertIsNone(caller.result("frameio", job_id))
            for refused in (lambda: caller.claim("frameio"), lambda: caller.submit("main", "x", "k")):
                with self.assertRaisesRegex(Exception, "403"):
                    refused()


class SignInTests(FrameioWorkerTestCase):
    def test_the_callback_code_reaches_the_worker_and_is_exchanged_there(self) -> None:
        self.sign_in("code-1")
        self.assertEqual(self.token_store.exchanged, [("code-1", "s1")])
        stored = (Path(self.directory.name) / "q.sqlite3").read_bytes()
        self.assertNotIn(self.token_store.token.encode(), stored)  # the VPS queue never sees a token

    def test_the_same_code_delivered_twice_is_exchanged_once(self) -> None:
        self.sign_in("code-1")
        self.sign_in("code-1")
        self.assertEqual(self.token_store.exchanged, [("code-1", "s1")])

    def test_a_callback_without_a_code_or_state_queues_nothing(self) -> None:
        for path in ("/frameio/callback?error=access_denied&state=s1", "/frameio/callback?code=c", "/frameio/callback"):
            with self.subTest(path=path):
                self.assertEqual(self.app.public(Request("GET", path, {}, b"")).status, 400)
        self.assertEqual(self.app.public(Request("POST", "/frameio/callback?code=c&state=s1", {}, b"")).status, 405)
        self.assertFalse(self.worker.run_once())

    def test_the_callback_is_off_without_a_frameio_worker_and_is_not_on_the_tailnet(self) -> None:
        request = Request("GET", "/frameio/callback?code=c&state=s1", {}, b"")
        self.assertEqual(self.app.tailnet(request).status, 404)
        app = WebhookApp(self.queue, ServerConfig(), self.clock)
        self.assertEqual(app.public(request).status, 503)

    def test_a_refused_code_leaves_the_worker_signed_out(self) -> None:
        self.token_store.refresh_error = SignInRequired("refresh token revoked")
        self.frameio_worker.tick()
        self.sign_in("code-1", state="forged")
        self.assertEqual(self.ask(FrameioProjects(), timeout=1), Failed(SIGN_IN_NEEDED))


class RefreshTests(FrameioWorkerTestCase):
    def test_the_token_is_refreshed_once_a_day(self) -> None:
        self.frameio_worker.tick()
        self.frameio_worker.tick()
        self.assertEqual(self.token_store.refreshes, 1)
        self.clock.advance(REFRESH_SECONDS - 1)
        self.frameio_worker.tick()
        self.assertEqual(self.token_store.refreshes, 1)
        self.clock.advance(1)
        self.frameio_worker.tick()
        self.assertEqual(self.token_store.refreshes, 2)

    def test_a_refresh_failure_sends_one_alert_and_fails_requests_until_sign_in(self) -> None:
        self.token_store.refresh_error = SignInRequired("refresh token revoked")
        self.frameio_worker.tick()
        for _ in range(3):
            self.assertEqual(self.ask(FrameioProjects(), timeout=1), Failed(SIGN_IN_NEEDED))
            self.clock.advance(REFRESH_SECONDS)
            self.frameio_worker.tick()
        self.assertEqual(len(self.alerts), 1)
        self.assertIn(self.token_store.sign_in_url(), self.alerts[0][0])
        self.assertEqual(self.frameio.seen, [])

        self.token_store.refresh_error = None
        self.sign_in("code-2")
        self.assertEqual(self.ask(FrameioProjects()), Ok({"answered": FrameioProjects.kind}))
        self.assertEqual(len(self.alerts), 1)

    def test_a_token_refused_during_a_request_also_alerts_once(self) -> None:
        self.frameio_worker.tick()
        self.frameio.error = SignInRequired("401 from Frame.io")
        self.assertEqual(self.ask(FrameioProjects(), timeout=1), Failed(SIGN_IN_NEEDED))
        self.assertEqual(self.ask(FrameioProjects(), timeout=1), Failed(SIGN_IN_NEEDED))
        self.assertEqual(len(self.alerts), 1)
        self.assertEqual(len(self.frameio.seen), 1)

    def test_an_unreachable_sign_in_service_is_retried_without_an_alert(self) -> None:
        self.token_store.refresh_error = OSError("network down")
        self.frameio_worker.tick()
        self.token_store.refresh_error = None
        self.clock.advance(REFRESH_RETRY_SECONDS - 1)
        self.frameio_worker.tick()
        self.assertEqual(self.token_store.refreshes, 0)
        self.clock.advance(1)
        self.frameio_worker.tick()
        self.assertEqual(self.token_store.refreshes, 1)
        self.assertEqual(self.alerts, [])
        self.assertEqual(self.ask(FrameioProjects()), Ok({"answered": FrameioProjects.kind}))

    def test_an_alert_the_gateway_did_not_take_goes_out_on_a_later_tick_once(self) -> None:
        self.token_store.refresh_error = SignInRequired("refresh token revoked")
        self.notify_error = OSError("gateway down")
        self.frameio_worker.tick()
        self.assertEqual(self.alerts, [])
        self.notify_error = None
        self.frameio_worker.tick()
        self.frameio_worker.tick()
        self.assertEqual(len(self.alerts), 1)

    def test_the_alert_key_is_the_same_after_a_restart(self) -> None:
        self.token_store.refresh_error = SignInRequired("refresh token revoked")
        self.frameio_worker.tick()
        FrameioWorker(self.token_store, self.frameio, self.notify, self.clock).tick()
        self.assertEqual(len({key for _, key in self.alerts}), 1)

    def test_a_later_sign_out_with_the_same_link_gets_a_new_alert_key(self) -> None:
        self.token_store.refresh_error = SignInRequired("refresh token revoked")
        self.frameio_worker.tick()
        self.sign_in("code-1")
        self.clock.advance(REFRESH_SECONDS)
        self.frameio_worker.tick()
        self.assertEqual(len({key for _, key in self.alerts}), 2)


FRAMEIO_ENVIRON = {
    "T6_SERVER_URL": "http://server.invalid:8081",
    "T6_WORKER_SECRET": FRAMEIO_WORKER_SECRET,
    "T6_WORKER_QUEUE": "frameio",
}
ADOBE_ENVIRON = {
    "T6_FRAMEIO_CLIENT_ID": "client-1",
    "T6_FRAMEIO_CLIENT_SECRET": "client-secret-1",
    "T6_FRAMEIO_REDIRECT_URI": "https://hooks.example/frameio/callback",
    "T6_FRAMEIO_TOKEN_FILE": "/nonexistent/frameio-token.json",
}
GATEWAY_ENVIRON = {
    "T6_GATEWAY_URL": "http://gateway.invalid",
    "T6_GATEWAY_SECRET": "gateway-secret",
    "T6_FRAMEIO_ALERT_CHAT": "42",
}


class StopOnIdle(threading.Event):
    """A stop flag whose wait returns at once and ends the loop, so a test never sleeps."""

    def wait(self, timeout: float | None = None) -> bool:
        self.set()
        return True


class EntrypointTests(FrameioWorkerTestCase):
    def run_main(self, environ: dict[str, str], **kwargs: Any) -> tuple[int, str]:
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            code = main(environ, transport=InProcess(self.app), stop=StopOnIdle(), **kwargs)
        return code, errors.getvalue()

    def test_the_frameio_queue_serves_requests_and_refreshes_the_token(self) -> None:
        job_id = self.caller.submit("frameio", FrameioProjects.kind, "k")
        code, _ = self.run_main(FRAMEIO_ENVIRON, frameio=self.frameio_worker)
        self.assertEqual(code, 0)
        self.assertEqual(self.caller.result("frameio", job_id), ("ok", {"answered": FrameioProjects.kind}))
        self.assertEqual(self.token_store.refreshes, 1)

    def test_a_lost_sign_in_alerts_from_the_loop_and_fails_the_request(self) -> None:
        self.token_store.refresh_error = SignInRequired("refresh token revoked")
        job_id = self.caller.submit("frameio", FrameioProjects.kind, "k")
        self.run_main(FRAMEIO_ENVIRON, frameio=self.frameio_worker)
        self.assertEqual(self.caller.result("frameio", job_id), ("failed", SIGN_IN_NEEDED))
        self.assertEqual(len(self.alerts), 1)

    def test_without_the_adobe_credential_the_frameio_worker_refuses_to_start(self) -> None:
        for name in ADOBE_ENVIRON:
            environ = {key: value for key, value in ADOBE_ENVIRON.items() if key != name}
            with self.subTest(name=name):
                code, message = self.run_main({**FRAMEIO_ENVIRON, **GATEWAY_ENVIRON, **environ})
                self.assertEqual(code, 2)
                self.assertIn(name, message)
        plain = {**ADOBE_ENVIRON, "T6_FRAMEIO_REDIRECT_URI": "http://hooks.example/frameio/callback"}
        code, message = self.run_main({**FRAMEIO_ENVIRON, **GATEWAY_ENVIRON, **plain})
        self.assertEqual((code, "https://" in message), (2, True))

    def test_a_first_start_sends_the_sign_in_link_and_the_callback_stores_a_token(self) -> None:
        sent: list[dict[str, Any]] = []

        class Gateway:
            def __init__(self, base_url: str) -> None:
                pass

            def send(self, method: str, path: str, headers: Any, body: bytes) -> tuple[int, dict[str, Any]]:
                sent.append(json.loads(body))
                return 202, {"message_ref": 1}

        adobe = FakeAdobe()
        token_file = Path(self.directory.name) / "frameio-token.json"
        environ = {**FRAMEIO_ENVIRON, **GATEWAY_ENVIRON, **ADOBE_ENVIRON, "T6_FRAMEIO_TOKEN_FILE": str(token_file)}
        with mock.patch("t6.worker.__main__.HttpTransport", Gateway), mock.patch("t6.frameio.tokens.http_post", adobe):
            tokens, run = frameio_backend(environ)
            worker = frameio_worker(environ, tokens, run)
            self.assertEqual(self.run_main(environ, frameio=worker)[0], 0)
            self.assertEqual(len(sent), 1)
            link = html.unescape(sent[0]["text"]).split("Sign in here: ")[1]
            state = urllib.parse.parse_qs(urllib.parse.urlsplit(link).query)["state"][0]
            self.app.public(Request("GET", f"/frameio/callback?code=code-1&state={state}", {}, b""))
            self.assertEqual(self.run_main(environ, frameio=worker)[0], 0)
        self.assertEqual(json.loads(token_file.read_text())["access_token"], "access-1")
        self.assertEqual(len(sent), 1)

    def test_the_worker_built_from_the_environment_answers_reads_through_the_queue(self) -> None:
        adobe, fake = FakeAdobe(), FakeFrameioApi()
        fake.add_project("fp1", "Example Show")
        fake.add_share("fp1", "s1", "Cut 1")
        token_file = Path(self.directory.name) / "frameio-token.json"
        environ = {**FRAMEIO_ENVIRON, **GATEWAY_ENVIRON, **ADOBE_ENVIRON, "T6_FRAMEIO_TOKEN_FILE": str(token_file)}
        with mock.patch("t6.frameio.tokens.http_post", adobe), mock.patch("t6.frameio.api.http_fetch", fake.fetch):
            tokens, run = frameio_backend(environ)
            tokens.exchange("code-1", urllib.parse.parse_qs(urllib.parse.urlsplit(tokens.sign_in_url()).query)["state"][0])
            worker = Worker(
                QueueClient(InProcess(self.app), FRAMEIO_WORKER_SECRET),
                "frameio",
                FrameioWorker(tokens, run, self.notify, self.clock).handlers(),
            )
            self.worker = worker
            result = self.ask(ShareLinks(frameio_project_id="fp1"))
            feed = self.ask(BriefingFeed(since=SINCE))
        assert isinstance(result, Ok)
        self.assertEqual([link.url for link in decode(ShareLinks, result.value)], ["https://f.io/s1"])
        self.assertEqual(fake.tokens_seen[0], "Bearer access-1")
        assert isinstance(feed, Failed)
        self.assertIn("T6_NOTION_EXTERNAL_LINKS_DATA_SOURCE", feed.reason)

    def test_the_alert_needs_the_gateway_and_a_chat(self) -> None:
        for name in GATEWAY_ENVIRON:
            environ = {key: value for key, value in GATEWAY_ENVIRON.items() if key != name}
            with self.subTest(name=name), self.assertRaisesRegex(ConfigError, name):
                frameio_worker(environ, self.token_store, self.frameio)
        with self.assertRaisesRegex(ConfigError, "T6_FRAMEIO_ALERT_CHAT"):
            frameio_worker({**GATEWAY_ENVIRON, "T6_FRAMEIO_ALERT_CHAT": "cyril"}, self.token_store, self.frameio)

    def test_the_alert_goes_to_the_chat_through_the_gateway_notify_route(self) -> None:
        sent: list[tuple[str, str, dict[str, Any]]] = []

        class Gateway:
            def __init__(self, base_url: str) -> None:
                self.base_url = base_url

            def send(self, method: str, path: str, headers: Any, body: bytes) -> tuple[int, dict[str, Any]]:
                sent.append((method, self.base_url + path, json.loads(body)))
                return 202, {"message_ref": 1}

        self.token_store.refresh_error = SignInRequired("refresh token revoked")
        with mock.patch("t6.worker.__main__.HttpTransport", Gateway):
            frameio_worker(GATEWAY_ENVIRON, self.token_store, self.frameio).tick()
        self.assertEqual(len(sent), 1)
        method, url, body = sent[0]
        self.assertEqual((method, url, body["to"]), ("POST", "http://gateway.invalid/notify", 42))
        self.assertIn("https://sign-in.example/authorize?state=s1", body["text"])
        self.assertTrue(body["idempotency_key"].startswith("frameio-sign-in:"))


if __name__ == "__main__":
    unittest.main()
