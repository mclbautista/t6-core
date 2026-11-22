"""`resolve.stamp` jobs in the internal job table: the server's side of the Resolve worker.

A verb queues a job with `enqueue` inside its own transaction. The Resolve worker (`t6.resolve.worker`) never opens
the database: it claims, extends and finishes jobs through `/v1/resolve/*`, which call `claim`, `extend`, `report`
and `fail` here. A job is never run again: a failure ends it `dead`, so `queue failed` lists it and `queue dismiss`
clears it (#414). Both finishes go through `_settle`, the one place that acts on the result.
"""
from __future__ import annotations

import secrets
import sqlite3
from typing import Any

from t6.domain.events import JsonValue
from t6.registry.sqlite import SqliteProjectRegistry
from t6.server.queue import Claim
from t6.worker.internal_queue import InternalJobQueue, JobRecord

QUEUE = 'resolve'
KIND = 'resolve.stamp'
IDENTITY = 'resolve'
LEASE_SECONDS = 300


class NotFound(LookupError):
    """The job does not exist on the resolve queue."""


class Forbidden(PermissionError):
    """Only the Resolve worker may act on these jobs."""


class Invalid(ValueError):
    """A report that does not match the job."""


class ClaimLost(RuntimeError):
    """The caller's lease on the job is gone: expired, replaced, or never held."""


class ResolveStampJobs:
    def __init__(self, registry: SqliteProjectRegistry) -> None:
        self.registry = registry
        self.queue = InternalJobQueue(registry, clock=registry._clock)

    def enqueue(self, db: sqlite3.Connection, *, project: str, resolve_project: str, timeline: str, stamp: str, replaces: str | None = None) -> int:
        """Queue a stamp job in the caller's transaction `db`; its job id. `timeline` is a timeline name or Resolve unique id;
        `replaces` is the duplicated stamp a re-stamp overwrites."""
        payload: dict[str, JsonValue] = {'project': project, 'resolve_project': resolve_project, 'timeline': timeline, 'stamp': stamp}
        if replaces is not None:
            payload['replaces'] = replaces
        return self.queue.submit(QUEUE, KIND, secrets.token_urlsafe(24), payload, db=db).job_id

    def claim(self, actor: str) -> Claim | None:
        if actor != IDENTITY:
            raise Forbidden(actor)
        return self.queue.claim(QUEUE, LEASE_SECONDS, [KIND])

    def _held(self, db: sqlite3.Connection | None, job_id: int, token: object, actor: str) -> JobRecord:
        if actor != IDENTITY:
            raise Forbidden(actor)
        job = self.queue.get(job_id, queue=QUEUE, kind=KIND, db=db)
        if job is None:
            raise NotFound(job_id)
        if job.state != 'claimed' or job.lease_token != token or (job.lease_until or 0) <= self.registry._clock():
            raise ClaimLost(job_id)
        return job

    def extend(self, job_id: int, token: object, actor: str) -> None:
        self._held(None, job_id, token, actor)
        if not self.queue.extend(job_id, str(token), LEASE_SECONDS):
            raise ClaimLost(job_id)

    def report(self, job_id: int, token: object, data: object, actor: str) -> None:
        """The worker wrote and read back the stamp: `data` is the timeline as Resolve now shows it."""
        with self.registry._transaction() as db:
            job = self._held(db, job_id, token, actor)
            if not isinstance(data, dict) or set(data) != {'uuid', 'name', 'stamp'} or not all(isinstance(v, str) for v in data.values()) \
                    or data['stamp'] != job.payload['stamp']:
                raise Invalid()
            _settle(self.queue, db, job, {'outcome': 'done', 'result': {'job_id': job_id, 'timeline': data}})

    def fail(self, job_id: int, token: object, reason: object, actor: str) -> None:
        """The worker could not stamp: the job ends dead with the reason, and is never run again."""
        if not isinstance(reason, str) or not reason.strip():
            raise Invalid()
        with self.registry._transaction() as db:
            job = self._held(db, job_id, token, actor)
            _settle(self.queue, db, job, {'outcome': 'refused', 'rule': 'resolve', 'reason': reason.strip()[:300]})


def _settle(queue: InternalJobQueue, db: sqlite3.Connection, job: JobRecord, outcome: dict[str, Any]) -> None:
    """End the job with the worker's outcome, in the report's transaction, and mark the Timeline of the job's stamp
    stamped (written and read back, so verified) or failed with the reason."""
    done = outcome['outcome'] == 'done'
    queue.finish(job.job_id, outcome, state='done' if done else 'dead', db=db)
    db.execute('UPDATE timelines SET stamp_state=?, stamp_reason=? WHERE id=?', ('stamped' if done else 'failed', None if done else outcome['reason'], job.payload['stamp']))
