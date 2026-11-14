"""The durable Project registry on SQLite (WAL). Standard library only.

Run one instance for all of T6: code reservations are only T6-wide when every
writer uses the same file. Every operation opens its own connection and runs
in one `BEGIN IMMEDIATE` transaction, so writers take turns and a change
commits its Project state, audit entry and sync intent together or not at
all. A refused change writes nothing.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
import json
import sys
from pathlib import Path
import sqlite3
import time
from typing import TypeAlias

from t6.domain.events import JsonValue
from t6.ids import EntityKind, T6Id
from t6.registry.core import (
    NOTION,
    ActivityEntry,
    AuditEntry,
    Changed,
    ChangeOutcome,
    CodeHeld,
    KeyReused,
    NotFound,
    Observed,
    PageAlreadyBound,
    PageBinding,
    RegisterOutcome,
    Registered,
    RegisteredProject,
    RegistryError,
    RegistryUnavailable,
    ReviewItem,
    RevisionConflict,
    Rotation,
    Shown,
    SyncIntent,
    SyncStatus,
    normalize_code,
    normalize_page_id,
)

__all__ = ["SqliteProjectRegistry"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    code TEXT,
    code_form TEXT,
    rotation TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_holder_per_code ON projects (code_form)
    WHERE rotation = 'in' AND code_form IS NOT NULL;
CREATE TABLE IF NOT EXISTS bindings (
    system TEXT NOT NULL,
    page_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    bound_at REAL NOT NULL,
    active INTEGER NOT NULL,
    available INTEGER NOT NULL DEFAULT 1,
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
    done_at REAL,
    dispatched_at REAL,
    error TEXT,
    error_final INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS review_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    page_id TEXT NOT NULL,
    project_id TEXT,
    detail TEXT NOT NULL,
    created_at REAL NOT NULL,
    resolved_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_open_review_item ON review_items (kind, page_id)
    WHERE resolved_at IS NULL;
CREATE TABLE IF NOT EXISTS requests (
    idempotency_key TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    project_id TEXT NOT NULL
);
"""

# Each migration runs in the same transaction as its version record. Version 1
# adopts the previous release's schema. bindings.project_id stores IDs of any entity kind.
_MIGRATIONS = (
    (1, False, _SCHEMA),
    (2, False, ""),
    (3, False, """CREATE TABLE activity (
        id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL,
        subject_id TEXT, at REAL NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
        summary TEXT NOT NULL, audit_id INTEGER REFERENCES audit(id)
    );
    INSERT INTO activity (project_id, at, actor, action, summary, audit_id)
        SELECT project_id, at, actor, action, REPLACE(action, '_', ' '), id FROM audit;
    CREATE INDEX activity_project_order ON activity (project_id, at DESC, id DESC)"""),
    (4, False, """
        CREATE TABLE internal_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, queue TEXT NOT NULL, kind TEXT NOT NULL,
            dedupe_key TEXT NOT NULL, payload TEXT NOT NULL, run_at REAL NOT NULL,
            state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            lease_token TEXT, lease_until REAL, result TEXT, created_at REAL NOT NULL,
            UNIQUE (kind, dedupe_key)
        );
        CREATE INDEX internal_jobs_claimable ON internal_jobs (queue, state, run_at)
    """),
    (5, False, """
        CREATE TABLE project_hold_state (
            project_id TEXT PRIMARY KEY REFERENCES projects(id),
            held INTEGER NOT NULL, reason TEXT
        );
        CREATE TABLE used_preview_tokens (nonce TEXT PRIMARY KEY);
        CREATE TABLE verb_requests (
            idempotency_key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, result TEXT NOT NULL
        );
        CREATE TABLE status_sync_intents (
            intent_id INTEGER PRIMARY KEY REFERENCES sync_intents(id),
            held INTEGER NOT NULL
        )
    """),
    (6, False, """
        CREATE TABLE persons (
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, departments TEXT NOT NULL,
            roles TEXT NOT NULL, telegram_id INTEGER, master_brief INTEGER NOT NULL,
            active INTEGER NOT NULL
        );
        CREATE UNIQUE INDEX one_active_person_per_telegram ON persons (telegram_id)
            WHERE active = 1 AND telegram_id IS NOT NULL
    """),
    (7, False, """
        CREATE TABLE person_migration_receipts (digest TEXT PRIMARY KEY, contacts TEXT NOT NULL, entries TEXT NOT NULL);
        CREATE TABLE persons_v7 (
            id TEXT PRIMARY KEY, kind TEXT, departments TEXT NOT NULL,
            roles TEXT NOT NULL, telegram_id INTEGER, master_brief INTEGER NOT NULL,
            active INTEGER NOT NULL
        );
        INSERT INTO persons_v7 SELECT * FROM persons;
        UPDATE persons_v7 SET kind = NULL WHERE kind = 'staff' AND id IN
            (SELECT project_id FROM audit WHERE action = 'person_migrated');
        DROP TABLE persons;
        ALTER TABLE persons_v7 RENAME TO persons;
        CREATE UNIQUE INDEX one_active_person_per_telegram ON persons (telegram_id)
            WHERE active = 1 AND telegram_id IS NOT NULL
    """),
    (8, False, """
        CREATE TABLE person_permissions (
            person_id TEXT NOT NULL REFERENCES persons(id), permission TEXT NOT NULL,
            PRIMARY KEY (person_id, permission)
        );
        CREATE TABLE permission_seed (id INTEGER PRIMARY KEY, unmatched TEXT NOT NULL)
    """),
)


def _migrate(db: sqlite3.Connection) -> None:
    db.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, forward_only INTEGER NOT NULL)")
    row = db.execute("SELECT MAX(version) FROM schema_version").fetchone()
    current = row[0] or 0
    if db.execute("SELECT 1 FROM schema_version WHERE version > ? AND forward_only != 0", (_MIGRATIONS[-1][0],)).fetchone():
        raise RegistryError(f"unsupported database schema version: {current}")
    for version, forward_only, sql in _MIGRATIONS:
        if version > current:
            for statement in sql.split(";"):
                if statement.strip():
                    db.execute(statement)
            if version == 3:
                for table in ("audit", "activity"):
                    for operation in ("UPDATE", "DELETE"):
                        db.execute(f"CREATE TRIGGER {table}_no_{operation.lower()} BEFORE {operation} ON {table} "
                                   f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END")
            db.execute("INSERT INTO schema_version VALUES (?, ?)", (version, int(forward_only)))

    db.execute("INSERT INTO activity (project_id, at, actor, action, summary, audit_id) "
               "SELECT project_id, at, actor, action, REPLACE(action, '_', ' '), id FROM audit "
               "WHERE NOT EXISTS (SELECT 1 FROM activity WHERE activity.audit_id = audit.id)")


_Values: TypeAlias = Mapping[str, JsonValue]
_Step: TypeAlias = Callable[
    [sqlite3.Connection, RegisteredProject, float],
    "tuple[str, _Values, _Values] | CodeHeld | PageAlreadyBound | None",
]
"""One change inside a transaction: what to audit (action, before, after), a
refusal, or `None` when the Project already has the value. A step that refuses
must not have written anything."""


def _new_id() -> str:
    return str(T6Id.mint(EntityKind.PROJECT))


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
            _migrate(db)
            if not db.execute('SELECT 1 FROM permission_seed WHERE id = 1').fetchone():
                from t6.domain.studio_people import operator_ids
                unmatched = []
                for telegram_id in dict.fromkeys(operator_ids()):
                    people = db.execute('SELECT id FROM persons WHERE telegram_id = ?', (telegram_id,)).fetchall()
                    if not people:
                        unmatched.append(telegram_id)
                    for (person_id,) in people:
                        db.execute("INSERT OR IGNORE INTO person_permissions VALUES (?, 'operator')", (person_id,))
                        _audit(db, person_id, 'person_grant', 'operator seed', self._clock(), None, {'permission': 'operator'})
                db.execute('INSERT INTO permission_seed VALUES (1, ?)', (json.dumps(unmatched),))
                if unmatched:
                    print('Unmatched T6_OPERATOR_IDS: ' + ', '.join(map(str, unmatched)), file=sys.stderr)

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

    def bind_entity(self, system: str, page_id: str, entity_id: T6Id, *, actor: str) -> None:
        """Bind an external page, retaining any previous page as legacy."""
        if not isinstance(entity_id, T6Id) or entity_id.kind is EntityKind.PROJECT:
            raise ValueError("bind non-Project entities with a T6Id; use the registry for Projects")
        if not system.strip() or not page_id.strip() or not actor.strip():
            raise ValueError("system, page_id and actor must not be blank")
        page_id = _page(page_id) if system == NOTION else page_id
        with self._transaction() as db:
            row = db.execute(
                "SELECT project_id FROM bindings WHERE system = ? AND page_id = ?", (system, page_id)
            ).fetchone()
            if row is not None and row[0] != str(entity_id):
                raise ValueError("page is already bound")
            active = db.execute("SELECT page_id FROM bindings WHERE system = ? AND project_id = ? AND active = 1", (system, str(entity_id))).fetchone()
            if active is not None and active[0] == page_id:
                return
            now = self._clock()
            db.execute("UPDATE bindings SET active = 0 WHERE system = ? AND project_id = ?", (system, str(entity_id)))
            if row is None:
                db.execute(
                    "INSERT INTO bindings (system, page_id, project_id, bound_at, active) VALUES (?, ?, ?, ?, 1)",
                    (system, page_id, str(entity_id), now),
                )
            else:
                db.execute("UPDATE bindings SET active = 1 WHERE system = ? AND page_id = ?", (system, page_id))
            _audit(db, str(entity_id), "page_bound", actor, now, {"page_id": None if active is None else active[0]}, {"page_id": page_id})

    def bound_entity(self, system: str, page_id: str) -> str | None:
        """Resolve active and legacy external pages to their entity ID."""
        page_id = _page(page_id) if system == NOTION else page_id
        with self._transaction() as db:
            row = db.execute("SELECT project_id FROM bindings WHERE system = ? AND page_id = ?", (system, page_id)).fetchone()
            return None if row is None else str(row[0])

    # -- register ------------------------------------------------------------

    def register(
        self,
        *,
        actor: str,
        idempotency_key: str,
        notion_page_id: str | None = None,
        code: str | None = None,
        rotation: Rotation = Rotation.OUT,
    ) -> RegisterOutcome:
        _require(actor, idempotency_key)
        page = None if notion_page_id is None else _page(notion_page_id)
        entered, form = normalize_code(code)
        fingerprint = json.dumps(
            {"actor": actor, "register": True, "notion_page_id": page, "code": entered, "rotation": rotation.value}
        )
        now = self._clock()
        with self._transaction() as db:
            seen = _seen(db, idempotency_key)
            if seen is not None:
                if seen[0] != fingerprint:
                    return KeyReused(idempotency_key)
                return Registered(_must_read(db, seen[1]), created=False)
            if page is not None:
                holder = _page_holder(db, page)
                if holder is not None:
                    return PageAlreadyBound(page, holder)
            if rotation is Rotation.IN and entered is not None and form is not None:
                held = _code_held(db, entered, form, "")
                if held is not None:
                    return held
            project_id = self._new_id()
            db.execute("INSERT INTO requests VALUES (?, ?, ?)", (idempotency_key, fingerprint, project_id))
            if page is not None:
                _bind(db, page, project_id, now)
            db.execute(
                "INSERT INTO projects (id, revision, code, code_form, rotation, created_at) VALUES (?, 1, ?, ?, ?, ?)",
                (project_id, entered, form, rotation.value, now),
            )
            _audit(db, project_id, "registered", actor, now, None, {"code": entered, "rotation": rotation.value})
            if page is not None:
                _audit(db, project_id, "page_bound", actor, now, {"page_id": None}, {"page_id": page})
            _intend_sync(db, project_id, 1, now)
            return Registered(_must_read(db, project_id), created=True)

    # -- changes -------------------------------------------------------------

    def set_code(
        self, project_id: str, code: str | None, *, actor: str, idempotency_key: str, expected_revision: int
    ) -> ChangeOutcome:
        entered, form = normalize_code(code)

        def step(
            db: sqlite3.Connection, project: RegisteredProject, now: float
        ) -> tuple[str, _Values, _Values] | CodeHeld | None:
            if entered == project.code:
                return None
            if project.rotation is Rotation.IN and entered is not None and form is not None:
                held = _code_held(db, entered, form, project.id)
                if held is not None:
                    return held
            db.execute("UPDATE projects SET code = ?, code_form = ? WHERE id = ?", (entered, form, project.id))
            return "code_changed", {"code": project.code}, {"code": entered}

        return self._change(project_id, actor, idempotency_key, expected_revision, {"code": entered}, step)

    def set_rotation(
        self, project_id: str, rotation: Rotation, *, actor: str, idempotency_key: str, expected_revision: int
    ) -> ChangeOutcome:
        if rotation is Rotation.UNCLASSIFIED:
            raise RegistryError("a Project is classified as in or out of rotation, never back to unclassified")

        def step(
            db: sqlite3.Connection, project: RegisteredProject, now: float
        ) -> tuple[str, _Values, _Values] | CodeHeld | None:
            if rotation is project.rotation:
                return None
            entered, form = normalize_code(project.code)
            if rotation is Rotation.IN and entered is not None and form is not None:
                held = _code_held(db, entered, form, project.id)
                if held is not None:
                    return held
            db.execute("UPDATE projects SET rotation = ? WHERE id = ?", (rotation.value, project.id))
            action = "classified" if project.rotation is Rotation.UNCLASSIFIED else "rotation_changed"
            return action, {"rotation": project.rotation.value}, {"rotation": rotation.value}

        return self._change(project_id, actor, idempotency_key, expected_revision, {"rotation": rotation.value}, step)

    def bind_page(
        self, project_id: str, notion_page_id: str, *, actor: str, idempotency_key: str, expected_revision: int
    ) -> ChangeOutcome:
        page = _page(notion_page_id)

        def step(
            db: sqlite3.Connection, project: RegisteredProject, now: float
        ) -> tuple[str, _Values, _Values] | PageAlreadyBound | None:
            holder = _page_holder(db, page)
            if holder is not None and holder != project.id:
                return PageAlreadyBound(page, holder)
            active = next((binding.page_id for binding in project.bindings if binding.active), None)
            if active == page:
                return None
            db.execute("UPDATE bindings SET active = 0 WHERE project_id = ?", (project.id,))
            if holder is None:
                _bind(db, page, project.id, now)
            else:
                db.execute("UPDATE bindings SET active = 1 WHERE system = ? AND page_id = ?", (NOTION, page))
            return "page_bound", {"page_id": active}, {"page_id": page}

        return self._change(project_id, actor, idempotency_key, expected_revision, {"notion_page_id": page}, step)

    def _change(
        self,
        project_id: str,
        actor: str,
        idempotency_key: str,
        expected_revision: int,
        request: Mapping[str, JsonValue],
        step: _Step,
    ) -> ChangeOutcome:
        _require(actor, idempotency_key)
        fingerprint = json.dumps({"actor": actor, "project": project_id, **request})
        now = self._clock()
        with self._transaction() as db:
            seen = _seen(db, idempotency_key)
            if seen is not None:
                if seen[0] != fingerprint:
                    return KeyReused(idempotency_key)
                return Changed(_must_read(db, seen[1]), changed=False, replayed=True)
            project = _read(db, project_id)
            if project is None:
                return NotFound(project_id)
            if project.revision != expected_revision:
                return RevisionConflict(project_id, project.revision)
            result = step(db, project, now)
            if result is None:
                return Changed(project, changed=False)
            if not isinstance(result, tuple):
                return result
            action, before, after = result
            revision = project.revision + 1
            db.execute("UPDATE projects SET revision = ? WHERE id = ?", (revision, project_id))
            _audit(db, project_id, action, actor, now, before, after)
            _intend_sync(db, project_id, revision, now)
            db.execute("INSERT INTO requests VALUES (?, ?, ?)", (idempotency_key, fingerprint, project_id))
            return Changed(_must_read(db, project_id), changed=True)

    # -- reads ---------------------------------------------------------------

    def get(self, project_id: str) -> RegisteredProject | None:
        with self._transaction() as db:
            return _read(db, project_id)

    def list_projects(self, rotation: Rotation | None = None) -> tuple[RegisteredProject, ...]:
        with self._transaction() as db:
            if rotation is None:
                rows = db.execute("SELECT id FROM projects ORDER BY rowid").fetchall()
            else:
                rows = db.execute(
                    "SELECT id FROM projects WHERE rotation = ? ORDER BY rowid", (rotation.value,)
                ).fetchall()
            return tuple(_must_read(db, row[0]) for row in rows)

    def get_by_page(self, notion_page_id: str) -> RegisteredProject | None:
        with self._transaction() as db:
            holder = _page_holder(db, normalize_page_id(notion_page_id))
            return None if holder is None else _read(db, holder)

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

    def activity(self, project_id: str, *, since: float | None = None, limit: int | None = None) -> tuple[ActivityEntry, ...]:
        if limit is not None and limit < 1:
            raise ValueError("limit must be positive")
        with self._transaction() as db:
            rows = db.execute(
                "SELECT project_id, subject_id, at, actor, action, summary, audit_id FROM activity "
                "WHERE project_id = ? AND (? IS NULL OR at >= ?) ORDER BY at DESC, id DESC LIMIT ?",
                (project_id, since, since, -1 if limit is None else limit),
            ).fetchall()
        return tuple(ActivityEntry(*row) for row in rows)

    def pending_sync(self) -> tuple[SyncIntent, ...]:
        return self._intents("done_at IS NULL")

    # -- sync and reconciliation ---------------------------------------------

    def undispatched_sync(self) -> tuple[SyncIntent, ...]:
        return self._intents("done_at IS NULL AND dispatched_at IS NULL AND id NOT IN (SELECT intent_id FROM status_sync_intents)")

    def _intents(self, where: str) -> tuple[SyncIntent, ...]:
        with self._transaction() as db:
            rows = db.execute(
                f"SELECT id, project_id, revision, created_at, "
                f"(SELECT held FROM status_sync_intents WHERE intent_id = sync_intents.id) "
                f"FROM sync_intents WHERE {where} ORDER BY id"
            ).fetchall()
        return tuple(SyncIntent(row[0], row[1], row[2], row[3], None if row[4] is None else bool(row[4])) for row in rows)

    def mark_dispatched(self, intent_ids: Iterable[int]) -> None:
        now = self._clock()
        with self._transaction() as db:
            db.executemany(
                "UPDATE sync_intents SET dispatched_at = ? WHERE id = ? AND dispatched_at IS NULL",
                [(now, intent_id) for intent_id in intent_ids],
            )

    def report_sync(
        self, project_id: str, revision: int, *, ok: bool, detail: str = "", final: bool = False
    ) -> RegisteredProject | None:
        now = self._clock()
        with self._transaction() as db:
            covered = "project_id = ? AND done_at IS NULL AND revision <= ? AND id NOT IN (SELECT intent_id FROM status_sync_intents)"
            if ok:
                db.execute(f"UPDATE sync_intents SET done_at = ? WHERE {covered}", (now, project_id, revision))
            else:
                db.execute(
                    f"UPDATE sync_intents SET error = ?, error_final = MAX(error_final, ?) WHERE {covered}",
                    (detail or "failed", int(final), project_id, revision),
                )
            return _read(db, project_id)

    def observe_page(
        self, notion_page_id: str, *, actor: str, available: bool, shown: Shown | None = None
    ) -> Observed:
        page = _page(notion_page_id)
        now = self._clock()
        with self._transaction() as db:
            holder = _page_holder(db, page)
            if holder is None:
                if not available:
                    return Observed("ignored", None)
                _open_review(db, "unbound_page", page, None, {}, now)
                return Observed("unbound", None)
            project = _read(db, holder)
            if project is None:
                return Observed("ignored", None)
            binding = next(b for b in project.bindings if b.page_id == page)
            here = {"page_id": page}
            if not available:
                if binding.available:
                    db.execute("UPDATE bindings SET available = 0 WHERE system = ? AND page_id = ?", (NOTION, page))
                    _audit(db, holder, "page_unavailable", actor, now, {**here, "available": True}, {**here, "available": False})
                    _open_review(db, "page_unavailable", page, holder, {}, now)
                return Observed("unavailable", _must_read(db, holder))
            restored = not binding.available
            if restored:
                db.execute("UPDATE bindings SET available = 1 WHERE system = ? AND page_id = ?", (NOTION, page))
                _audit(db, holder, "page_restored", actor, now, {**here, "available": False}, {**here, "available": True})
                _close_review(db, "page_unavailable", page, now)
            if not binding.active:
                return Observed("restored" if restored else "legacy", _must_read(db, holder))
            expected = Shown(project.id, project.code, project.rotation.value)
            if shown == expected:
                _close_review(db, "projection_conflict", page, now)
                outcome = "restored" if restored else "in_sync"
            elif project.sync is SyncStatus.PENDING:
                outcome = "restored" if restored else "pending"  # a sync job is already on its way
            else:
                # Notion shows something else and nothing is on its way: put the registry's values back.
                _intend_sync(db, holder, project.revision, now)
                if restored:
                    outcome = "restored"
                else:
                    detail: dict[str, JsonValue] = {
                        "shown": None if shown is None else {"t6_id": shown.t6_id, "code": shown.code, "rotation": shown.rotation},
                        "expected": {"t6_id": expected.t6_id, "code": expected.code, "rotation": expected.rotation},
                    }
                    _open_review(db, "projection_conflict", page, holder, detail, now)
                    outcome = "conflict"
            return Observed(outcome, _must_read(db, holder))

    def review_items(self) -> tuple[ReviewItem, ...]:
        with self._transaction() as db:
            rows = db.execute(
                "SELECT id, kind, page_id, project_id, detail, created_at FROM review_items "
                "WHERE resolved_at IS NULL ORDER BY id"
            ).fetchall()
        return tuple(ReviewItem(row[0], row[1], row[2], row[3], json.loads(row[4]), row[5]) for row in rows)


def _require(actor: str, idempotency_key: str) -> None:
    if not actor.strip():
        raise RegistryError("actor must not be blank")
    if not idempotency_key.strip():
        raise RegistryError("idempotency_key must not be blank")


def _page(notion_page_id: str) -> str:
    page = normalize_page_id(notion_page_id)
    if not page:
        raise RegistryError("notion_page_id must not be blank")
    return page


def _seen(db: sqlite3.Connection, idempotency_key: str) -> tuple[str, str] | None:
    row: tuple[str, str] | None = db.execute(
        "SELECT fingerprint, project_id FROM requests WHERE idempotency_key = ?", (idempotency_key,)
    ).fetchone()
    return row


def _page_holder(db: sqlite3.Connection, page: str) -> str | None:
    row = db.execute("SELECT project_id FROM bindings WHERE system = ? AND page_id = ?", (NOTION, page)).fetchone()
    return None if row is None else str(row[0])


def _bind(db: sqlite3.Connection, page: str, project_id: str, now: float) -> None:
    db.execute(
        "INSERT INTO bindings (system, page_id, project_id, bound_at, active) VALUES (?, ?, ?, ?, 1)",
        (NOTION, page, project_id, now),
    )
    _close_review(db, "unbound_page", page, now)


def _open_review(
    db: sqlite3.Connection, kind: str, page: str, project_id: str | None, detail: _Values, now: float
) -> None:
    """Add a review item unless the same one is already open."""
    db.execute(
        "INSERT OR IGNORE INTO review_items (kind, page_id, project_id, detail, created_at) VALUES (?, ?, ?, ?, ?)",
        (kind, page, project_id, json.dumps(detail), now),
    )


def _close_review(db: sqlite3.Connection, kind: str, page: str, now: float) -> None:
    db.execute(
        "UPDATE review_items SET resolved_at = ? WHERE kind = ? AND page_id = ? AND resolved_at IS NULL",
        (now, kind, page),
    )


def _code_held(db: sqlite3.Connection, code: str, form: str, project_id: str) -> CodeHeld | None:
    """Who stops `project_id` from holding this code in rotation, if anyone."""
    rows = db.execute(
        "SELECT id, rotation FROM projects WHERE code_form = ? AND id != ? AND rotation IN (?, ?) ORDER BY rowid",
        (form, project_id, Rotation.IN.value, Rotation.UNCLASSIFIED.value),
    ).fetchall()
    if not rows:
        return None
    in_rotation = any(row[1] == Rotation.IN.value for row in rows)
    return CodeHeld(code, tuple(row[0] for row in rows), "in_rotation" if in_rotation else "unclassified")


def _audit(
    db: sqlite3.Connection, project_id: str, action: str, actor: str, at: float, before: _Values | None, after: _Values
) -> None:
    cursor = db.execute(
        "INSERT INTO audit (project_id, action, actor, at, before, after) VALUES (?, ?, ?, ?, ?, ?)",
        (project_id, action, actor, at, None if before is None else json.dumps(before), json.dumps(after)),
    )
    db.execute(
        "INSERT INTO activity (project_id, at, actor, action, summary, audit_id) VALUES (?, ?, ?, ?, ?, ?)",
        (project_id, at, actor, action, action.replace("_", " "), cursor.lastrowid),
    )


def _intend_sync(db: sqlite3.Connection, project_id: str, revision: int, now: float) -> None:
    db.execute(
        "INSERT INTO sync_intents (project_id, revision, created_at) VALUES (?, ?, ?)", (project_id, revision, now)
    )


def _must_read(db: sqlite3.Connection, project_id: str) -> RegisteredProject:
    project = _read(db, project_id)
    assert project is not None
    return project


def _read(db: sqlite3.Connection, project_id: str) -> RegisteredProject | None:
    row = db.execute(
        "SELECT id, revision, code, rotation, created_at FROM projects WHERE id = ?", (project_id,)
    ).fetchone()
    if row is None:
        return None
    bindings = db.execute(
        "SELECT system, page_id, bound_at, active, available FROM bindings "
        "WHERE project_id = ? ORDER BY bound_at, page_id",
        (project_id,),
    ).fetchall()
    undone = db.execute(
        "SELECT error, error_final FROM sync_intents WHERE project_id = ? AND done_at IS NULL ORDER BY id",
        (project_id,),
    ).fetchall()
    if not undone:
        sync = SyncStatus.SYNCED
    elif any(row[1] for row in undone):
        sync = SyncStatus.FAILED
    else:
        sync = SyncStatus.PENDING
    return RegisteredProject(
        id=row[0],
        revision=row[1],
        code=row[2],
        rotation=Rotation(row[3]),
        bindings=tuple(PageBinding(b[0], b[1], b[2], bool(b[3]), bool(b[4])) for b in bindings),
        sync=sync,
        created_at=row[4],
        sync_error=next((row[0] for row in reversed(undone) if row[0]), None),
    )
