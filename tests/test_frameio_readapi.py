"""The Frame.io read API and its token-free client, against the real worker on a fake Frame.io. No real client data."""

from collections.abc import Mapping
from datetime import datetime, timezone
import json
import threading
from typing import Any
import unittest

from t6.frameio import (
    Failed,
    FrameioReader,
    FrameioWorker,
    SignInNeeded,
    SignInRequired,
)
from t6.frameio.api import FrameioApi
from t6.frameio.model import Approval, BriefingShow, Comment, FrameioProject, ShareLink
from t6.frameio.readapi import ReadApi
from t6.frameio.reads import FrameioReads
from t6.frameio.testing import FakeFrameio, StaticTokens
from t6.notion.external_links import ExternalLinks
from t6.notion.testing import FakeNotion
from t6.server.app import Request
from t6.server.http import make_server
from t6.worker.__main__ import ConfigError, read_api_server
from t6.worker.client import HttpTransport

SECRET = "read-secret"
NOW = datetime(2026, 11, 8, 8, 0, tzinfo=timezone.utc)
SINCE = datetime(2026, 11, 1, 8, 0, tzinfo=timezone.utc)


class FakeTokens:
    def __init__(self) -> None:
        self.refresh_error: Exception | None = None
        self.refreshes = 0

    def sign_in_url(self) -> str:
        return "https://sign-in.example/authorize?state=s1"

    def exchange(self, code: str, state: str) -> None:
        raise AssertionError("the read API never signs in")

    def refresh(self) -> None:
        if self.refresh_error is not None:
            raise self.refresh_error
        self.refreshes += 1


class InProcess:
    """A client transport that calls the read API directly. No sockets."""

    def __init__(self, api: ReadApi) -> None:
        self.api = api

    def send(self, method: str, path: str, headers: Mapping[str, str], body: bytes) -> tuple[int, dict[str, Any]]:
        answer = self.api.handle(Request(method, path, dict(headers), body))
        return answer.status, json.loads(json.dumps(dict(answer.body)))


class ReadApiTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.frameio = FakeFrameio()
        self.frameio.add_project("fp1", "Example Show")
        self.frameio.add_share("fp1", "s1", "Cut 1", last_viewed_at="2026-11-03T10:00:00Z")
        self.frameio.add_file("f1", "Cut 1.mov")
        self.frameio.add_comment("f1", "c1", "Example comment")
        self.frameio.set_status("f1", "Approved")
        self.notion = FakeNotion()
        self.notion.add_data_source("ds-links")
        client = self.notion.client()
        self.addCleanup(client.close)
        reads = FrameioReads(
            FrameioApi(StaticTokens(), self.frameio.fetch, lambda _: None),
            ExternalLinks(client, "ds-links"),
            now=lambda: NOW,
        )
        self.alerts: list[tuple[str, str]] = []
        self.tokens = FakeTokens()
        self.worker = FrameioWorker(self.tokens, reads, lambda text, key: self.alerts.append((text, key)))
        self.api = ReadApi(self.worker, SECRET)
        self.reader = FrameioReader(InProcess(self.api), SECRET)

    def call(self, method: str, path: str, secret: str | None = SECRET) -> tuple[int, Mapping[str, Any]]:
        headers = {} if secret is None else {"Authorization": f"Bearer {secret}"}
        answer = self.api.handle(Request(method, path, headers, b""))
        return answer.status, answer.body


class ReaderTests(ReadApiTestCase):
    def test_every_read_comes_back_typed(self) -> None:
        projects = self.reader.projects()
        self.assertIsInstance(projects, tuple)
        assert not isinstance(projects, Failed)
        self.assertEqual([FrameioProject] * 1, [type(project) for project in projects])
        self.assertEqual(projects[0].name, "Example Show")

        links = self.reader.share_links("fp1")
        assert not isinstance(links, Failed)
        self.assertEqual([(type(link), link.id) for link in links], [(ShareLink, "s1")])

        comments = self.reader.comments("f1")
        assert not isinstance(comments, Failed)
        self.assertEqual([(type(c), c.id) for c in comments], [(Comment, "c1")])

        approvals = self.reader.approvals("f1")
        assert not isinstance(approvals, Failed)
        self.assertEqual([(type(a), a.values) for a in approvals], [(Approval, ("Approved",))])

        feed = self.reader.briefing(SINCE)
        assert not isinstance(feed, Failed)
        self.assertEqual([(type(show), show.frameio_project_id) for show in feed], [(BriefingShow, "fp1")])

    def test_status_says_whether_the_worker_is_signed_in(self) -> None:
        self.assertIs(self.reader.signed_in(), True)

    def test_a_signed_out_worker_is_a_typed_sign_in_failure(self) -> None:
        self.tokens.refresh_error = SignInRequired("Adobe refused")
        self.worker.tick()
        for result in (self.reader.projects(), self.reader.briefing(SINCE), self.reader.comments("f1")):
            self.assertIsInstance(result, SignInNeeded)
        self.assertIs(self.reader.signed_in(), False)
        self.assertEqual(len(self.alerts), 1)

    def test_a_refused_token_during_a_read_signs_the_worker_out(self) -> None:
        def refuse(_request: object) -> None:
            raise SignInRequired("Frame.io refused the token")

        worker = FrameioWorker(self.tokens, refuse, lambda text, key: self.alerts.append((text, key)))
        reader = FrameioReader(InProcess(ReadApi(worker, SECRET)), SECRET)
        self.assertIsInstance(reader.projects(), SignInNeeded)
        self.assertIs(reader.signed_in(), False)
        self.assertEqual(len(self.alerts), 1)

    def test_a_frameio_error_is_a_failure_with_a_reason(self) -> None:
        self.frameio.refuse_next(500, times=10)
        result = self.reader.projects()
        self.assertIsInstance(result, Failed)
        self.assertNotIsInstance(result, SignInNeeded)

    def test_a_wrong_secret_gets_nothing(self) -> None:
        result = FrameioReader(InProcess(self.api), "wrong").projects()
        self.assertIsInstance(result, Failed)
        self.assertIn("401", result.reason)  # type: ignore[union-attr]

    def test_an_unreachable_api_is_a_failure_not_an_exception(self) -> None:
        class Down:
            def send(self, *_args: object) -> tuple[int, dict[str, Any]]:
                raise ConnectionRefusedError("no route")

        self.assertIsInstance(FrameioReader(Down(), SECRET).projects(), Failed)

    def test_a_briefing_time_needs_a_zone(self) -> None:
        self.assertIsInstance(self.reader.briefing(datetime(2026, 11, 1)), Failed)


class RouteTests(ReadApiTestCase):
    def test_no_secret_or_a_wrong_secret_is_401_on_every_route(self) -> None:
        for path in ("/frameio/status", "/frameio/projects", "/frameio/token", "/other"):
            self.assertEqual(self.call("GET", path, None)[0], 401)
            self.assertEqual(self.call("GET", path, "wrong")[0], 401)

    def test_nothing_but_get_is_accepted(self) -> None:
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            for path in ("/frameio/projects", "/frameio/status", "/frameio/files/f1/comments"):
                self.assertEqual(self.call(method, path)[0], 405)
        self.assertEqual(self.frameio.requests, [])

    def test_no_route_gives_the_token_or_anything_unlisted(self) -> None:
        for path in ("/frameio/token", "/frameio/tokens", "/frameio/sign-in", "/frameio/files/f1", "/frameio/projects/fp1",
                     "/frameio/projects/fp1/comments", "/frameio/files/f1/versions", "/frameio/", "/", "/queue/claim"):
            status, body = self.call("GET", path)
            self.assertEqual(status, 404, path)
            self.assertEqual(body, {"error": "not found"})
        self.assertEqual(self.frameio.requests, [])

    def test_answers_never_hold_a_token_even_when_frameio_fails(self) -> None:
        self.frameio.refuse_next(500, times=10)
        for path in ("/frameio/status", "/frameio/projects", "/frameio/briefing?since=2026-11-01T00:00:00%2B00:00"):
            text = json.dumps(dict(self.call("GET", path)[1]))
            self.assertNotIn("access-", text)
            self.assertNotIn("Bearer", text)

    def test_a_bad_briefing_time_is_400(self) -> None:
        for path in ("/frameio/briefing", "/frameio/briefing?since=soon", "/frameio/briefing?since=2026-11-01T00:00:00"):
            self.assertEqual(self.call("GET", path)[0], 400, path)

    def test_an_id_cannot_name_another_route(self) -> None:
        self.call("GET", "/frameio/files/f1%2Fmetadata/comments")
        self.assertTrue(all("%2F" in path or "metadata" not in path for path in self.frameio.requests))


class SocketTests(ReadApiTestCase):
    def test_the_client_reads_over_a_real_socket(self) -> None:
        server = make_server(("127.0.0.1", 0), self.api.handle)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_address[1]}"
        reader = FrameioReader(HttpTransport(url), SECRET)
        projects = reader.projects()
        assert not isinstance(projects, Failed)
        self.assertEqual([project.id for project in projects], ["fp1"])
        self.assertIsInstance(FrameioReader(HttpTransport(url), "wrong").projects(), Failed)


class ConfigTests(ReadApiTestCase):
    def test_the_read_api_is_off_without_a_bind_address(self) -> None:
        self.assertIsNone(read_api_server({}, self.worker))

    def test_a_bind_address_needs_a_secret_and_a_port(self) -> None:
        with self.assertRaises(ConfigError):
            read_api_server({"T6_FRAMEIO_READ_BIND": "127.0.0.1:0"}, self.worker)
        with self.assertRaises(ConfigError):
            read_api_server({"T6_FRAMEIO_READ_BIND": "nonsense", "T6_FRAMEIO_READ_SECRET": SECRET}, self.worker)

    def test_a_bind_address_and_secret_make_a_server(self) -> None:
        server = read_api_server({"T6_FRAMEIO_READ_BIND": "127.0.0.1:0", "T6_FRAMEIO_READ_SECRET": SECRET}, self.worker)
        assert server is not None
        server.server_close()


if __name__ == "__main__":
    unittest.main()
