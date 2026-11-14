"""Run one job at a time from one named queue."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Protocol
from urllib.error import URLError

from t6.domain.events import JsonValue
from t6.worker.client import WorkerJob

Handler = Callable[[WorkerJob], JsonValue]
"""Does the work for one job and returns its result. Raising fails the attempt."""

RETRY_SECONDS = 30.0


class Refused(Exception):
    """Raised by a handler to fail the job for good, with no retry. Its text is the reason."""


class WorkerQueue(Protocol):
    def claim(self, queue: str, lease_seconds: float = 60.0) -> WorkerJob | None: ...
    def ack(self, job: WorkerJob, result: JsonValue = None) -> None: ...
    def fail(self, job: WorkerJob, reason: str, retry_in: float | None = None) -> None: ...
    def extend(self, job: WorkerJob, lease_seconds: float = 60.0) -> None: ...


class Worker:
    """Claims jobs from `queue` and runs the handler registered for each kind.

    Handlers must be safe to run twice: delivery is at-least-once. A job with
    no handler is failed without retry, so it does not block the queue.
    """

    def __init__(
        self,
        client: WorkerQueue,
        queue: str,
        handlers: Mapping[str, Handler],
        lease_seconds: float = 60.0,
        internal_client: WorkerQueue | None = None,
    ) -> None:
        self._clients = [client] if internal_client is None else [client, internal_client]
        self._next_client = 0
        self._queue = queue
        self._handlers = dict(handlers)
        self._lease_seconds = lease_seconds

    def run_once(self) -> bool:
        """Pull and finish one job. False when the queue had nothing due."""
        job = None
        for offset in range(len(self._clients)):
            index = (self._next_client + offset) % len(self._clients)
            client = self._clients[index]
            try:
                job = client.claim(self._queue, self._lease_seconds)
            except (OSError, URLError):
                if len(self._clients) == 1:
                    raise
                continue
            if job is not None:
                self._next_client = (index + 1) % len(self._clients)
                break
        if job is None:
            return False
        handler = self._handlers.get(job.kind)
        if handler is None:
            client.fail(job, f"no handler for {job.kind}")
            return True
        try:
            result = handler(job)
        except Refused as refusal:
            client.fail(job, str(refusal)[:300])
        except Exception as error:  # noqa: BLE001 - any handler failure is a failed attempt
            client.fail(job, f"{type(error).__name__}: {error}"[:300], retry_in=RETRY_SECONDS)
        else:
            client.ack(job, result)
        return True
