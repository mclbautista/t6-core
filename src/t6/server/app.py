"""The webhook server's request handling, with no sockets (see `t6.server.http`).

Two listeners, two entry points:

- `WebhookApp.public(request)` is the internet-facing path. It serves only
  `/webhook` and `/webhook/*` (Frame.io and Notion). Everything else is 404, so
  the tap and queue routes are not reachable from the internet.
- `WebhookApp.tailnet(request)` is for tailnet peers only. It serves the tap
  endpoint (`POST /tap`, the gateway) and the queue routes (`/queue/*`,
  workers and callers).

The server only answers requests. It never connects to a worker.

Payload shapes below follow the provider documentation as summarised in
t6-core#13 and are NOT yet checked against live deliveries:
Frame.io `{"type": "comment.created", "resource": {"id": ...}, "project": {"id": ...}}`
and Notion `{"id", "timestamp", "type": "page.properties_updated",
"entity": {"id", "type": "page"}, "data": {"updated_properties": [...]}}`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import hmac
import json
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

from t6.domain.events import (
    EventSource,
    FrameioCommentCreated,
    Job,
    JobKind,
    JsonValue,
)
from t6.server.queue import FRAMEIO_QUEUE, MAIN_QUEUE, JobQueue
from t6.server.signatures import WebhookRejected, verify_frameio, verify_notion

REVIEW_QUIET_SECONDS = 15 * 60.0
DEFAULT_LEASE_SECONDS = 60.0

__all__ = ["Grant", "Request", "Response", "ServerConfig", "WebhookApp"]


@dataclass(frozen=True, slots=True)
class Request:
    method: str
    path: str
    """Path and optional query string, as received."""
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""

    def headers_lower(self) -> dict[str, str]:
        return {key.lower(): value for key, value in self.headers.items()}

    def header(self, name: str) -> str:
        return self.headers_lower().get(name, "")


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Mapping[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Grant:
    """What one bearer secret may do on the queue routes."""

    secret: str
    claim: frozenset[str] = frozenset()
    """Queues this holder may claim from, ack and fail."""
    submit: frozenset[str] = frozenset()
    """Queues this holder may submit to and read results from."""


@dataclass(frozen=True, slots=True)
class ServerConfig:
    """Secrets, from the environment. An empty secret turns that route off (503)."""

    frameio_webhook_secret: str = ""
    notion_webhook_token: str = ""
    tap_secret: str = ""
    grants: tuple[Grant, ...] = ()
    review_quiet_seconds: float = REVIEW_QUIET_SECONDS

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> ServerConfig:
        main, frameio = frozenset({MAIN_QUEUE}), frozenset({FRAMEIO_QUEUE})
        grants: list[Grant] = []
        if environ.get("T6_WORKER_SECRET"):
            grants.append(Grant(environ["T6_WORKER_SECRET"], claim=main, submit=main | frameio))
        if environ.get("T6_FRAMEIO_WORKER_SECRET"):
            grants.append(Grant(environ["T6_FRAMEIO_WORKER_SECRET"], claim=frameio))
        return cls(
            frameio_webhook_secret=environ.get("T6_FRAMEIO_WEBHOOK_SECRET", ""),
            notion_webhook_token=environ.get("T6_NOTION_WEBHOOK_TOKEN", ""),
            tap_secret=environ.get("T6_TAP_SECRET", ""),
            grants=tuple(grants),
        )


def _same(a: str, b: str) -> bool:
    return bool(a) and hmac.compare_digest(a.encode(), b.encode())


def _bearer(request: Request) -> str:
    value = request.header("authorization")
    return value[7:] if value.lower().startswith("bearer ") else ""


class WebhookApp:
    def __init__(
        self,
        queue: JobQueue,
        config: ServerConfig,
        clock: Callable[[], float] = time.time,
        on_notion_verification: Callable[[str], None] = lambda token: None,
    ) -> None:
        self._queue = queue
        self._config = config
        self._clock = clock
        self._on_notion_verification = on_notion_verification

    # -- public listener --------------------------------------------------------

    def public(self, request: Request) -> Response:
        path = urlsplit(request.path).path
        if path != "/webhook" and not path.startswith("/webhook/"):
            return Response(404)
        if request.method != "POST":
            return Response(405)
        try:
            if request.header("x-frameio-signature"):
                return self._frameio(request)
            if request.header("x-notion-signature") or b"verification_token" in request.body:
                return self._notion(request)
        except WebhookRejected as rejection:
            return Response(401, {"error": rejection.reason})
        return Response(401, {"error": "unknown sender"})

    def _frameio(self, request: Request) -> Response:
        if not self._config.frameio_webhook_secret:
            return Response(503, {"error": "frameio webhook secret not configured"})
        now = self._clock()
        verify_frameio(self._config.frameio_webhook_secret, request.headers_lower(), request.body, now)
        body = _json_object(request.body)
        if body is None:
            return Response(400, {"error": "body is not a JSON object"})
        if body.get("type") != "comment.created":
            return Response(200, {"ignored": str(body.get("type"))})
        comment_id = _get(body, "resource", "id")
        project_id = _get(body, "project", "id")
        if not comment_id or not project_id:
            return Response(400, {"error": "comment event without ids"})
        event = FrameioCommentCreated(
            comment_id=comment_id,
            frameio_project_id=project_id,
            source=EventSource.WEBHOOK,
        )
        # First comment starts the review timer, each comment resets it.
        counted = self._queue.arm_timer(
            MAIN_QUEUE,
            f"frameio.review:{project_id}",
            JobKind.FRAMEIO_REVIEW_DONE.value,
            self._config.review_quiet_seconds,
            {"frameio_project_id": project_id},
            event_key=event.dedupe_key,
        )
        return Response(200, {"counted": counted})

    def _notion(self, request: Request) -> Response:
        body = _json_object(request.body)
        if not request.header("x-notion-signature") and body and "verification_token" in body:
            # Setup handshake: Notion sends its token once and the operator stores it as a secret.
            self._on_notion_verification(str(body["verification_token"]))
            return Response(200)
        if not self._config.notion_webhook_token:
            return Response(503, {"error": "notion webhook token not configured"})
        verify_notion(self._config.notion_webhook_token, request.headers_lower(), request.body, self._clock())
        if body is None:
            return Response(400, {"error": "body is not a JSON object"})
        if body.get("type") != "page.properties_updated":
            return Response(200, {"ignored": str(body.get("type"))})
        event_id, page_id = body.get("id"), _get(body, "entity", "id")
        if not isinstance(event_id, str) or not page_id:
            return Response(400, {"error": "page event without ids"})
        properties = _get(body, "data", "updated_properties", default=[])
        job = Job(
            kind=JobKind.NOTION_PAGE_CHANGED,
            dedupe_key=f"notion:event:{event_id}",
            payload={
                "page_id": page_id,
                "updated_properties": properties if isinstance(properties, list) else [],
                "event_id": event_id,
                "timestamp": body.get("timestamp") if isinstance(body.get("timestamp"), str) else None,
            },
        )
        made = self._queue.submit(MAIN_QUEUE, job.kind.value, job.dedupe_key, job.payload)
        return Response(200, {"job_id": made.job_id, "created": made.created})

    # -- tailnet listener ---------------------------------------------------------

    def tailnet(self, request: Request) -> Response:
        parts = urlsplit(request.path)
        if parts.path == "/tap":
            return self._tap(request)
        if parts.path.startswith("/queue/"):
            return self._queue_route(parts.path[len("/queue/") :], parts.query, request)
        return Response(404)

    def _tap(self, request: Request) -> Response:
        if request.method != "POST":
            return Response(405)
        if not self._config.tap_secret:
            return Response(503, {"error": "tap secret not configured"})
        if not _same(self._config.tap_secret, _bearer(request)):
            return Response(401, {"error": "bad secret"})
        body = _json_object(request.body)
        key = request.header("idempotency-key") or (body or {}).get("idempotency_key")
        if body is None or not isinstance(key, str) or not key.strip():
            return Response(400, {"error": "tap needs a JSON body and an idempotency key"})
        sender = body.get("from")
        job = Job(
            kind=JobKind.TAP,
            dedupe_key=f"tap:{key}",
            payload={
                "data": body.get("data") if isinstance(body.get("data"), str) else "",
                "from_id": sender.get("id") if isinstance(sender, dict) else None,
                "chat": body.get("chat"),
                "message_id": body.get("message_id"),
            },
        )
        made = self._queue.submit(MAIN_QUEUE, job.kind.value, job.dedupe_key, job.payload)
        return Response(200, {"job_id": made.job_id, "created": made.created})

    def _queue_route(self, name: str, query: str, request: Request) -> Response:
        grant = next((g for g in self._config.grants if _same(g.secret, _bearer(request))), None)
        if grant is None:
            return Response(401, {"error": "bad secret"})
        if name == "result":
            if request.method != "GET":
                return Response(405)
            wanted = parse_qs(query)
            wanted_id, named = (wanted.get("job_id") or [""])[0], (wanted.get("queue") or [""])[0]
            if not wanted_id.isdigit() or named not in grant.submit:
                return Response(403 if wanted_id.isdigit() else 400, {"error": "not allowed or bad id"})
            found = self._queue.result(int(wanted_id), named)
            if found is None:
                return Response(200, {"done": False})
            return Response(200, {"done": True, "status": found.status, "value": found.value})
        if request.method != "POST":
            return Response(405)
        body = _json_object(request.body)
        if body is None:
            return Response(400, {"error": "body is not a JSON object"})
        queue = body.get("queue")
        if name == "submit":
            kind, key, payload = body.get("kind"), body.get("dedupe_key"), body.get("payload", {})
            if not (isinstance(queue, str) and isinstance(kind, str) and isinstance(key, str)):
                return Response(400, {"error": "queue, kind and dedupe_key are required"})
            if queue not in grant.submit:
                return Response(403, {"error": "queue not allowed"})
            if not isinstance(payload, dict):
                return Response(400, {"error": "payload must be an object"})
            made = self._queue.submit(queue, kind, key, payload)
            return Response(200, {"job_id": made.job_id, "created": made.created})
        if name == "claim":
            lease = _number(body.get("lease_seconds"), DEFAULT_LEASE_SECONDS)
            if not isinstance(queue, str) or lease is None:
                return Response(400, {"error": "queue and lease_seconds are required"})
            if queue not in grant.claim:
                return Response(403, {"error": "queue not allowed"})
            claim = self._queue.claim(queue, lease)
            if claim is None:
                return Response(200, {"job": None})
            return Response(
                200,
                {
                    "job": {
                        "job_id": claim.job_id,
                        "kind": claim.kind,
                        "dedupe_key": claim.dedupe_key,
                        "payload": dict(claim.payload),
                        "token": claim.token,
                        "lease_until": claim.lease_until,
                        "attempt": claim.attempt,
                    }
                },
            )
        if name in {"ack", "fail", "extend"}:
            job_id, token = body.get("job_id"), body.get("token")
            if not isinstance(job_id, int) or not isinstance(token, str) or not isinstance(queue, str):
                return Response(400, {"error": "queue, job_id and token are required"})
            if queue not in grant.claim:
                return Response(403, {"error": "queue not allowed"})
            if not self._queue.owns(job_id, queue):
                return Response(404, {"error": "no such job on this queue"})
            if name == "ack":
                accepted = self._queue.ack(job_id, token, body.get("result"))
            elif name == "fail":
                retry = _number(body.get("retry_in"), None)
                accepted = self._queue.fail(job_id, token, str(body.get("reason", "failed")), retry)
            else:
                lease = _number(body.get("lease_seconds"), DEFAULT_LEASE_SECONDS) or DEFAULT_LEASE_SECONDS
                accepted = self._queue.extend(job_id, token, lease)
            return Response(200 if accepted else 409, {"accepted": accepted})
        return Response(404)


def _json_object(body: bytes) -> dict[str, Any] | None:
    try:
        parsed = json.loads(body)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _get(body: Mapping[str, Any], *path: str, default: Any = "") -> Any:
    current: Any = body
    for step in path:
        if not isinstance(current, dict) or step not in current:
            return default
        current = current[step]
    return current


def _number(value: object, default: float | None) -> float | None:
    if isinstance(value, bool):
        return default
    return float(value) if isinstance(value, (int, float)) else default
