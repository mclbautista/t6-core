"""Internal jobs in the t6 database, with the worker queue interface."""
from __future__ import annotations

from collections.abc import Callable, Mapping
import json
from pathlib import Path
import secrets
import time

from t6.domain.events import JsonValue
from t6.registry.sqlite import SqliteProjectRegistry
from t6.server.queue import Claim, DEFAULT_MAX_ATTEMPTS, JobResult, Submitted, _require_name
from t6.worker.client import QueueRejected, WorkerJob


class InternalJobQueue:
    def __init__(self, path: str | Path, clock: Callable[[], float] = time.time,
                 max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self._db = SqliteProjectRegistry(path)
        self._clock = clock
        self._max_attempts = max_attempts

    def submit(self, queue: str, kind: str, dedupe_key: str,
               payload: Mapping[str, JsonValue] | None = None, run_at: float | None = None) -> Submitted:
        for value, name in ((queue, 'queue'), (kind, 'kind'), (dedupe_key, 'dedupe_key')):
            _require_name(value, name)
        now = self._clock()
        with self._db._transaction() as db:
            cursor = db.execute(
                "INSERT INTO internal_jobs (queue, kind, dedupe_key, payload, run_at, state, created_at) "
                "VALUES (?, ?, ?, ?, ?, 'queued', ?) ON CONFLICT (kind, dedupe_key) DO NOTHING",
                (queue, kind, dedupe_key, json.dumps(dict(payload or {})), now if run_at is None else run_at, now))
            if cursor.rowcount:
                return Submitted(int(cursor.lastrowid or 0), True)
            row = db.execute("SELECT id FROM internal_jobs WHERE kind = ? AND dedupe_key = ?", (kind, dedupe_key)).fetchone()
            return Submitted(row[0], False)

    def claim(self, queue: str, lease_seconds: float = 60.0) -> Claim | None:
        _require_name(queue, 'queue')
        if lease_seconds <= 0:
            raise ValueError('lease_seconds must be positive')
        now = self._clock()
        with self._db._transaction() as db:
            db.execute("UPDATE internal_jobs SET state = 'dead', lease_token = NULL, lease_until = NULL, result = ? "
                       "WHERE queue = ? AND state = 'claimed' AND lease_until <= ? AND attempts >= ?",
                       (json.dumps({'status': 'failed', 'value': 'lease expired too many times'}), queue, now, self._max_attempts))
            row = db.execute("SELECT id, kind, dedupe_key, payload, attempts FROM internal_jobs WHERE queue = ? AND "
                             "((state = 'queued' AND run_at <= ?) OR (state = 'claimed' AND lease_until <= ?)) "
                             "ORDER BY run_at, id LIMIT 1", (queue, now, now)).fetchone()
            if row is None:
                return None
            job_id, kind, key, payload, attempts = row
            token = secrets.token_urlsafe(16)
            until = now + lease_seconds
            db.execute("UPDATE internal_jobs SET state = 'claimed', lease_token = ?, lease_until = ?, attempts = attempts + 1 WHERE id = ?",
                       (token, until, job_id))
            return Claim(job_id, queue, kind, key, json.loads(payload), token, until, attempts + 1)

    def extend(self, job_id: int, token: str, lease_seconds: float) -> bool:
        if lease_seconds <= 0:
            raise ValueError('lease_seconds must be positive')
        now = self._clock()
        with self._db._transaction() as db:
            return db.execute("UPDATE internal_jobs SET lease_until = ? WHERE id = ? AND state = 'claimed' AND lease_token = ? AND lease_until > ?",
                              (now + lease_seconds, job_id, token, now)).rowcount == 1

    def ack(self, job_id: int, token: str, value: JsonValue = None) -> bool:
        with self._db._transaction() as db:
            return db.execute("UPDATE internal_jobs SET state = 'done', lease_token = NULL, lease_until = NULL, result = ? "
                              "WHERE id = ? AND state = 'claimed' AND lease_token = ?",
                              (json.dumps({'status': 'ok', 'value': value}), job_id, token)).rowcount == 1

    def fail(self, job_id: int, token: str, reason: str, retry_in: float | None = None) -> bool:
        with self._db._transaction() as db:
            row = db.execute("SELECT attempts FROM internal_jobs WHERE id = ? AND state = 'claimed' AND lease_token = ?", (job_id, token)).fetchone()
            if row is None:
                return False
            state = 'failed' if retry_in is None else ('dead' if row[0] >= self._max_attempts else 'queued')
            db.execute("UPDATE internal_jobs SET state = ?, lease_token = NULL, lease_until = NULL, run_at = ?, result = ? WHERE id = ?",
                       (state, self._clock() + (max(retry_in, 30.0 * 2 ** (row[0] - 1)) if retry_in is not None else 0), json.dumps({'status': 'failed', 'value': reason}), job_id))
            return True

    def result(self, job_id: int, queue: str) -> JobResult | None:
        with self._db._transaction() as db:
            row = db.execute("SELECT state, result FROM internal_jobs WHERE id = ? AND queue = ?", (job_id, queue)).fetchone()
        if row is None or row[0] not in ('done', 'failed', 'dead'):
            return None
        result = json.loads(row[1])
        return JobResult(result['status'], result.get('value'))


class InternalQueueClient:
    """Adapt the database queue to the same interface as QueueClient."""
    def __init__(self, queue: InternalJobQueue) -> None:
        self._queue = queue

    def claim(self, queue: str, lease_seconds: float = 60.0) -> WorkerJob | None:
        claim = self._queue.claim(queue, lease_seconds)
        if claim is None:
            return None
        return WorkerJob(claim.job_id, claim.queue, claim.kind, claim.dedupe_key, claim.payload, claim.token, claim.attempt)

    def ack(self, job: WorkerJob, result: JsonValue = None) -> None:
        if not self._queue.ack(job.job_id, job.token, result):
            raise QueueRejected(409, 'claim lost')

    def fail(self, job: WorkerJob, reason: str, retry_in: float | None = None) -> None:
        if not self._queue.fail(job.job_id, job.token, reason, retry_in):
            raise QueueRejected(409, 'claim lost')

    def extend(self, job: WorkerJob, lease_seconds: float = 60.0) -> None:
        if not self._queue.extend(job.job_id, job.token, lease_seconds):
            raise QueueRejected(409, 'claim lost')
