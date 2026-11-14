"""Requests to Frame.io and Adobe carry the worker's own User-Agent, never Python's default (#291)."""

from collections.abc import Callable
import json
import unittest
from unittest import mock
import urllib.request

from t6.frameio.api import http_fetch
from t6.frameio.tokens import USER_AGENT, http_post


class FakeResponse:
    status = 200
    headers: dict[str, str] = {}

    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class UserAgentTest(unittest.TestCase):
    def sent(self, call: Callable[[], object], body: bytes) -> urllib.request.Request:
        seen: list[urllib.request.Request] = []

        def urlopen(request: urllib.request.Request, timeout: float) -> FakeResponse:
            seen.append(request)
            return FakeResponse(body)

        with mock.patch("urllib.request.urlopen", urlopen):
            call()
        self.assertEqual(len(seen), 1)
        return seen[0]

    def test_frameio_reads_send_the_worker_agent(self) -> None:
        request = self.sent(lambda: http_fetch("https://api.frame.io/v4/accounts", {"Authorization": "Bearer x"}), b"{}")
        self.assertEqual(request.get_header("User-agent"), USER_AGENT)
        self.assertNotIn("Python-urllib", USER_AGENT)
        self.assertEqual(request.get_header("Authorization"), "Bearer x")

    def test_adobe_token_calls_send_the_worker_agent(self) -> None:
        request = self.sent(lambda: http_post("https://ims.example/token", {"Accept": "application/json"}, {"a": "b"}), json.dumps({}).encode())
        self.assertEqual(request.get_header("User-agent"), USER_AGENT)
        self.assertEqual(request.get_header("Accept"), "application/json")

    def test_a_caller_can_still_set_its_own_agent(self) -> None:
        request = self.sent(lambda: http_fetch("https://api.frame.io/v4/accounts", {"User-Agent": "custom"}), b"{}")
        self.assertEqual(request.get_header("User-agent"), "custom")


if __name__ == "__main__":
    unittest.main()
