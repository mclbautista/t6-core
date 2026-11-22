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
  one job carrying how many times it was armed. A caller can also give the time
  of each call (`at`); the job then carries the first and the last of them.

Time comes from a `clock` callable (seconds since the epoch), so tests use a
fake clock. Every operation opens its own connection and runs in one
`BEGIN IMMEDIATE` transaction.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
import json
from pathlib import Path
import secrets
import sqlite3
import time

from t6.domain.events import JsonValue

__all__ = ["Claim", "Dismissal", "FailedJob", "JobQueue", "JobResult", "QueueError", "Submitted", "Waiting"]

MAIN_QUEUE = "main"
FRAMEIO_QUEUE = "frameio"
DIAGNOSTIC_QUEUE = "diagnostic"
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
    first_at REAL,
    last_at REAL,
    PRIMARY KEY (queue, key)
);
CREATE TABLE IF NOT EXISTS dismissals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    queue TEXT NOT NULL,
    kind TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    failure TEXT,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    at REAL NOT NULL
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


@dataclass(frozen=True, slots=True)
class Waiting:
    """A job nobody has finished and nobody holds a live lease on."""

    job_id: int
    kind: str
    dedupe_key: str
    payload: Mapping[str, JsonValue]


@dataclass(frozen=True, slots=True)
class FailedJob:
    """A job that failed for good and was not dismissed."""

    job_id: int
    queue: str
    kind: str
    dedupe_key: str
    reason: str | None
    """The failure the job stored."""


@dataclass(frozen=True, slots=True)
class Dismissal:
    """The record of one dismissed job: who dismissed it, when, and why."""

    job_id: int
    queue: str
    kind: str
    dedupe_key: str
    failure: str | None
    actor: str
    reason: str
    at: float


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
            # Timers stored before first_at/last_at existed: add the columns.
            have = {row[1] for row in db.execute("PRAGMA table_info(timers)")}
            for column in ("first_at", "last_at"):
                if column not in have:
                    db.execute(f"ALTER TABLE timers ADD COLUMN {column} REAL")

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

    def waiting(self, queue: str) -> list[Waiting]:
        """The jobs on `queue` that are waiting to be claimed (pending, or claimed with a lease that ran out)."""
        _require_name(queue, "queue")
        now = self._clock()
        with self._transaction() as db:
            rows = db.execute(
                "SELECT id, kind, dedupe_key, payload FROM jobs WHERE queue = ? AND "
                "(state = 'pending' OR (state = 'claimed' AND lease_until <= ?)) ORDER BY id",
                (queue, now),
            ).fetchall()
        return [Waiting(row[0], row[1], row[2], json.loads(row[3])) for row in rows]

    def hand_over(self, job_id: int, destination: str) -> bool:
        """Retire a waiting job that now lives in `destination`, so no claim here finds it again.

        The job keeps its row, so its dedupe key stays taken. False when it is no longer waiting.
        """
        with self._transaction() as db:
            return (
                db.execute(
                    "UPDATE jobs SET state = 'done', lease_token = NULL, lease_until = NULL, result = ? "
                    "WHERE id = ? AND (state = 'pending' OR state = 'claimed')",
                    (json.dumps({"status": "ok", "value": {"handed_over_to": destination}}), job_id),
                ).rowcount
                == 1
            )

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

    def claim(self, queue: str, lease_seconds: float, kinds: Collection[str] | None = None) -> Claim | None:
        """Lease the oldest due job on `queue`, or return None. With `kinds`, only jobs of those kinds are looked at.

        A job whose lease ran out is claimable again. A job that has used all
        its attempts is failed instead of claimed.
        """
        _require_name(queue, "queue")
        now = self._clock()
        with self._transaction() as db:
            _promote_timers(db, now, queue if queue == DIAGNOSTIC_QUEUE else None)
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
            wanted = () if kinds is None else tuple(kinds)
            row = db.execute(
                "SELECT id, kind, dedupe_key, payload, attempts FROM jobs "
                "WHERE queue = ? AND ((state = 'pending' AND run_at <= ?) "
                "OR (state = 'claimed' AND lease_until <= ?))"
                + ("" if kinds is None else " AND kind IN (" + ", ".join("?" * len(wanted)) + ")")
                + " ORDER BY run_at, id LIMIT 1",
                (queue, now, now, *wanted),
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

    # -- failed jobs ----------------------------------------------------------

    def failed(self) -> list[FailedJob]:
        """The jobs that failed for good, oldest first. A dismissed job is not listed."""
        with self._transaction() as db:
            rows = db.execute("SELECT id, queue, kind, dedupe_key, result FROM jobs WHERE state = 'failed' ORDER BY id").fetchall()
        return [FailedJob(row[0], row[1], row[2], row[3], _failure(row[4])) for row in rows]

    def dismiss(self, job_id: int, *, actor: str, reason: str) -> bool:
        """Dismiss a failed job: it is never run again and no longer counts as failed.

        The job keeps its row, its stored failure and its dedupe key (so a redelivered event still makes no second job);
        its state becomes `dismissed`. Who dismissed it and why is kept in `dismissals`, in the same transaction.
        False when the job does not exist or has not failed. `ValueError` for a blank actor or reason.
        """
        if not actor.strip() or not reason.strip():
            raise ValueError("an actor and a reason are required")
        with self._transaction() as db:
            row = db.execute("SELECT queue, kind, dedupe_key, result FROM jobs WHERE id = ? AND state = 'failed'", (job_id,)).fetchone()
            if row is None:
                return False
            db.execute("UPDATE jobs SET state = 'dismissed' WHERE id = ?", (job_id,))
            db.execute("INSERT INTO dismissals (job_id, queue, kind, dedupe_key, failure, actor, reason, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                       (job_id, row[0], row[1], row[2], _failure(row[3]), actor.strip(), reason.strip(), self._clock()))
            return True

    def dismissals(self) -> list[Dismissal]:
        """Every dismissal, oldest first."""
        with self._transaction() as db:
            rows = db.execute("SELECT job_id, queue, kind, dedupe_key, failure, actor, reason, at FROM dismissals ORDER BY id").fetchall()
        return [Dismissal(*row) for row in rows]

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
        at: float | None = None,
    ) -> bool:
        """Start or reset the timer `key`; it fires `quiet_seconds` after the last call.

        A call that would fire the timer EARLIER than it is already set to keeps
        the later time: the longest wait wins. With one `quiet_seconds` for every
        call this never happens, because each call is later than the one before.

        When it fires it becomes one job whose payload is `payload` from the
        last call plus `count`, the number of calls since it started. Pass
        `at`, the caller's own time for this call (not the server clock), and
        the payload also gets `first_at` and `last_at`: the first call's `at`
        and the latest call's `at`. Pass
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
            existing = db.execute(
                "SELECT payload FROM timers WHERE queue = ? AND key = ?", (queue, key)
            ).fetchone()
            merged: dict[str, JsonValue] = dict(json.loads(existing[0])) if existing is not None else {}
            merged.update(payload or {})
            db.execute(
                "INSERT INTO timers (queue, key, kind, payload, fire_at, count, first_at, last_at) "
                "VALUES (?, ?, ?, ?, ?, 1, ?, ?) ON CONFLICT (queue, key) DO UPDATE SET "
                "kind = excluded.kind, payload = excluded.payload, "
                "fire_at = MAX(fire_at, excluded.fire_at), count = count + 1, "
                "first_at = COALESCE(first_at, excluded.first_at), last_at = excluded.last_at",
                (queue, key, kind, json.dumps(merged), now + quiet_seconds, at, at),
            )
            return True

    def promote_timers(self) -> int:
        """Turn due timers into jobs now. `claim` does this too. Returns how many fired."""
        with self._transaction() as db:
            return _promote_timers(db, self._clock())


def _failure(stored: str | None) -> str | None:
    value = None if stored is None else json.loads(stored).get("value")
    return None if value is None else str(value)


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


def _promote_timers(db: sqlite3.Connection, now: float, only_queue: str | None = None) -> int:
    due = db.execute(
        "SELECT queue, key, kind, payload, fire_at, count, first_at, last_at FROM timers "
        "WHERE fire_at <= ? AND (? IS NULL OR queue = ?)", (now, only_queue, only_queue)
    ).fetchall()
    for queue, key, kind, payload, fire_at, count, first_at, last_at in due:
        body = {**json.loads(payload), "count": count}
        if first_at is not None:
            body["first_at"], body["last_at"] = first_at, last_at
        # The fire time is in the key: one timer key can fire again later, once per run.
        _insert_job(db, queue, kind, f"timer:{key}:{fire_at!r}", body, now, now)
        db.execute("DELETE FROM timers WHERE queue = ? AND key = ?", (queue, key))
    return len(due)
