"""`resolve.stamp` jobs in the internal job table: the server's side of the Resolve worker.

A verb queues a job with `enqueue` inside its own transaction. The Resolve worker (`t6.resolve.worker`) never opens
the database: it claims, extends and finishes jobs through `/v1/resolve/*`, which call `claim`, `extend`, `report`
and `fail` here. A job is never run again: a failure ends it `dead`, so `queue failed` lists it and `queue dismiss`
clears it (#414). Both finishes go through `_settle`, the one place that acts on the result. A job whose lease runs
out (the worker stopped mid-job) is not claimed again: it is buried dead and `fail_dead` marks its Timeline failed.
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
LEASE_EXPIRED = 'The Resolve worker stopped before finishing the job (its lease ran out)'


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
        self.queue = InternalJobQueue(registry, clock=registry._clock, max_attempts=1)  # one try: an expired lease buries the job

    def enqueue(self, db: sqlite3.Connection, *, project: str, resolve_project: str, timeline: str, stamp: str, replaces: str | None = None,
                canonical: str | None = None) -> int:
        """Queue a stamp job in the caller's transaction `db`; its job id. `timeline` is a timeline name or Resolve unique id;
        `replaces` is the duplicated stamp a re-stamp overwrites, and `canonical` the Resolve unique id of the canonical
        Timeline it must not touch (when known)."""
        payload: dict[str, JsonValue] = {'project': project, 'resolve_project': resolve_project, 'timeline': timeline, 'stamp': stamp}
        if replaces is not None:
            payload['replaces'] = replaces
        if canonical is not None:
            payload['canonical'] = canonical
        return self.queue.submit(QUEUE, KIND, secrets.token_urlsafe(24), payload, db=db).job_id

    def claim(self, actor: str) -> Claim | None:
        if actor != IDENTITY:
            raise Forbidden(actor)
        claim = self.queue.claim(QUEUE, LEASE_SECONDS, [KIND])
        with self.registry._transaction() as db:
            self.fail_dead(db)
        return claim

    def fail_dead(self, db: sqlite3.Connection) -> None:
        """Bury every job whose lease ran out and mark its Timeline failed, so linking it again queues it again. Only a
        Timeline still `pending` with no later job for its stamp is touched: a job the worker failed has settled already."""
        self.queue.bury(QUEUE, db=db)
        for job in self.queue.find(KIND, states=['dead'], db=db):
            stamp = str(job.payload['stamp'])
            if not self.queue.find(KIND, after=job.job_id, payload={'stamp': stamp}, db=db):
                db.execute("UPDATE timelines SET stamp_state='failed', stamp_reason=? WHERE id=? AND stamp_state='pending'", (LEASE_EXPIRED, stamp))

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
    stamped (written and read back, so verified, with the Resolve unique id it was verified on) or failed with the reason."""
    done = outcome['outcome'] == 'done'
    queue.finish(job.job_id, outcome, state='done' if done else 'dead', db=db)
    if done:
        db.execute("UPDATE timelines SET stamp_state='stamped', stamp_reason=NULL, resolve_uuid=? WHERE id=?", (outcome['result']['timeline']['uuid'], job.payload['stamp']))
    else:
        db.execute("UPDATE timelines SET stamp_state='failed', stamp_reason=? WHERE id=?", (outcome['reason'], job.payload['stamp']))
