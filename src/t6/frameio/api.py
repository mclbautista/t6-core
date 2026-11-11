"""Frame.io V4 over HTTPS, for the Frame.io worker only. Standard library only.

`FrameioApi.get` and `.pages` send the worker's access token. A 401 gets one
token refresh and one more try; a second 401 is `SignInRequired`. A 429 waits
and tries again (Frame.io allows 100 calls a minute on most routes).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
import json
import time
from typing import Any, Protocol
import urllib.error
import urllib.parse
import urllib.request

from t6.frameio.worker import SignInRequired

BASE_URL = "https://api.frame.io"
PAGE_SIZE = 100
RATE_LIMIT_TRIES = 5
RATE_LIMIT_WAIT_SECONDS = 15.0

Fetch = Callable[[str, Mapping[str, str]], tuple[int, Mapping[str, str], dict[str, Any]]]
"""GETs a URL with headers. Returns the status, the response headers and the JSON object answered."""


class AccessTokens(Protocol):
    def access_token(self) -> str: ...

    def refresh(self) -> None: ...


class FrameioError(Exception):
    """Frame.io answered something other than the data asked for."""

    def __init__(self, status: int, path: str) -> None:
        super().__init__(f"Frame.io answered {status} for {path}")
        self.status = status


def http_fetch(url: str, headers: Mapping[str, str]) -> tuple[int, Mapping[str, str], dict[str, Any]]:
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        with urllib.request.urlopen(request, timeout=30.0) as response:
            status, answered, raw = response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        status, answered, raw = error.code, dict(error.headers), error.read()
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        body = {}
    return status, answered, body if isinstance(body, dict) else {}


class FrameioApi:
    def __init__(
        self, tokens: AccessTokens, fetch: Fetch | None = None, sleep: Callable[[float], None] = time.sleep
    ) -> None:
        self._tokens = tokens
        self._fetch = fetch or http_fetch
        self._sleep = sleep

    def get(self, path: str, params: Mapping[str, str | int] | None = None) -> dict[str, Any]:
        """The JSON object at `path` (a path that starts with `/v4/`, with or without its own query)."""
        if params:
            path += ("&" if "?" in path else "?") + urllib.parse.urlencode(params)
        refreshed = False
        waits = 0
        while True:
            headers = {"Authorization": f"Bearer {self._tokens.access_token()}", "Accept": "application/json"}
            status, answered, body = self._fetch(BASE_URL + path, headers)
            if status == 200:
                return body
            shown = path.split("?")[0]
            if status == 401:
                if refreshed:
                    raise SignInRequired("Frame.io refused a fresh token")
                self._tokens.refresh()
                refreshed = True
            elif status == 429 and waits < RATE_LIMIT_TRIES:
                waits += 1
                self._sleep(_retry_after(answered))
            else:
                raise FrameioError(status, shown)

    def pages(self, path: str, params: Mapping[str, str | int] | None = None) -> Iterator[dict[str, Any]]:
        """Every item of a paged list, following `links.next` to the end."""
        seen: set[str] = set()
        body = self.get(path, {"page_size": PAGE_SIZE, **(params or {})})
        while True:
            for item in body.get("data") or ():
                if isinstance(item, dict):
                    yield item
            links = body.get("links")
            following = links.get("next") if isinstance(links, dict) else None
            if not isinstance(following, str) or not following.startswith("/") or following in seen:
                return
            seen.add(following)
            body = self.get(following)


def _retry_after(headers: Mapping[str, str]) -> float:
    for name, value in headers.items():
        if name.lower() == "retry-after":
            try:
                return min(max(float(value), 1.0), 60.0)
            except ValueError:
                break
    return RATE_LIMIT_WAIT_SECONDS
