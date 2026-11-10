"""Durable job queue on SQLite (WAL). Standard library only.

- At-least-once: a job claimed by a worker that never acks is claimed again
  when its lease runs out.
- Deduped: one job per (queue, dedupe key), kept for good, so a redelivered
  event or tap never makes a second job.
- Named queues (`main`, `frameio`): a claim names its queue and sees only that
  queue's jobs.
- Results: `ack` or `fail` stores a typed result on the job. The caller of
  `submit` reads it back with `result`.
- Quiet-period timers live in the database, so they survive a restart. Each
  `arm_timer` call pushes the fire time out; when it passes, the timer becomes
  one job carrying how many times it was armed.

Time comes from a `clock` callable (seconds since the epoch), so tests use a
fake clock. Every operation opens its own connection and runs in one
`BEGIN IMMEDIATE` transaction.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
import json
from pathlib import Path
import secrets
import sqlite3
import time

from t6.domain.events import JsonValue

__all__ = ["Claim", "JobQueue", "JobResult", "QueueError", "Submitted"]

MAIN_QUEUE = "main"
FRAMEIO_QUEUE = "frameio"
DEFAULT_MAX_ATTEMPTS = 5

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    queue TEXT NOT NULL,
    kind TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    payload TEXT NOT NULL,
    run_at REAL NOT NULL,
    state TEXT NOT NULL,
    lease_token TEXT,
    lease_until REAL,
    attempts INTEGER NOT NULL DEFAULT 0,
    result TEXT,
    created_at REAL NOT NULL,
    UNIQUE (queue, dedupe_key)
);
CREATE INDEX IF NOT EXISTS jobs_claimable ON jobs (queue, state, run_at);
CREATE TABLE IF NOT EXISTS timers (
    queue TEXT NOT NULL,
    key TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    fire_at REAL NOT NULL,
    count INTEGER NOT NULL,
    PRIMARY KEY (queue, key)
);
CREATE TABLE IF NOT EXISTS seen (
    queue TEXT NOT NULL,
    key TEXT NOT NULL,
    PRIMARY KEY (queue, key)
);
"""


class QueueError(Exception):
    """A bad argument, such as an empty queue name."""


@dataclass(frozen=True, slots=True)
class Submitted:
    job_id: int
    created: bool
    """False when a job with this dedupe key already existed."""


@dataclass(frozen=True, slots=True)
class Claim:
    """A job leased to one worker. `token` must come back with `ack` or `fail`."""

    job_id: int
    queue: str
    kind: str
    dedupe_key: str
    payload: Mapping[str, JsonValue]
    token: str
    lease_until: float
    attempt: int


@dataclass(frozen=True, slots=True)
class JobResult:
    """The typed result of a finished job: `ok` with a value, or `failed` with a reason."""

    status: str
    value: JsonValue = None


class JobQueue:
    def __init__(
        self,
        path: str | Path,
        clock: Callable[[], float] = time.time,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        self._path = str(path)
        self._clock = clock
        self._max_attempts = max_attempts
        setup = sqlite3.connect(self._path, timeout=30, isolation_level=None)
        try:
            setup.execute("PRAGMA journal_mode=WAL")  # persists in the database file
        finally:
            setup.close()
        with self._transaction() as db:
            for statement in _SCHEMA.split(";"):
                if statement.strip():
                    db.execute(statement)

    @property
    def max_attempts(self) -> int:
        """A job that fails this many times is failed for good."""
        return self._max_attempts

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self._path, timeout=30, isolation_level=None)
        try:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")
        finally:
            db.close()

    # -- submit and read back ------------------------------------------------

    def submit(
        self,
        queue: str,
        kind: str,
        dedupe_key: str,
        payload: Mapping[str, JsonValue] | None = None,
        run_at: float | None = None,
    ) -> Submitted:
        """Add a job unless this queue already has one with `dedupe_key`."""
        _require_name(queue, "queue")
        _require_name(dedupe_key, "dedupe_key")
        now = self._clock()
        with self._transaction() as db:
            return _insert_job(db, queue, kind, dedupe_key, payload or {}, run_at or now, now)

    def owns(self, job_id: int, queue: str) -> bool:
        with self._transaction() as db:
            row = db.execute(
                "SELECT 1 FROM jobs WHERE id = ? AND queue = ?", (job_id, queue)
            ).fetchone()
        return row is not None

    def result(self, job_id: int, queue: str) -> JobResult | None:
        """The stored result, or None while the job has not finished or is not on `queue`."""
        with self._transaction() as db:
            row = db.execute(
                "SELECT result FROM jobs WHERE id = ? AND queue = ?", (job_id, queue)
            ).fetchone()
        if row is None or row[0] is None:
            return None
        stored = json.loads(row[0])
        return JobResult(status=stored["status"], value=stored.get("value"))

    # -- claim, extend, finish ----------------------------------------------

    def claim(self, queue: str, lease_seconds: float) -> Claim | None:
        """Lease the oldest due job on `queue`, or return None.

        A job whose lease ran out is claimable again. A job that has used all
        its attempts is failed instead of claimed.
        """
        _require_name(queue, "queue")
        now = self._clock()
        with self._transaction() as db:
            _promote_timers(db, now)
            db.execute(
                "UPDATE jobs SET state = 'failed', lease_token = NULL, result = ? "
                "WHERE queue = ? AND state = 'claimed' AND lease_until <= ? AND attempts >= ?",
                (
                    json.dumps({"status": "failed", "value": "lease expired too many times"}),
                    queue,
                    now,
                    self._max_attempts,
                ),
            )
            row = db.execute(
                "SELECT id, kind, dedupe_key, payload, attempts FROM jobs "
                "WHERE queue = ? AND ((state = 'pending' AND run_at <= ?) "
                "OR (state = 'claimed' AND lease_until <= ?)) ORDER BY run_at, id LIMIT 1",
                (queue, now, now),
            ).fetchone()
            if row is None:
                return None
            job_id, kind, dedupe_key, payload, attempts = row
            token = secrets.token_urlsafe(16)
            lease_until = now + lease_seconds
            db.execute(
                "UPDATE jobs SET state = 'claimed', lease_token = ?, lease_until = ?, "
                "attempts = attempts + 1 WHERE id = ?",
                (token, lease_until, job_id),
            )
            return Claim(
                job_id=job_id,
                queue=queue,
                kind=kind,
                dedupe_key=dedupe_key,
                payload=json.loads(payload),
                token=token,
                lease_until=lease_until,
                attempt=attempts + 1,
            )

    def extend(self, job_id: int, token: str, lease_seconds: float) -> bool:
        """Push the lease out. False when the claim is no longer this worker's."""
        now = self._clock()
        with self._transaction() as db:
            return (
                db.execute(
                    "UPDATE jobs SET lease_until = ? WHERE id = ? AND state = 'claimed' "
                    "AND lease_token = ? AND lease_until > ?",
                    (now + lease_seconds, job_id, token, now),
                ).rowcount
                == 1
            )

    def ack(self, job_id: int, token: str, value: JsonValue = None) -> bool:
        """Finish the job with an `ok` result. False when the claim is no longer this worker's.

        An ack is accepted after the lease ran out as long as nobody claimed
        the job again, so a slow worker's finished work is not thrown away.
        """
        return self._finish(job_id, token, JobResult("ok", value))

    def fail(
        self, job_id: int, token: str, reason: str, retry_in: float | None = None
    ) -> bool:
        """Report a failed attempt. With `retry_in` the job is tried again unless attempts ran out."""
        now = self._clock()
        with self._transaction() as db:
            row = db.execute(
                "SELECT attempts FROM jobs WHERE id = ? AND state = 'claimed' AND lease_token = ?",
                (job_id, token),
            ).fetchone()
            if row is None:
                return False
            if retry_in is not None and row[0] < self._max_attempts:
                db.execute(
                    "UPDATE jobs SET state = 'pending', lease_token = NULL, run_at = ? WHERE id = ?",
                    (now + retry_in, job_id),
                )
            else:
                db.execute(
                    "UPDATE jobs SET state = 'failed', lease_token = NULL, result = ? WHERE id = ?",
                    (json.dumps({"status": "failed", "value": reason}), job_id),
                )
            return True

    def _finish(self, job_id: int, token: str, result: JobResult) -> bool:
        with self._transaction() as db:
            return (
                db.execute(
                    "UPDATE jobs SET state = 'done', lease_token = NULL, result = ? "
                    "WHERE id = ? AND state = 'claimed' AND lease_token = ?",
                    (json.dumps({"status": result.status, "value": result.value}), job_id, token),
                ).rowcount
                == 1
            )

    # -- quiet-period timers --------------------------------------------------

    def arm_timer(
        self,
        queue: str,
        key: str,
        kind: str,
        quiet_seconds: float,
        payload: Mapping[str, JsonValue] | None = None,
        event_key: str | None = None,
    ) -> bool:
        """Start or reset the timer `key`; it fires `quiet_seconds` after the last call.

        When it fires it becomes one job whose payload is `payload` from the
        last call plus `count`, the number of calls since it started. Pass
        `event_key` to ignore a redelivered event: a key seen before changes
        nothing and the call returns False.
        """
        _require_name(queue, "queue")
        _require_name(key, "key")
        now = self._clock()
        with self._transaction() as db:
            if event_key is not None:
                added = db.execute(
                    "INSERT OR IGNORE INTO seen (queue, key) VALUES (?, ?)", (queue, event_key)
                ).rowcount
                if not added:
                    return False
            db.execute(
                "INSERT INTO timers (queue, key, kind, payload, fire_at, count) "
                "VALUES (?, ?, ?, ?, ?, 1) ON CONFLICT (queue, key) DO UPDATE SET "
                "kind = excluded.kind, payload = excluded.payload, "
                "fire_at = excluded.fire_at, count = count + 1",
                (queue, key, kind, json.dumps(dict(payload or {})), now + quiet_seconds),
            )
            return True

    def promote_timers(self) -> int:
        """Turn due timers into jobs now. `claim` does this too. Returns how many fired."""
        with self._transaction() as db:
            return _promote_timers(db, self._clock())


def _require_name(value: str, name: str) -> None:
    if not value.strip():
        raise QueueError(f"{name} must not be blank")


def _insert_job(
    db: sqlite3.Connection,
    queue: str,
    kind: str,
    dedupe_key: str,
    payload: Mapping[str, JsonValue],
    run_at: float,
    now: float,
) -> Submitted:
    cursor = db.execute(
        "INSERT OR IGNORE INTO jobs (queue, kind, dedupe_key, payload, run_at, state, created_at) "
        "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
        (queue, kind, dedupe_key, json.dumps(dict(payload)), run_at, now),
    )
    if cursor.rowcount:
        return Submitted(job_id=int(cursor.lastrowid or 0), created=True)
    row = db.execute(
        "SELECT id FROM jobs WHERE queue = ? AND dedupe_key = ?", (queue, dedupe_key)
    ).fetchone()
    return Submitted(job_id=row[0], created=False)


def _promote_timers(db: sqlite3.Connection, now: float) -> int:
    due = db.execute(
        "SELECT queue, key, kind, payload, fire_at, count FROM timers WHERE fire_at <= ?", (now,)
    ).fetchall()
    for queue, key, kind, payload, fire_at, count in due:
        body = {**json.loads(payload), "count": count}
        # The fire time is in the key: one timer key can fire again later, once per run.
        _insert_job(db, queue, kind, f"timer:{key}:{fire_at!r}", body, now, now)
        db.execute("DELETE FROM timers WHERE queue = ? AND key = ?", (queue, key))
    return len(due)
