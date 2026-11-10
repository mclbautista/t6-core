"""Durable operator actions on SQLite (WAL). Standard library only.

An action is one card Sixto shows the operator, with the choices it offers and
what became of them. The store keeps what must survive a restart: the opaque
token, which message carries the card, who may act, whether it was used, and the
result. Every operation is one `BEGIN IMMEDIATE` transaction, so two taps on one
card take turns and exactly one wins the claim.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
import json
import secrets
import sqlite3
import time

from t6.domain.events import JsonValue
from t6.registry.core import RegistryUnavailable

__all__ = ["Action", "ActionStore", "Claim"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS actions (
    token TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    stage TEXT NOT NULL,
    state TEXT NOT NULL,
    params TEXT NOT NULL,
    chat INTEGER NOT NULL,
    message_ref INTEGER,
    message_id INTEGER,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    claimed_by TEXT,
    operator INTEGER,
    project_id TEXT,
    result TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    shown_sync TEXT,
    offer_key TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_live_offer ON actions (offer_key) WHERE state IN ('pending', 'claimed');
CREATE INDEX IF NOT EXISTS by_project ON actions (project_id);
CREATE TABLE IF NOT EXISTS rejections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL,
    reason TEXT NOT NULL,
    operator INTEGER,
    token TEXT,
    detail TEXT NOT NULL
)
"""

_COLUMNS = (
    "token, kind, stage, state, params, chat, message_ref, message_id, created_at, expires_at, "
    "claimed_by, operator, project_id, result, version, shown_sync"
)


@dataclass(frozen=True, slots=True)
class Action:
    token: str
    kind: str
    """`unbound_page` or `classify`."""
    stage: str
    """`choose`, `pick` (a Project to bind to), or `done`."""
    state: str
    """`pending`: waiting for a tap. `claimed`: a tap is being carried out. `done`: used."""
    params: Mapping[str, JsonValue]
    chat: int
    """The chat the card was sent to. A tap must come from this chat."""
    message_ref: int | None
    """The gateway's reference for the card, once it was sent."""
    message_id: int | None
    """Telegram's message ID of the card, once the gateway confirmed it was sent."""
    created_at: float
    expires_at: float
    claimed_by: str | None
    operator: int | None
    """The Telegram user who used the card."""
    project_id: str | None
    result: Mapping[str, JsonValue] | None
    version: int
    """Goes up whenever the card must be shown again (a new stage, a result, a sync outcome)."""
    shown_sync: str | None
    """The Notion sync status the card last promised to show."""


@dataclass(frozen=True, slots=True)
class Claim:
    kind: str
    """`ok`, `replay` (the same tap again), `busy`, `handled`, `expired`, `stale` or `missing`."""
    action: Action | None


def _action(row: tuple[object, ...]) -> Action:
    return Action(
        token=str(row[0]),
        kind=str(row[1]),
        stage=str(row[2]),
        state=str(row[3]),
        params=json.loads(str(row[4])),
        chat=int(str(row[5])),
        message_ref=None if row[6] is None else int(str(row[6])),
        message_id=None if row[7] is None else int(str(row[7])),
        created_at=float(str(row[8])),
        expires_at=float(str(row[9])),
        claimed_by=None if row[10] is None else str(row[10]),
        operator=None if row[11] is None else int(str(row[11])),
        project_id=None if row[12] is None else str(row[12]),
        result=None if row[13] is None else json.loads(str(row[13])),
        version=int(str(row[14])),
        shown_sync=None if row[15] is None else str(row[15]),
    )


class ActionStore:
    def __init__(
        self, path: str, clock: Callable[[], float] = time.time, new_token: Callable[[], str] | None = None
    ) -> None:
        self._path = str(path)
        self._clock = clock
        self._new_token = new_token or (lambda: secrets.token_urlsafe(16))
        setup = sqlite3.connect(self._path, timeout=30, isolation_level=None)
        try:
            setup.execute("PRAGMA journal_mode=WAL")
        finally:
            setup.close()
        with self._transaction() as db:
            for statement in _SCHEMA.split(";"):
                if statement.strip():
                    db.execute(statement)

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        try:
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
        except sqlite3.OperationalError as problem:
            raise RegistryUnavailable(str(problem)) from problem

    def _read(self, db: sqlite3.Connection, token: str) -> Action | None:
        row = db.execute(f"SELECT {_COLUMNS} FROM actions WHERE token = ?", (token,)).fetchone()
        return None if row is None else _action(row)

    def create(
        self,
        kind: str,
        params: Mapping[str, JsonValue],
        *,
        chat: int,
        offer_key: str,
        ttl: float,
        project_id: str | None = None,
        shown_sync: str | None = None,
    ) -> tuple[Action, bool]:
        """A new pending action, or the live one already offered under `offer_key`.

        An offer stays live until it is used or has expired, so a repeated
        trigger (a second webhook for the same page) does not send a second card.
        """
        now = self._clock()
        with self._transaction() as db:
            live = db.execute(
                "SELECT token FROM actions WHERE offer_key = ? AND state IN ('pending', 'claimed') AND expires_at > ?",
                (offer_key, now),
            ).fetchone()
            if live is not None:
                found = self._read(db, str(live[0]))
                assert found is not None
                return found, False
            # An expired offer no longer counts; retire it so the unique index lets a new one in.
            db.execute(
                "UPDATE actions SET state = 'done', stage = 'done', result = ? WHERE offer_key = ? "
                "AND state IN ('pending', 'claimed')",
                (json.dumps({"outcome": "expired"}), offer_key),
            )
            token = self._new_token()
            db.execute(
                "INSERT INTO actions (token, kind, stage, state, params, chat, created_at, expires_at, project_id, "
                "shown_sync, offer_key) VALUES (?, ?, 'choose', 'pending', ?, ?, ?, ?, ?, ?, ?)",
                (token, kind, json.dumps(dict(params)), chat, now, now + ttl, project_id, shown_sync, offer_key),
            )
            created = self._read(db, token)
            assert created is not None
            return created, True

    def get(self, token: str) -> Action | None:
        with self._transaction() as db:
            return self._read(db, token)

    def delivered(self, token: str, message_ref: int, message_id: int) -> Action | None:
        with self._transaction() as db:
            db.execute(
                "UPDATE actions SET message_ref = ?, message_id = ? WHERE token = ?", (message_ref, message_id, token)
            )
            return self._read(db, token)

    def claim(self, token: str, tap_key: str, operator: int, stage: str) -> Claim:
        """Take the card for one tap. Exactly one tap gets `ok`; the same tap again gets `replay`."""
        now = self._clock()
        with self._transaction() as db:
            action = self._read(db, token)
            if action is None:
                return Claim("missing", None)
            if action.state == "done":
                return Claim("handled", action)
            if action.stage != stage:
                return Claim("stale", action)
            if action.state == "claimed":
                return Claim("replay" if action.claimed_by == tap_key else "busy", action)
            if now >= action.expires_at:
                return Claim("expired", action)
            db.execute(
                "UPDATE actions SET state = 'claimed', claimed_by = ?, operator = ? WHERE token = ?",
                (tap_key, operator, token),
            )
            return Claim("ok", self._read(db, token))

    def release(self, token: str) -> None:
        """Give a claimed card back, after a failure that changed nothing."""
        with self._transaction() as db:
            db.execute("UPDATE actions SET state = 'pending', claimed_by = NULL WHERE token = ? AND state = 'claimed'", (token,))

    def advance(self, token: str, *, stage: str, params: Mapping[str, JsonValue]) -> Action:
        """Move to the next stage: the card is shown again with new choices and can be tapped again."""
        with self._transaction() as db:
            db.execute(
                "UPDATE actions SET state = 'pending', claimed_by = NULL, stage = ?, params = ?, version = version + 1 "
                "WHERE token = ?",
                (stage, json.dumps(dict(params)), token),
            )
            moved = self._read(db, token)
            assert moved is not None
            return moved

    def finish(
        self, token: str, result: Mapping[str, JsonValue], *, project_id: str | None = None, sync: str | None = None
    ) -> Action:
        with self._transaction() as db:
            db.execute(
                "UPDATE actions SET state = 'done', stage = 'done', result = ?, version = version + 1, "
                "project_id = COALESCE(?, project_id), shown_sync = COALESCE(?, shown_sync) WHERE token = ?",
                (json.dumps(dict(result)), project_id, sync, token),
            )
            done = self._read(db, token)
            assert done is not None
            return done

    def undelivered(self) -> tuple[Action, ...]:
        """Cards never confirmed in Telegram. Their delivery job may be lost (a restore), so it is queued again."""
        with self._transaction() as db:
            rows = db.execute(
                f"SELECT {_COLUMNS} FROM actions WHERE message_id IS NULL AND state IN ('pending', 'claimed') "
                "AND expires_at > ? ORDER BY created_at",
                (self._clock(),),
            ).fetchall()
        return tuple(_action(row) for row in rows)

    def done_for_project(self, project_id: str) -> tuple[Action, ...]:
        with self._transaction() as db:
            rows = db.execute(
                f"SELECT {_COLUMNS} FROM actions WHERE project_id = ? AND state = 'done' AND result IS NOT NULL "
                "ORDER BY created_at",
                (project_id,),
            ).fetchall()
        return tuple(_action(row) for row in rows)

    def show_sync(self, token: str, sync: str) -> Action:
        """Record that the card is to show this sync status, and ask for it to be shown."""
        with self._transaction() as db:
            db.execute("UPDATE actions SET shown_sync = ?, version = version + 1 WHERE token = ?", (sync, token))
            shown = self._read(db, token)
            assert shown is not None
            return shown

    def reject(self, reason: str, *, operator: int | None, token: str | None, detail: str = "") -> None:
        with self._transaction() as db:
            db.execute(
                "INSERT INTO rejections (at, reason, operator, token, detail) VALUES (?, ?, ?, ?, ?)",
                (self._clock(), reason, operator, token, detail),
            )

    def rejections(self) -> tuple[tuple[str, int | None, str | None], ...]:
        """(reason, operator, token) of every rejected tap, oldest first."""
        with self._transaction() as db:
            rows = db.execute("SELECT reason, operator, token FROM rejections ORDER BY id").fetchall()
        return tuple((str(r[0]), None if r[1] is None else int(r[1]), None if r[2] is None else str(r[2])) for r in rows)
