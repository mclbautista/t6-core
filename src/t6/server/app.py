"""The webhook server's request handling, with no sockets (see `t6.server.http`).

Two listeners, two entry points:

- `WebhookApp.public(request)` is the internet-facing path. It serves only
  `/webhook` and `/webhook/*` (Frame.io and Notion). Everything else is 404, so
  the tap and queue routes are not reachable from the internet.
- `WebhookApp.tailnet(request)` is for tailnet peers only. It serves the tap
  endpoint (`POST /tap`, the gateway), the queue routes (`/queue/*`, workers
  and callers) and the Project registry routes (`/registry/*`).

The server only answers requests. It never connects to a worker.

Payload shapes below follow the provider documentation as summarised in
t6-core#13 and are NOT yet checked against live deliveries:
Frame.io `{"type": "comment.created", "resource": {"id": ...}, "project": {"id": ...}}`
and Notion `{"id", "timestamp", "type": "page.properties_updated",
"entity": {"id", "type": "page"}, "data": {"updated_properties": [...]}}`.
Notion's `page.created`, `page.deleted` and `page.undeleted` have the same
envelope (Notion webhook event reference, read 2026-11-10).
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
from t6.registry import (
    AuditEntry,
    Changed,
    ChangeOutcome,
    CodeHeld,
    KeyReused,
    NotFound,
    PageAlreadyBound,
    ProjectRegistry,
    Registered,
    RegisteredProject,
    RegistryError,
    RegistryUnavailable,
    ReviewItem,
    RevisionConflict,
    Rotation,
    Shown,
)
from t6.server.queue import FRAMEIO_QUEUE, MAIN_QUEUE, JobQueue
from t6.server.signatures import WebhookRejected, verify_frameio, verify_notion

REVIEW_QUIET_SECONDS = 15 * 60.0
DEFAULT_LEASE_SECONDS = 60.0

NOTION_PAGE_EVENTS = frozenset({"page.properties_updated", "page.created", "page.deleted", "page.undeleted"})
"""Notion events that queue a `notion.page_changed` job. The event only says a
page changed; the worker reads the page's current state."""

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
    """What one bearer secret may do on the queue and registry routes."""

    secret: str
    claim: frozenset[str] = frozenset()
    """Queues this holder may claim from, ack and fail."""
    submit: frozenset[str] = frozenset()
    """Queues this holder may submit to and read results from."""
    registry_read: bool = False
    registry_write: bool = False
    registry_sync: bool = False
    """May report Notion sync results and page observations. Changes no Project."""
    actor: str = ""
    """Who this holder is in the registry's audit history."""


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
            grants.append(
                Grant(
                    environ["T6_WORKER_SECRET"],
                    claim=main,
                    submit=main | frameio,
                    registry_read=True,
                    registry_sync=True,
                    actor="main-worker",
                )
            )
        if environ.get("T6_FRAMEIO_WORKER_SECRET"):
            grants.append(Grant(environ["T6_FRAMEIO_WORKER_SECRET"], claim=frameio))
        if environ.get("T6_REGISTRY_SECRET"):
            grants.append(
                Grant(environ["T6_REGISTRY_SECRET"], registry_read=True, registry_write=True, actor="registry-writer")
            )
        if environ.get("T6_REGISTRY_READ_SECRET"):
            grants.append(Grant(environ["T6_REGISTRY_READ_SECRET"], registry_read=True, actor="registry-reader"))
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
        registry: ProjectRegistry | None = None,
    ) -> None:
        self._registry = registry
        self._queue = queue
        self._config = config
        self._clock = clock
        self._on_notion_verification = on_notion_verification
        self._dispatch_sync()  # jobs for changes accepted before a restart

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
        if body.get("type") not in NOTION_PAGE_EVENTS:
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
                "event_type": body["type"],
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
        if parts.path.startswith("/registry/"):
            answer = self._registry_route(parts.path[len("/registry/") :], request)
            self._dispatch_sync()
            return answer
        return Response(404)

    def _dispatch_sync(self) -> None:
        """Queue a `registry.sync` job for every sync intent that has none yet.

        The intent was committed with its change, so a crash before this point
        loses nothing: the next call (a restart, a registry request or a claim)
        queues it. Queueing twice is harmless: the job is deduped by intent.
        """
        if self._registry is None:
            return
        try:
            intents = self._registry.undispatched_sync()
            for intent in intents:
                self._queue.submit(
                    MAIN_QUEUE,
                    JobKind.REGISTRY_SYNC.value,
                    f"registry.sync:{intent.id}",
                    {"project_id": intent.project_id, "revision": intent.revision},
                )
            self._registry.mark_dispatched(intent.id for intent in intents)
        except RegistryUnavailable:
            return

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
            self._dispatch_sync()
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


    def _registry_route(self, name: str, request: Request) -> Response:
        if self._registry is None:
            return Response(503, {"error": "registry not configured"})
        grants = [g for g in self._config.grants if _same(g.secret, _bearer(request))]
        if not grants:
            return Response(401, {"error": "bad secret"})
        try:
            return self._registry_answer(self._registry, grants, name.split("/"), request)
        except RegistryUnavailable:
            return Response(503, {"error": "registry_unavailable"})
        except RegistryError as problem:
            return Response(400, {"error": str(problem)})

    def _registry_answer(
        self, registry: ProjectRegistry, grants: list[Grant], steps: list[str], request: Request
    ) -> Response:
        if steps == ["review-items"]:
            if request.method != "GET":
                return Response(405)
            if not any(g.registry_read for g in grants):
                return Response(403, {"error": "registry read not allowed"})
            return Response(200, {"items": [_review_body(item) for item in registry.review_items()]})
        if len(steps) == 3 and steps[1] and (steps[0], steps[2]) in _REGISTRY_SYNC_ROUTES:
            return self._registry_sync_answer(registry, grants, steps, request)
        if steps == ["unclassified"]:
            if request.method != "GET":
                return Response(405)
            if not any(g.registry_read for g in grants):
                return Response(403, {"error": "registry read not allowed"})
            found = registry.list_projects(Rotation.UNCLASSIFIED)
            return Response(200, {"projects": [_project_body(project) for project in found]})
        if steps == ["projects"]:
            reads = False
        elif len(steps) == 2 and steps[0] in {"projects", "pages"} and steps[1]:
            reads = True
        elif len(steps) == 3 and steps[0] == "projects" and steps[1] and steps[2] in {"audit", *_REGISTRY_CHANGES}:
            reads = steps[2] == "audit"
        else:
            return Response(404)
        if request.method != ("GET" if reads else "POST"):
            return Response(405)

        if reads:
            if not any(g.registry_read for g in grants):
                return Response(403, {"error": "registry read not allowed"})
            project = registry.get_by_page(steps[1]) if steps[0] == "pages" else registry.get(steps[1])
            if project is None:
                return Response(404, {"error": "no such project"})
            if steps[2:]:
                return Response(200, {"entries": [_audit_body(entry) for entry in registry.audit(project.id)]})
            return Response(200, {"project": _project_body(project)})

        writer = next((g for g in grants if g.registry_write), None)
        if writer is None:
            return Response(403, {"error": "registry write not allowed"})
        body = _json_object(request.body)
        if body is None:
            return Response(400, {"error": "body is not a JSON object"})
        key = request.header("idempotency-key")
        page, code, rotation = body.get("notion_page_id"), body.get("code"), body.get("rotation")

        if steps == ["projects"]:
            if not (page is None or isinstance(page, str)) or not (code is None or isinstance(code, str)):
                return Response(400, {"error": "notion_page_id and code are strings"})
            if rotation is not None and rotation not in {r.value for r in Rotation}:
                return Response(400, {"error": "rotation is in, out or unclassified"})
            made = registry.register(
                actor=writer.actor,
                idempotency_key=key,
                notion_page_id=page,
                code=code,
                rotation=Rotation(rotation or Rotation.OUT),
            )
            if isinstance(made, Registered):
                return Response(
                    201 if made.created else 200, {"project": _project_body(made.project), "created": made.created}
                )
            return _refusal(made)

        revision = body.get("expected_revision")
        if not isinstance(revision, int) or isinstance(revision, bool):
            return Response(400, {"error": "expected_revision is required"})
        outcome: ChangeOutcome
        if steps[2] == "code":
            if "code" not in body or not (code is None or isinstance(code, str)):
                return Response(400, {"error": "code is a string, or null to clear it"})
            outcome = registry.set_code(
                steps[1], code, actor=writer.actor, idempotency_key=key, expected_revision=revision
            )
        elif steps[2] == "rotation":
            if rotation not in {Rotation.IN.value, Rotation.OUT.value}:
                return Response(400, {"error": "rotation is in or out"})
            outcome = registry.set_rotation(
                steps[1], Rotation(rotation), actor=writer.actor, idempotency_key=key, expected_revision=revision
            )
        else:
            if not isinstance(page, str):
                return Response(400, {"error": "notion_page_id is required"})
            outcome = registry.bind_page(
                steps[1], page, actor=writer.actor, idempotency_key=key, expected_revision=revision
            )
        if isinstance(outcome, Changed):
            return Response(
                200,
                {"project": _project_body(outcome.project), "changed": outcome.changed, "replayed": outcome.replayed},
            )
        return _refusal(outcome)

    def _registry_sync_answer(
        self, registry: ProjectRegistry, grants: list[Grant], steps: list[str], request: Request
    ) -> Response:
        """Routes for the Notion sync worker, plus an operator's request to reconcile a page."""
        if request.method != "POST":
            return Response(405)
        body = _json_object(request.body)
        if steps[2] == "reconcile":
            if not any(g.registry_write for g in grants):
                return Response(403, {"error": "registry write not allowed"})
            key = request.header("idempotency-key")
            if not key.strip():
                return Response(400, {"error": "idempotency key is required"})
            made = self._queue.submit(
                MAIN_QUEUE,
                JobKind.NOTION_PAGE_CHANGED.value,
                f"registry.reconcile:{key}",
                {"page_id": steps[1], "event_type": "reconcile"},
            )
            return Response(200, {"job_id": made.job_id, "created": made.created})
        syncer = next((g for g in grants if g.registry_sync), None)
        if syncer is None:
            return Response(403, {"error": "registry sync not allowed"})
        if body is None:
            return Response(400, {"error": "body is not a JSON object"})
        if steps[2] == "sync":
            revision, ok, attempt = body.get("revision"), body.get("ok"), body.get("attempt", 1)
            if not (isinstance(revision, int) and isinstance(ok, bool) and isinstance(attempt, int)):
                return Response(400, {"error": "revision, ok and attempt are required"})
            project = registry.report_sync(
                steps[1],
                revision,
                ok=ok,
                detail=str(body.get("detail", "")),
                final=bool(body.get("final")) or attempt >= self._queue.max_attempts,
            )
            if project is None:
                return Response(404, {"error": "no such project"})
            return Response(200, {"project": _project_body(project)})
        available, shown = body.get("available"), body.get("shown")
        if not isinstance(available, bool) or not (shown is None or isinstance(shown, dict)):
            return Response(400, {"error": "available is required; shown is an object"})
        seen = None
        if shown is not None:
            values = [shown.get(name) for name in ("t6_id", "code", "rotation")]
            if not all(value is None or isinstance(value, str) for value in values):
                return Response(400, {"error": "shown values are strings or null"})
            seen = Shown(*values)
        observed = registry.observe_page(steps[1], actor=syncer.actor, available=available, shown=seen)
        return Response(
            200,
            {
                "outcome": observed.outcome,
                "project": None if observed.project is None else _project_body(observed.project),
            },
        )


_REGISTRY_CHANGES = frozenset({"code", "rotation", "bindings"})
_REGISTRY_SYNC_ROUTES = frozenset({("projects", "sync"), ("pages", "observed"), ("pages", "reconcile")})


def _review_body(item: ReviewItem) -> dict[str, JsonValue]:
    return {
        "id": item.id,
        "kind": item.kind,
        "page_id": item.page_id,
        "project_id": item.project_id,
        "detail": dict(item.detail),
        "created_at": item.created_at,
    }


def _refusal(outcome: NotFound | RevisionConflict | CodeHeld | PageAlreadyBound | KeyReused) -> Response:
    if isinstance(outcome, NotFound):
        return Response(404, {"error": "no such project"})
    if isinstance(outcome, RevisionConflict):
        return Response(409, {"error": "revision_conflict", "revision": outcome.revision})
    if isinstance(outcome, CodeHeld):
        return Response(
            409,
            {"error": "code_held", "code": outcome.code, "held_by": list(outcome.held_by), "reason": outcome.reason},
        )
    if isinstance(outcome, PageAlreadyBound):
        return Response(409, {"error": "page_already_bound", "project_id": outcome.project_id})
    return Response(409, {"error": "idempotency_key_reused"})


def _audit_body(entry: AuditEntry) -> dict[str, JsonValue]:
    return {
        "project_id": entry.project_id,
        "action": entry.action,
        "actor": entry.actor,
        "at": entry.at,
        "before": None if entry.before is None else dict(entry.before),
        "after": dict(entry.after),
    }


def _project_body(project: RegisteredProject) -> dict[str, JsonValue]:
    return {
        "id": project.id,
        "revision": project.revision,
        "code": project.code,
        "rotation": project.rotation.value,
        "bindings": [
            {
                "system": binding.system,
                "page_id": binding.page_id,
                "bound_at": binding.bound_at,
                "status": "active" if binding.active else "legacy",
                "available": binding.available,
            }
            for binding in project.bindings
        ],
        "sync": project.sync.value,
        "sync_error": project.sync_error,
        "created_at": project.created_at,
    }


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
