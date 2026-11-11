"""Run one job at a time from one named queue."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from t6.domain.events import JsonValue
from t6.worker.client import QueueClient, WorkerJob

Handler = Callable[[WorkerJob], JsonValue]
"""Does the work for one job and returns its result. Raising fails the attempt."""

RETRY_SECONDS = 30.0


class Refused(Exception):
    """Raised by a handler to fail the job for good, with no retry. Its text is the reason."""


class Worker:
    """Claims jobs from `queue` and runs the handler registered for each kind.

    Handlers must be safe to run twice: delivery is at-least-once. A job with
    no handler is failed without retry, so it does not block the queue.
    """

    def __init__(
        self,
        client: QueueClient,
        queue: str,
        handlers: Mapping[str, Handler],
        lease_seconds: float = 60.0,
    ) -> None:
        self._client = client
        self._queue = queue
        self._handlers = dict(handlers)
        self._lease_seconds = lease_seconds

    def run_once(self) -> bool:
        """Pull and finish one job. False when the queue had nothing due."""
        job = self._client.claim(self._queue, self._lease_seconds)
        if job is None:
            return False
        handler = self._handlers.get(job.kind)
        if handler is None:
            self._client.fail(job, f"no handler for {job.kind}")
            return True
        try:
            result = handler(job)
        except Refused as refusal:
            self._client.fail(job, str(refusal)[:300])
        except Exception as error:  # noqa: BLE001 - any handler failure is a failed attempt
            self._client.fail(job, f"{type(error).__name__}: {error}"[:300], retry_in=RETRY_SECONDS)
        else:
            self._client.ack(job, result)
        return True
