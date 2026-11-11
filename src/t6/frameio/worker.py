"""The Frame.io worker: the only process that holds the Frame.io token.

It serves the `frameio` queue. Every other part of T6 gets Frame.io data by
asking it (`t6.frameio.ask`), so the rotating refresh token has one holder.

The token store and the Frame.io API are passed in:

- `Tokens` is the token store, sign-in exchange and refresh (t6-core#15).
- `run` executes one typed request against the Frame.io API (t6-core#16).

Call `tick()` between jobs. It refreshes the token once a day. When Frame.io
refuses the token, one sign-in alert goes out through `notify` and every
request is refused with a clear reason until a `frameio.sign_in` job is
exchanged.
"""

from __future__ import annotations

from collections.abc import Callable
import hashlib
import time
from typing import Protocol

from t6.domain.events import JsonValue
from t6.frameio.requests import REQUEST_KINDS, SIGN_IN_KIND, FrameioRequest, from_job
from t6.worker.client import WorkerJob
from t6.worker.runner import Handler, Refused

REFRESH_SECONDS = 24 * 3600.0
REFRESH_RETRY_SECONDS = 15 * 60.0
SIGN_IN_NEEDED = "Frame.io sign-in needed"


class SignInRequired(Exception):
    """Frame.io refused the token or the sign-in, or there is no token. A person must sign in."""


class Tokens(Protocol):
    """The token store. Only the Frame.io worker has one."""

    def sign_in_url(self) -> str:
        """The link a person opens to sign in."""
        ...

    def exchange(self, code: str, state: str) -> None:
        """Trade the callback's authorization code for a stored token. Raises SignInRequired when refused."""
        ...

    def refresh(self) -> None:
        """Replace the stored token. Raises SignInRequired when refused; any other error is worth a retry."""
        ...


Run = Callable[[FrameioRequest], JsonValue]
"""Runs one request against Frame.io. Raises SignInRequired when the token is refused."""

Notify = Callable[[str, str], None]
"""Sends `text` to Telegram through the gateway's `/notify`, once per idempotency `key`."""


class FrameioWorker:
    def __init__(
        self, tokens: Tokens, run: Run, notify: Notify, clock: Callable[[], float] = time.time
    ) -> None:
        self._tokens = tokens
        self._run = run
        self._notify = notify
        self._clock = clock
        self._signed_in = True
        self._alert_pending = False
        self._refresh_at = 0.0  # the first tick refreshes

    def handlers(self) -> dict[str, Handler]:
        return {**{kind: self._request for kind in REQUEST_KINDS}, SIGN_IN_KIND: self._sign_in}

    def tick(self) -> None:
        """Refresh the token when it is due, and retry an alert that did not go out."""
        now = self._clock()
        if self._signed_in and now >= self._refresh_at:
            try:
                self._tokens.refresh()
            except SignInRequired:
                self._signed_out()
            except Exception:  # noqa: BLE001 - Adobe unreachable is not a lost sign-in
                self._refresh_at = now + REFRESH_RETRY_SECONDS
            else:
                self._refresh_at = now + REFRESH_SECONDS
        if self._alert_pending:
            self._alert()

    def _signed_out(self) -> None:
        if self._signed_in:
            self._signed_in = False
            self._alert()

    def _alert(self) -> None:
        self._alert_pending = True
        try:
            url = self._tokens.sign_in_url()
            # Keyed by the link, so a restart while signed out does not send it twice.
            key = "frameio-sign-in:" + hashlib.sha256(url.encode()).hexdigest()[:16]
            self._notify(f"{SIGN_IN_NEEDED}. Sign in here: {url}", key)
        except Exception:  # noqa: BLE001 - the next tick tries again
            return
        self._alert_pending = False

    def _request(self, job: WorkerJob) -> JsonValue:
        if not self._signed_in:
            raise Refused(SIGN_IN_NEEDED)
        try:
            request = from_job(job.kind, job.payload)
        except (ValueError, TypeError) as error:
            raise Refused(f"bad request: {error}") from error
        try:
            return self._run(request)
        except SignInRequired:
            self._signed_out()
            raise Refused(SIGN_IN_NEEDED) from None
        except Exception as error:  # noqa: BLE001 - the caller gets a failed result, never a retry loop
            raise Refused(f"{type(error).__name__}: {error}") from error

    def _sign_in(self, job: WorkerJob) -> JsonValue:
        code, state = job.payload.get("code"), job.payload.get("state")
        if not isinstance(code, str) or not isinstance(state, str):
            raise Refused("sign-in job without code and state")
        try:
            self._tokens.exchange(code, state)
        except SignInRequired as error:
            raise Refused(f"sign-in refused: {error}") from error
        self._signed_in = True
        self._alert_pending = False
        self._refresh_at = self._clock() + REFRESH_SECONDS
        return {"signed_in": True}
