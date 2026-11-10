"""The durable Project registry on SQLite (WAL). Standard library only.

Run one instance for all of T6: code reservations (t6-core#69) are only
T6-wide when every writer uses the same file. Every operation opens its own
connection and runs in one `BEGIN IMMEDIATE` transaction, so a registration
commits its Project, binding, audit entries and sync intent together or not
at all.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
import json
from pathlib import Path
import secrets
import sqlite3
import time

from t6.registry.core import (
    NOTION,
    PROJECT_ID_PREFIX,
    AuditEntry,
    KeyReused,
    PageAlreadyBound,
    PageBinding,
    RegisterOutcome,
    Registered,
    RegisteredProject,
    RegistryError,
    Rotation,
    SyncIntent,
    SyncStatus,
    normalize_page_id,
)

__all__ = ["SqliteProjectRegistry"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    code TEXT,
    rotation TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS bindings (
    system TEXT NOT NULL,
    page_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    bound_at REAL NOT NULL,
    PRIMARY KEY (system, page_id)
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    at REAL NOT NULL,
    before TEXT,
    after TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sync_intents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    created_at REAL NOT NULL,
    done_at REAL
);
CREATE TABLE IF NOT EXISTS requests (
    idempotency_key TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    project_id TEXT NOT NULL
);
"""


def _new_id() -> str:
    return PROJECT_ID_PREFIX + secrets.token_hex(12)


class SqliteProjectRegistry:
    def __init__(
        self,
        path: str | Path,
        clock: Callable[[], float] = time.time,
        new_id: Callable[[], str] = _new_id,
    ) -> None:
        self._path = str(path)
        self._clock = clock
        self._new_id = new_id
        setup = sqlite3.connect(self._path, timeout=30, isolation_level=None)
        try:
            setup.execute("PRAGMA journal_mode=WAL")  # persists in the database file
        finally:
            setup.close()
        with self._transaction() as db:
            for statement in _SCHEMA.split(";"):
                if statement.strip():
                    db.execute(statement)

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

    def register(
        self, *, actor: str, idempotency_key: str, notion_page_id: str | None = None
    ) -> RegisterOutcome:
        if not actor.strip():
            raise RegistryError("actor must not be blank")
        if not idempotency_key.strip():
            raise RegistryError("idempotency_key must not be blank")
        page = None if notion_page_id is None else normalize_page_id(notion_page_id)
        if page == "":
            raise RegistryError("notion_page_id must not be blank")
        fingerprint = json.dumps({"actor": actor, "notion_page_id": page})
        now = self._clock()
        with self._transaction() as db:
            seen = db.execute(
                "SELECT fingerprint, project_id FROM requests WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if seen is not None:
                if seen[0] != fingerprint:
                    return KeyReused(idempotency_key)
                original = _read(db, seen[1])
                assert original is not None
                return Registered(original, created=False)
            if page is not None:
                holder = db.execute(
                    "SELECT project_id FROM bindings WHERE system = ? AND page_id = ?", (NOTION, page)
                ).fetchone()
                if holder is not None:
                    return PageAlreadyBound(page, holder[0])
            project_id = self._new_id()
            db.execute("INSERT INTO requests VALUES (?, ?, ?)", (idempotency_key, fingerprint, project_id))
            if page is not None:
                db.execute("INSERT INTO bindings VALUES (?, ?, ?, ?)", (NOTION, page, project_id, now))
            db.execute(
                "INSERT INTO projects (id, revision, code, rotation, created_at) VALUES (?, 1, NULL, ?, ?)",
                (project_id, Rotation.OUT.value, now),
            )
            _audit(db, project_id, "registered", actor, now, {"code": None, "rotation": Rotation.OUT.value})
            if page is not None:
                _audit(db, project_id, "page_bound", actor, now, {"system": NOTION, "page_id": page})
            db.execute(
                "INSERT INTO sync_intents (project_id, revision, created_at) VALUES (?, 1, ?)",
                (project_id, now),
            )
            made = _read(db, project_id)
            assert made is not None
            return Registered(made, created=True)

    def get(self, project_id: str) -> RegisteredProject | None:
        with self._transaction() as db:
            return _read(db, project_id)

    def audit(self, project_id: str) -> tuple[AuditEntry, ...]:
        with self._transaction() as db:
            rows = db.execute(
                "SELECT project_id, action, actor, at, before, after FROM audit WHERE project_id = ? ORDER BY id",
                (project_id,),
            ).fetchall()
        return tuple(
            AuditEntry(row[0], row[1], row[2], row[3], None if row[4] is None else json.loads(row[4]), json.loads(row[5]))
            for row in rows
        )

    def pending_sync(self) -> tuple[SyncIntent, ...]:
        with self._transaction() as db:
            rows = db.execute(
                "SELECT id, project_id, revision, created_at FROM sync_intents WHERE done_at IS NULL ORDER BY id"
            ).fetchall()
        return tuple(SyncIntent(*row) for row in rows)


def _audit(
    db: sqlite3.Connection, project_id: str, action: str, actor: str, at: float, after: dict[str, str | None]
) -> None:
    db.execute(
        "INSERT INTO audit (project_id, action, actor, at, before, after) VALUES (?, ?, ?, ?, NULL, ?)",
        (project_id, action, actor, at, json.dumps(after)),
    )


def _read(db: sqlite3.Connection, project_id: str) -> RegisteredProject | None:
    row = db.execute(
        "SELECT id, revision, code, rotation, created_at FROM projects WHERE id = ?", (project_id,)
    ).fetchone()
    if row is None:
        return None
    bindings = db.execute(
        "SELECT system, page_id, bound_at FROM bindings WHERE project_id = ? ORDER BY bound_at, page_id",
        (project_id,),
    ).fetchall()
    pending = db.execute(
        "SELECT 1 FROM sync_intents WHERE project_id = ? AND done_at IS NULL LIMIT 1", (project_id,)
    ).fetchone()
    return RegisteredProject(
        id=row[0],
        revision=row[1],
        code=row[2],
        rotation=Rotation(row[3]),
        bindings=tuple(PageBinding(*binding) for binding in bindings),
        sync=SyncStatus.PENDING if pending else SyncStatus.SYNCED,
        created_at=row[4],
    )
