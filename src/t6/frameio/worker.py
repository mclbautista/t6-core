"""The Frame.io worker: the only process that holds the Frame.io token.

It serves the `frameio` queue. Every other part of T6 gets Frame.io data by
asking it (`t6.frameio.ask`), so the rotating refresh token has one holder.

The token store and the Frame.io API are passed in:

- `Tokens` is the token store, sign-in exchange and refresh (t6-core#15).
- `run` executes one typed request against the Frame.io API (t6-core#16).

Call `tick()` between jobs. It refreshes the token once a day. When Frame.io
refuses the token, one sign-in alert goes out through `notify` and every
request is failed with a clear reason until a `frameio.sign_in` job is
exchanged.
"""

from __future__ import annotations

from collections.abc import Callable
import hashlib
import threading
import time
from typing import Protocol

from t6.domain.events import JobKind, JsonValue
from t6.frameio.model import ResolvedComment
from t6.frameio.requests import REQUEST_KINDS, FrameioRequest, ResolveComment, from_job
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

ArmReviewTimer = Callable[[ResolvedComment], None]
"""Starts or restarts the review timer for the comment's (file, author) on the server."""

Notify = Callable[[str, str], None]
"""Sends `text` to Telegram through the gateway's `/notify`, once per idempotency `key`."""


class FrameioWorker:
    def __init__(
        self,
        tokens: Tokens,
        run: Run,
        notify: Notify,
        clock: Callable[[], float] = time.time,
        arm_review_timer: ArmReviewTimer | None = None,
    ) -> None:
        self._tokens = tokens
        self._run = run
        self._notify = notify
        self._clock = clock
        self._arm_review_timer = arm_review_timer
        self._lock = threading.Lock()  # the read API runs on other threads; Adobe rotates the refresh token on each use
        self._signed_in = True
        self._alert_pending = False
        self._refresh_at = 0.0  # the first tick refreshes

    def handlers(self) -> dict[str, Handler]:
        return {
            **{kind: self._request for kind in REQUEST_KINDS},
            JobKind.FRAMEIO_RESOLVE_COMMENT.value: self._resolve_comment,
            JobKind.FRAMEIO_SIGN_IN.value: self._sign_in,
        }

    def tick(self) -> None:
        """Refresh the token when it is due, and retry an alert that did not go out."""
        now = self._clock()
        if self._signed_in and now >= self._refresh_at:
            try:
                with self._lock:
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
            # Keyed by the link and the day: a restart while signed out does not send
            # it twice, and a later sign-out with the same link still gets its alert.
            day = int(self._clock() // 86400)
            key = f"frameio-sign-in:{day}:" + hashlib.sha256(url.encode()).hexdigest()[:16]
            self._notify(f"{SIGN_IN_NEEDED}. Sign in here: {url}", key)
        except Exception:  # noqa: BLE001 - the next tick tries again
            return
        self._alert_pending = False

    @property
    def signed_in(self) -> bool:
        return self._signed_in

    def read(self, request: FrameioRequest) -> JsonValue:
        """Run one read for the read API (`t6.frameio.readapi`) on the caller's thread.

        Raises SignInRequired when there is no usable token; any other error is
        Frame.io's or the request's and goes to the caller.
        """
        if not self._signed_in:
            raise SignInRequired(SIGN_IN_NEEDED)
        try:
            return self._execute(request)
        except SignInRequired:
            self._signed_out()
            raise

    def _execute(self, request: FrameioRequest) -> JsonValue:
        with self._lock:
            return self._run(request)

    def _request(self, job: WorkerJob) -> JsonValue:
        if not self._signed_in:
            raise Refused(SIGN_IN_NEEDED)
        try:
            request = from_job(job.kind, job.payload)
        except (ValueError, TypeError) as error:
            raise Refused(f"bad request: {error}") from error
        try:
            return self._execute(request)
        except SignInRequired:
            self._signed_out()
            raise Refused(SIGN_IN_NEEDED) from None
        except Exception as error:  # noqa: BLE001 - the caller gets a failed result, never a retry loop
            raise Refused(f"{type(error).__name__}: {error}") from error

    def _resolve_comment(self, job: WorkerJob) -> JsonValue:
        """Read the comment a webhook named and start its (file, author) review timer.

        Unlike other requests, a failure here is raised for a retry: nobody is
        waiting on an answer and the comment must not be lost. Arming is
        idempotent per comment, so running this twice counts it once.
        """
        if self._arm_review_timer is None:
            raise Refused("this Frame.io worker has no way to start review timers")
        try:
            request = from_job(job.kind, job.payload)
        except (ValueError, TypeError) as error:
            raise Refused(f"bad request: {error}") from error
        assert isinstance(request, ResolveComment)
        if not self._signed_in:
            raise RuntimeError(SIGN_IN_NEEDED)
        try:
            found = self._execute(request)
        except SignInRequired:
            self._signed_out()
            raise RuntimeError(SIGN_IN_NEEDED) from None
        except ValueError as error:  # the comment has no file or author: asking again will not help
            raise Refused(f"unusable comment: {error}") from error
        if not isinstance(found, dict):
            raise Refused("Frame.io gave no comment")
        try:
            comment = ResolvedComment.from_json(found)
        except (ValueError, TypeError) as error:
            raise Refused(f"unusable comment: {error}") from error
        self._arm_review_timer(comment)
        return {"file_id": comment.file_id, "author_id": comment.author_id}

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
