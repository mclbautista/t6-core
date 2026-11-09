"""Client for the server's queue routes, over a `Transport`.

`HttpTransport` talks to the server's tailnet address. Tests pass a transport
that calls `WebhookApp.tailnet` in the same process.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
from typing import Any, Protocol
import urllib.error
import urllib.request

from t6.domain.events import JsonValue


class Transport(Protocol):
    def send(
        self, method: str, path: str, headers: Mapping[str, str], body: bytes
    ) -> tuple[int, dict[str, Any]]:
        """Return the status and the JSON object the server answered."""
        ...


class HttpTransport:
    def __init__(self, base_url: str, timeout: float = 30.0) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = timeout

    def send(
        self, method: str, path: str, headers: Mapping[str, str], body: bytes
    ) -> tuple[int, dict[str, Any]]:
        request = urllib.request.Request(
            self._base + path, data=body or None, headers=dict(headers), method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"{}")


class QueueRejected(Exception):
    """The server refused a call (bad secret, queue not allowed, claim lost)."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"queue answered {status}: {detail}")
        self.status = status


@dataclass(frozen=True, slots=True)
class WorkerJob:
    job_id: int
    queue: str
    kind: str
    dedupe_key: str
    payload: Mapping[str, JsonValue]
    token: str
    attempt: int


class QueueClient:
    def __init__(self, transport: Transport, secret: str) -> None:
        self._transport = transport
        self._headers = {"Authorization": f"Bearer {secret}", "Content-Type": "application/json"}

    def _call(self, method: str, path: str, body: Mapping[str, Any] | None = None) -> dict[str, Any]:
        status, answer = self._transport.send(
            method, path, self._headers, json.dumps(body).encode() if body is not None else b""
        )
        if status != 200:
            raise QueueRejected(status, str(answer.get("error", answer)))
        return answer

    def claim(self, queue: str, lease_seconds: float = 60.0) -> WorkerJob | None:
        answer = self._call("POST", "/queue/claim", {"queue": queue, "lease_seconds": lease_seconds})
        job = answer["job"]
        if job is None:
            return None
        return WorkerJob(
            job_id=job["job_id"],
            queue=queue,
            kind=job["kind"],
            dedupe_key=job["dedupe_key"],
            payload=job["payload"],
            token=job["token"],
            attempt=job["attempt"],
        )

    def ack(self, job: WorkerJob, result: JsonValue = None) -> None:
        self._call("POST", "/queue/ack", {"queue": job.queue, "job_id": job.job_id, "token": job.token, "result": result})

    def fail(self, job: WorkerJob, reason: str, retry_in: float | None = None) -> None:
        self._call(
            "POST",
            "/queue/fail",
            {"queue": job.queue, "job_id": job.job_id, "token": job.token, "reason": reason, "retry_in": retry_in},
        )

    def extend(self, job: WorkerJob, lease_seconds: float = 60.0) -> None:
        self._call(
            "POST",
            "/queue/extend",
            {"queue": job.queue, "job_id": job.job_id, "token": job.token, "lease_seconds": lease_seconds},
        )

    def submit(
        self, queue: str, kind: str, dedupe_key: str, payload: Mapping[str, JsonValue] | None = None
    ) -> int:
        """Submit a request and return its job ID; read the answer with `result`."""
        answer = self._call(
            "POST",
            "/queue/submit",
            {"queue": queue, "kind": kind, "dedupe_key": dedupe_key, "payload": dict(payload or {})},
        )
        return int(answer["job_id"])

    def result(self, queue: str, job_id: int) -> tuple[str, JsonValue] | None:
        """`(status, value)` once the job has finished, else None."""
        answer = self._call("GET", f"/queue/result?queue={queue}&job_id={job_id}")
        if not answer["done"]:
            return None
        return answer["status"], answer.get("value")
