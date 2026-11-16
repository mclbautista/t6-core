"""Internal jobs in the t6 database, with the worker queue interface."""
from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Mapping
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
import json
import sqlite3
from typing import Any
from pathlib import Path
import secrets
import time

from t6.domain.events import JsonValue
from t6.registry.sqlite import SqliteProjectRegistry
from t6.server.queue import Claim, DEFAULT_MAX_ATTEMPTS, JobResult, Submitted, _require_name
from t6.worker.client import QueueRejected, WorkerJob


@dataclass(frozen=True)
class JobRecord:
    """One internal job as stored: its payload, its state and the lease it holds, if any."""
    job_id: int
    queue: str
    kind: str
    dedupe_key: str
    payload: dict[str, JsonValue]
    state: str
    attempts: int
    lease_token: str | None
    lease_until: float | None
    result: JsonValue


def _stored(status: str, value: JsonValue) -> str:
    """The result as the table keeps it, the one shape `result` reads back: written by `ack`, `fail`, `bury` and `finish` alike."""
    return json.dumps({'status': status, 'value': value})


_COLUMNS = "id, queue, kind, dedupe_key, payload, state, attempts, lease_token, lease_until, result"


def _record(row: Any) -> JobRecord:
    job_id, queue, kind, key, payload, state, attempts, token, until, result = row
    return JobRecord(int(job_id), str(queue), str(kind), str(key), json.loads(str(payload)), str(state), int(attempts),
                     None if token is None else str(token), None if until is None else float(until),
                     json.loads(str(result)) if result else None)


class InternalJobQueue:
    def __init__(self, path: str | Path | SqliteProjectRegistry, clock: Callable[[], float] = time.time,
                 max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self._db = path if isinstance(path, SqliteProjectRegistry) else SqliteProjectRegistry(path)
        self._clock = clock
        self._max_attempts = max_attempts

    def _within(self, db: sqlite3.Connection | None) -> AbstractContextManager[sqlite3.Connection]:
        """The caller's open transaction, or a new one: lets a caller change jobs and its own rows together."""
        return self._db._transaction() if db is None else nullcontext(db)

    def get(self, job_id: int, *, kind: str | None = None, queue: str | None = None,
            db: sqlite3.Connection | None = None) -> JobRecord | None:
        """One job by id (None when it is missing, or is not of the kind or queue asked for)."""
        with self._within(db) as conn:
            row = conn.execute(f"SELECT {_COLUMNS} FROM internal_jobs WHERE id = ? AND (? IS NULL OR kind = ?) AND (? IS NULL OR queue = ?)",
                               (job_id, kind, kind, queue, queue)).fetchone()
        return None if row is None else _record(row)

    def find(self, kind: str, *, states: Iterable[str] | None = None, after: int | None = None,
             payload: Mapping[str, str] | None = None, db: sqlite3.Connection | None = None) -> list[JobRecord]:
        """Jobs of one kind in id order, narrowed by state, by id greater than `after`, and by top-level payload fields."""
        wanted = tuple(states) if states is not None else None
        clauses, args = ['kind = ?'], [kind]
        if wanted is not None:
            clauses.append('state IN (' + ', '.join('?' * len(wanted)) + ')')
            args.extend(wanted)
        if after is not None:
            clauses.append('id > ?')
            args.append(str(after))
        for name, value in (payload or {}).items():
            clauses.append("json_extract(payload, ?) = ?")
            args.extend((f'$.{name}', value))
        with self._within(db) as conn:
            rows = conn.execute(f"SELECT {_COLUMNS} FROM internal_jobs WHERE " + ' AND '.join(clauses) + ' ORDER BY id', args).fetchall()
        return [_record(row) for row in rows]

    def finish(self, job_id: int, outcome: JsonValue, *, state: str = 'done', db: sqlite3.Connection | None = None) -> None:
        """End a job with the outcome the caller reads back through `result`: `done` is status ok, `failed` is status failed; the lease is dropped."""
        if state not in ('done', 'failed'):
            raise ValueError('state must be done or failed')
        with self._within(db) as conn:
            conn.execute("UPDATE internal_jobs SET state = ?, result = ?, lease_token = NULL, lease_until = NULL WHERE id = ?",
                         (state, _stored('ok' if state == 'done' else 'failed', outcome), job_id))

    def settle(self, job_id: int, *, db: sqlite3.Connection | None = None) -> bool:
        """Mark a dead job as dealt with; true only the first time, so a follow-up is written once however often it is looked for."""
        with self._within(db) as conn:
            return bool(conn.execute("UPDATE internal_jobs SET result = json_set(COALESCE(result, '{}'), '$.settled', json('true')) "
                                     "WHERE id = ? AND state = 'dead' AND json_extract(COALESCE(result, '{}'), '$.settled') IS NULL", (job_id,)).rowcount)

    def dead(self, *, db: sqlite3.Connection | None = None) -> list[JobRecord]:
        """The dead jobs of every kind in id order (a dismissed job is not dead any more)."""
        with self._within(db) as conn:
            rows = conn.execute(f"SELECT {_COLUMNS} FROM internal_jobs WHERE state = 'dead' ORDER BY id").fetchall()
        return [_record(row) for row in rows]

    def dismiss(self, job_id: int, *, db: sqlite3.Connection | None = None) -> bool:
        """Dismiss a dead job: it is never claimed again. Its row, result and dedupe key stay. False unless the job is dead."""
        with self._within(db) as conn:
            return conn.execute("UPDATE internal_jobs SET state = 'dismissed' WHERE id = ? AND state = 'dead'", (job_id,)).rowcount == 1

    def submit(self, queue: str, kind: str, dedupe_key: str,
               payload: Mapping[str, JsonValue] | None = None, run_at: float | None = None,
               db: sqlite3.Connection | None = None) -> Submitted:
        for value, name in ((queue, 'queue'), (kind, 'kind'), (dedupe_key, 'dedupe_key')):
            _require_name(value, name)
        now = self._clock()
        with self._within(db) as conn:
            cursor = conn.execute(
                "INSERT INTO internal_jobs (queue, kind, dedupe_key, payload, run_at, state, created_at) "
                "VALUES (?, ?, ?, ?, ?, 'queued', ?) ON CONFLICT (kind, dedupe_key) DO NOTHING",
                (queue, kind, dedupe_key, json.dumps(dict(payload or {})), now if run_at is None else run_at, now))
            if cursor.rowcount:
                return Submitted(int(cursor.lastrowid or 0), True)
            row = conn.execute("SELECT id FROM internal_jobs WHERE kind = ? AND dedupe_key = ?", (kind, dedupe_key)).fetchone()
            return Submitted(row[0], False)

    def bury(self, queue: str | None = None, *, db: sqlite3.Connection | None = None) -> int:
        """Mark dead every claimed job whose lease ran out with all its attempts used (of one queue, or of all); how many.

        A claim does this first. Anyone who must know whether such a job is dead, with no worker polling, calls it too.
        """
        with self._within(db) as conn:
            return conn.execute("UPDATE internal_jobs SET state = 'dead', lease_token = NULL, lease_until = NULL, result = ? "
                                "WHERE (? IS NULL OR queue = ?) AND state = 'claimed' AND lease_until <= ? AND attempts >= ?",
                                (_stored('failed', 'lease expired too many times'), queue, queue,
                                 self._clock(), self._max_attempts)).rowcount

    def claim(self, queue: str, lease_seconds: float = 60.0, kinds: Collection[str] | None = None) -> Claim | None:
        _require_name(queue, 'queue')
        if lease_seconds <= 0:
            raise ValueError('lease_seconds must be positive')
        now = self._clock()
        with self._db._transaction() as db:
            self.bury(queue, db=db)
            wanted = () if kinds is None else tuple(kinds)
            row = db.execute("SELECT id, kind, dedupe_key, payload, attempts FROM internal_jobs WHERE queue = ? AND "
                             "((state = 'queued' AND run_at <= ?) OR (state = 'claimed' AND lease_until <= ?))"
                             + ("" if kinds is None else " AND kind IN (" + ", ".join("?" * len(wanted)) + ")")
                             + " ORDER BY run_at, id LIMIT 1", (queue, now, now, *wanted)).fetchone()
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
                              "WHERE id = ? AND state = 'claimed' AND lease_token = ? AND lease_until > ?",
                              (_stored('ok', value), job_id, token, self._clock())).rowcount == 1

    def fail(self, job_id: int, token: str, reason: str, retry_in: float | None = None) -> bool:
        with self._db._transaction() as db:
            row = db.execute("SELECT attempts FROM internal_jobs WHERE id = ? AND state = 'claimed' AND lease_token = ? AND lease_until > ?", (job_id, token, self._clock())).fetchone()
            if row is None:
                return False
            state = 'failed' if retry_in is None else ('dead' if row[0] >= self._max_attempts else 'queued')
            db.execute("UPDATE internal_jobs SET state = ?, lease_token = NULL, lease_until = NULL, run_at = ?, result = ? WHERE id = ?",
                       (state, self._clock() + (max(retry_in, 30.0 * 2 ** (row[0] - 1)) if retry_in is not None else 0), _stored('failed', reason), job_id))
            return True

    def result(self, job_id: int, queue: str) -> JobResult | None:
        with self._db._transaction() as db:
            row = db.execute("SELECT state, result FROM internal_jobs WHERE id = ? AND queue = ?", (job_id, queue)).fetchone()
        if row is None or row[0] not in ('done', 'failed', 'dead', 'dismissed'):
            return None
        result = json.loads(row[1])
        return JobResult(result['status'], result.get('value'))


class InternalQueueClient:
    """Adapt the database queue to the same interface as QueueClient."""
    def __init__(self, queue: InternalJobQueue) -> None:
        self._queue = queue

    def claim(self, queue: str, lease_seconds: float = 60.0, kinds: Collection[str] | None = None) -> WorkerJob | None:
        claim = self._queue.claim(queue, lease_seconds, kinds)
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
