"""The Frame.io token store: Adobe sign-in, code exchange and refresh. Standard library only.

Only the Frame.io worker makes an `AdobeTokens`. The token file is its alone:
Adobe replaces the refresh token on every refresh, so a second reader that
refreshed would sign the first one out.

The file holds the access token, the refresh token, when the access token
expires, and the `state` of a sign-in that is waiting for its callback. It is
written with mode 0600 and replaced in one step, so a crash never leaves half
a token.
"""

from __future__ import annotations

import base64
from collections.abc import Callable, Mapping
import hmac
import json
import os
from pathlib import Path
import secrets
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

from t6.frameio.worker import SignInRequired

AUTHORIZE_URL = "https://ims-na1.adobelogin.com/ims/authorize/v2"
TOKEN_URL = "https://ims-na1.adobelogin.com/ims/token/v3"
SCOPE = "openid email profile offline_access additional_info.roles"
EXPIRY_MARGIN_SECONDS = 300.0
"""An access token this close to its end is replaced before it is used."""

USER_AGENT = "t6-frameio-worker"
"""Sent on every request to Frame.io and Adobe. The default `Python-urllib` agent is refused with a 403 HTML page (#291)."""

Post = Callable[[str, Mapping[str, str], Mapping[str, str]], tuple[int, dict[str, Any]]]
"""Sends a form to a URL with headers. Returns the status and the JSON object answered."""


class AdobeUnavailable(Exception):
    """Adobe did not give a usable answer (a 5xx, a 429, not JSON). Worth a retry; the sign-in may be fine."""


def http_post(url: str, headers: Mapping[str, str], form: Mapping[str, str]) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        url, data=urllib.parse.urlencode(form).encode(), headers={"User-Agent": USER_AGENT, **headers}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=30.0) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read()
    try:
        answer = json.loads(raw or b"{}")
    except ValueError:
        answer = None
    if not isinstance(answer, dict):
        raise AdobeUnavailable(f"Adobe answered {status} without a JSON object")
    return status, answer


class AdobeTokens:
    """Adobe OAuth Web App sign-in for Frame.io V4, with the token kept in one file.

    It is the `Tokens` the Frame.io worker needs, plus `access_token()` for
    the Frame.io API calls.
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        path: Path,
        post: Post | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._client_id = client_id
        self._redirect_uri = redirect_uri
        self._path = path
        self._post = post or http_post
        self._clock = clock
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        self._headers = {
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }

    def sign_in_url(self) -> str:
        """The link a person opens to sign in. It stays the same until a sign-in completes."""
        stored = self._load()
        state = stored.get("state")
        if not isinstance(state, str) or not state:
            state = secrets.token_urlsafe(32)
            self._save({**stored, "state": state})
        query = {
            "client_id": self._client_id,
            "redirect_uri": self._redirect_uri,
            "scope": SCOPE,
            "response_type": "code",
            "state": state,
        }
        return AUTHORIZE_URL + "?" + urllib.parse.urlencode(query)

    def exchange(self, code: str, state: str) -> None:
        """Trade the callback's authorization code for a stored token.

        The state must be the one the last sign-in link carried, so a callback
        nobody asked for is refused before Adobe is called.
        """
        expected = self._load().get("state")
        if not isinstance(expected, str) or not hmac.compare_digest(expected.encode(), state.encode()):
            raise SignInRequired("the callback's state does not match a sign-in link")
        answer = self._grant({"grant_type": "authorization_code", "code": code})
        if not isinstance(answer.get("refresh_token"), str):
            raise SignInRequired("Adobe gave no refresh token (the credential needs the offline_access scope)")
        self._store(answer, {})

    def refresh(self) -> None:
        """Replace the stored token with a new one from Adobe."""
        stored = self._load()
        refresh_token = stored.get("refresh_token")
        if not isinstance(refresh_token, str):
            raise SignInRequired("no Frame.io token is stored")
        self._store(self._grant({"grant_type": "refresh_token", "refresh_token": refresh_token}), stored)

    def access_token(self) -> str:
        """An access token that is good now. Refreshes first when the stored one is at its end."""
        stored = self._load()
        token, expires_at = stored.get("access_token"), stored.get("expires_at")
        if not isinstance(token, str) or not isinstance(expires_at, (int, float)):
            raise SignInRequired("no Frame.io token is stored")
        if self._clock() < expires_at - EXPIRY_MARGIN_SECONDS:
            return token
        self.refresh()
        return str(self._load()["access_token"])

    def _grant(self, form: Mapping[str, str]) -> dict[str, Any]:
        status, answer = self._post(TOKEN_URL, self._headers, form)
        if status in {400, 401, 403}:
            raise SignInRequired(f"Adobe refused: {answer.get('error', status)}")
        if status != 200:
            raise AdobeUnavailable(f"Adobe answered {status}")
        if not isinstance(answer.get("access_token"), str):
            raise AdobeUnavailable("Adobe answered 200 without an access token")
        return answer

    def _store(self, answer: Mapping[str, Any], previous: Mapping[str, Any]) -> None:
        """Keep the answered token. Without a new refresh token the old one stays; the sign-in state is used up."""
        expires_in = answer.get("expires_in")
        lifetime = float(expires_in) if isinstance(expires_in, (int, float)) else 0.0
        refresh_token = answer.get("refresh_token")
        self._save(
            {
                "access_token": answer["access_token"],
                "refresh_token": refresh_token if isinstance(refresh_token, str) else previous.get("refresh_token"),
                "expires_at": self._clock() + lifetime,
            }
        )

    def _load(self) -> dict[str, Any]:
        try:
            stored = json.loads(self._path.read_text())
        except (FileNotFoundError, ValueError):
            return {}
        return stored if isinstance(stored, dict) else {}

    def _save(self, stored: Mapping[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        scratch = self._path.with_name(self._path.name + ".new")
        descriptor = os.open(scratch, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            json.dump(stored, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(scratch, self._path)
