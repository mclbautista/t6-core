"""The Frame.io token store against a fake Adobe token endpoint and a fake clock. No real credentials."""

import base64
from collections.abc import Mapping
import json
from pathlib import Path
import stat
import tempfile
from typing import Any
import unittest
import urllib.parse

from t6.frameio import SignInRequired
from t6.frameio.tokens import AUTHORIZE_URL, EXPIRY_MARGIN_SECONDS, TOKEN_URL, AdobeTokens, AdobeUnavailable
from t6.server.testing import FakeClock

CLIENT_ID = "client-1"
CLIENT_SECRET = "client-secret-1"
REDIRECT = "https://hooks.example/frameio/callback"


class FakeAdobe:
    """Stands in for Adobe's token endpoint. Every refresh token works once, as Adobe's do."""

    def __init__(self) -> None:
        self.codes = {"code-1"}
        self.refresh_tokens: set[str] = set()
        self.issued = 0
        self.calls: list[tuple[str, Mapping[str, str], Mapping[str, str]]] = []
        self.down: tuple[int, dict[str, Any]] | Exception | None = None
        self.expires_in = 86399
        self.rotate = True

    def __call__(self, url: str, headers: Mapping[str, str], form: Mapping[str, str]) -> tuple[int, dict[str, Any]]:
        self.calls.append((url, headers, form))
        if isinstance(self.down, Exception):
            raise self.down
        if self.down is not None:
            return self.down
        basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
        if url != TOKEN_URL or headers.get("Authorization") != f"Basic {basic}":
            return 401, {"error": "invalid_client"}
        if form.get("grant_type") == "authorization_code" and form.get("code") in self.codes:
            self.codes.discard(form["code"])
        elif form.get("grant_type") == "refresh_token" and form.get("refresh_token") in self.refresh_tokens:
            if self.rotate:
                self.refresh_tokens.discard(form["refresh_token"])
        else:
            return 400, {"error": "invalid_grant"}
        self.issued += 1
        answer: dict[str, Any] = {
            "access_token": f"access-{self.issued}",
            "token_type": "bearer",
            "expires_in": self.expires_in,
        }
        if self.rotate:
            answer["refresh_token"] = f"refresh-{self.issued}"
            self.refresh_tokens.add(answer["refresh_token"])
        return 200, answer


class TokensTestCase(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "store" / "frameio-token.json"
        self.clock = FakeClock()
        self.adobe = FakeAdobe()
        self.tokens = self.open()

    def open(self) -> AdobeTokens:
        return AdobeTokens(CLIENT_ID, CLIENT_SECRET, REDIRECT, self.path, self.adobe, self.clock)

    def state(self) -> str:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.tokens.sign_in_url()).query)
        return query["state"][0]

    def sign_in(self) -> None:
        self.tokens.exchange("code-1", self.state())


class SignInTests(TokensTestCase):
    def test_the_link_goes_to_adobe_with_offline_access_and_the_callback(self) -> None:
        url = urllib.parse.urlsplit(self.tokens.sign_in_url())
        query = urllib.parse.parse_qs(url.query)
        self.assertEqual(f"{url.scheme}://{url.netloc}{url.path}", AUTHORIZE_URL)
        self.assertEqual(query["client_id"], [CLIENT_ID])
        self.assertEqual(query["redirect_uri"], [REDIRECT])
        self.assertEqual(query["response_type"], ["code"])
        self.assertIn("offline_access", query["scope"][0].split())
        self.assertNotIn(CLIENT_SECRET, self.tokens.sign_in_url())

    def test_the_link_is_the_same_until_a_sign_in_completes_even_after_a_restart(self) -> None:
        first = self.tokens.sign_in_url()
        self.assertEqual(self.tokens.sign_in_url(), first)
        self.assertEqual(self.open().sign_in_url(), first)
        self.sign_in()
        self.assertNotEqual(self.tokens.sign_in_url(), first)

    def test_completing_sign_in_stores_a_token(self) -> None:
        self.sign_in()
        self.assertEqual(self.tokens.access_token(), "access-1")
        self.assertEqual(self.open().access_token(), "access-1")

    def test_a_callback_with_the_wrong_state_is_refused_before_adobe_is_called(self) -> None:
        self.tokens.sign_in_url()
        for state in ("forged", ""):
            with self.subTest(state=state), self.assertRaises(SignInRequired):
                self.tokens.exchange("code-1", state)
        self.assertEqual(self.adobe.calls, [])

    def test_a_callback_nobody_asked_for_is_refused(self) -> None:
        with self.assertRaises(SignInRequired):
            self.tokens.exchange("code-1", "")
        self.assertEqual(self.adobe.calls, [])

    def test_a_used_link_cannot_be_replayed(self) -> None:
        state = self.state()
        self.tokens.exchange("code-1", state)
        self.adobe.codes.add("code-2")
        with self.assertRaises(SignInRequired):
            self.tokens.exchange("code-2", state)

    def test_a_code_adobe_refuses_stores_nothing_and_keeps_the_link(self) -> None:
        link = self.tokens.sign_in_url()
        with self.assertRaisesRegex(SignInRequired, "invalid_grant"):
            self.tokens.exchange("stale-code", self.state())
        with self.assertRaises(SignInRequired):
            self.tokens.access_token()
        self.assertEqual(self.tokens.sign_in_url(), link)

    def test_a_sign_in_without_a_refresh_token_is_refused(self) -> None:
        self.adobe.rotate = False
        with self.assertRaisesRegex(SignInRequired, "offline_access"):
            self.sign_in()


class RefreshTests(TokensTestCase):
    def test_a_refresh_replaces_the_stored_token_and_the_next_one_uses_the_new_refresh_token(self) -> None:
        self.sign_in()
        self.tokens.refresh()
        self.assertEqual(self.tokens.access_token(), "access-2")
        self.open().refresh()  # Adobe took refresh-2: the rotated token was the one stored
        self.assertEqual(self.tokens.access_token(), "access-3")
        self.assertEqual([form.get("refresh_token") for _, _, form in self.adobe.calls[1:]], ["refresh-1", "refresh-2"])

    def test_a_refresh_that_returns_no_new_refresh_token_keeps_the_old_one(self) -> None:
        self.sign_in()
        self.adobe.rotate = False
        self.tokens.refresh()
        self.tokens.refresh()
        self.assertEqual(self.tokens.access_token(), "access-3")

    def test_with_nothing_stored_a_refresh_asks_for_sign_in_without_calling_adobe(self) -> None:
        with self.assertRaises(SignInRequired):
            self.tokens.refresh()
        self.assertEqual(self.adobe.calls, [])

    def test_a_refused_refresh_asks_for_sign_in(self) -> None:
        self.sign_in()
        self.adobe.refresh_tokens.clear()  # revoked, or past its 14 days
        with self.assertRaisesRegex(SignInRequired, "invalid_grant"):
            self.tokens.refresh()

    def test_adobe_being_down_is_not_a_lost_sign_in_and_keeps_the_token(self) -> None:
        self.sign_in()
        for down in ((503, {}), (429, {"error": "rate_limited"}), (200, {}), OSError("network down")):
            self.adobe.down = down
            with self.subTest(down=down), self.assertRaises((AdobeUnavailable, OSError)) as raised:
                self.tokens.refresh()
            self.assertNotIsInstance(raised.exception, SignInRequired)
        self.adobe.down = None
        self.tokens.refresh()
        self.assertEqual(self.tokens.access_token(), "access-2")


class AccessTokenTests(TokensTestCase):
    def test_an_access_token_near_its_end_is_refreshed_before_use(self) -> None:
        self.adobe.expires_in = 3600
        self.sign_in()
        self.clock.advance(3600 - EXPIRY_MARGIN_SECONDS - 1)
        self.assertEqual(self.tokens.access_token(), "access-1")
        self.clock.advance(1)
        self.assertEqual(self.tokens.access_token(), "access-2")
        self.assertEqual(self.tokens.access_token(), "access-2")

    def test_with_nothing_stored_it_asks_for_sign_in(self) -> None:
        with self.assertRaises(SignInRequired):
            self.tokens.access_token()


class StoreTests(TokensTestCase):
    def test_the_token_file_is_private_and_holds_no_client_secret(self) -> None:
        self.sign_in()
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertNotIn(CLIENT_SECRET, self.path.read_text())
        self.assertEqual([entry.name for entry in self.path.parent.iterdir()], [self.path.name])

    def test_a_damaged_token_file_asks_for_sign_in(self) -> None:
        self.path.parent.mkdir(parents=True)
        for content in ("not json", json.dumps(["a list"]), json.dumps({"access_token": 7})):
            self.path.write_text(content)
            with self.subTest(content=content), self.assertRaises(SignInRequired):
                self.tokens.refresh()


if __name__ == "__main__":
    unittest.main()
