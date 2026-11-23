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
from pathlib import Path
import sqlite3
import time
from typing import Any, TypeAlias

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
    (8, False, """CREATE TABLE person_shifts (
        person_id TEXT PRIMARY KEY REFERENCES persons(id), pattern TEXT NOT NULL,
        start TEXT NOT NULL, end TEXT NOT NULL
    )"""),
    (9, False, """
        CREATE TABLE person_permissions (
            person_id TEXT NOT NULL REFERENCES persons(id), permission TEXT NOT NULL,
            PRIMARY KEY (person_id, permission)
        );
        CREATE TABLE permission_seed (id INTEGER PRIMARY KEY, unmatched TEXT NOT NULL)
    """),
    (10, False, """
        CREATE TABLE job_orders (
            id TEXT PRIMARY KEY, project_id TEXT REFERENCES projects(id),
            telegram_key TEXT UNIQUE, assignee_id TEXT REFERENCES persons(id), fields TEXT NOT NULL
        );
        CREATE TABLE job_order_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, job_order_id TEXT NOT NULL REFERENCES job_orders(id),
            text TEXT NOT NULL, actor TEXT NOT NULL, at REAL NOT NULL
        );
        CREATE TABLE jo_migration_receipts (digest TEXT PRIMARY KEY, snapshot TEXT NOT NULL, entries TEXT NOT NULL);
        CREATE TABLE jo_sync_intents (
            intent_id INTEGER PRIMARY KEY REFERENCES sync_intents(id), operation TEXT NOT NULL, payload TEXT NOT NULL, started INTEGER NOT NULL DEFAULT 0
        )
    """),
    (11, False, """
        CREATE TABLE jo_state (
            job_order_id TEXT PRIMARY KEY REFERENCES job_orders(id),
            status TEXT NOT NULL, blocked INTEGER NOT NULL DEFAULT 0,
            block_reason TEXT, delayed_until TEXT, delay_reason TEXT
        );
        CREATE TABLE jo_state_previews (nonce TEXT PRIMARY KEY, state TEXT NOT NULL)
    """),
    (12, False, """CREATE TABLE notion_last_seen (
        page_id TEXT NOT NULL, property TEXT NOT NULL, value TEXT NOT NULL,
        PRIMARY KEY (page_id, property)
    )"""),
    (13, False, """CREATE TABLE deliverables (id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), code TEXT NOT NULL, episode TEXT, due TEXT, air TEXT);
        CREATE TABLE deliverable_sync_intents (intent_id INTEGER PRIMARY KEY REFERENCES sync_intents(id), started INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE deliverable_migration_receipts (digest TEXT PRIMARY KEY, snapshot TEXT NOT NULL, entries TEXT NOT NULL)"""),
    (14, False, """CREATE TABLE approvals (
        id TEXT PRIMARY KEY, gate TEXT NOT NULL, target TEXT NOT NULL, requester TEXT NOT NULL,
        tapper_rule TEXT NOT NULL, state TEXT NOT NULL, decided_by TEXT, decided_at REAL, reason TEXT
    )"""),
    (15, False, """CREATE TABLE concerns (
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
        owner_id TEXT NOT NULL REFERENCES persons(id), waiting_on TEXT NOT NULL,
        anchor TEXT NOT NULL, as_of TEXT NOT NULL, opened_at REAL NOT NULL,
        evidence_at REAL NOT NULL, state TEXT NOT NULL, manual_override INTEGER NOT NULL DEFAULT 0,
        settles_id TEXT, revision INTEGER NOT NULL DEFAULT 1
    );
    CREATE TABLE concern_notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT, concern_id TEXT NOT NULL REFERENCES concerns(id),
        text TEXT NOT NULL, actor TEXT NOT NULL, at REAL NOT NULL
    )"""),
    (16, False, """CREATE TABLE project_archives (project_id TEXT PRIMARY KEY REFERENCES projects(id));
        CREATE TABLE project_person_roles (project_id TEXT NOT NULL REFERENCES projects(id), person_id TEXT NOT NULL REFERENCES persons(id), role TEXT NOT NULL, PRIMARY KEY (project_id, person_id, role))"""),
    (17, False, """CREATE TABLE milestones (
        id TEXT PRIMARY KEY, deliverable_id TEXT NOT NULL REFERENCES deliverables(id),
        milestone TEXT NOT NULL, occurred_on TEXT NOT NULL, actor TEXT NOT NULL, recorded_at REAL NOT NULL
    )"""),
    (18, False, """CREATE TABLE packages (
        id TEXT PRIMARY KEY, deliverable_id TEXT NOT NULL REFERENCES deliverables(id),
        kind TEXT NOT NULL, from_person TEXT NOT NULL, destination TEXT NOT NULL,
        recipient TEXT NOT NULL REFERENCES persons(id), note TEXT, spec TEXT NOT NULL,
        checklist TEXT NOT NULL, state TEXT NOT NULL, reason TEXT, revision INTEGER NOT NULL DEFAULT 1
    )"""),
    (19, False, """ALTER TABLE concern_notes ADD COLUMN message_id TEXT;
        CREATE TABLE concern_brief_messages (chat TEXT NOT NULL, message_id TEXT NOT NULL,
            concern_id TEXT NOT NULL REFERENCES concerns(id), PRIMARY KEY (chat, message_id, concern_id))"""),
    (20, False, """CREATE TABLE chat_links (
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
        chat_id INTEGER NOT NULL, topic_id INTEGER, state TEXT NOT NULL
    );
    CREATE UNIQUE INDEX chat_link_identity ON chat_links(project_id, chat_id, COALESCE(topic_id, 0));
    CREATE TABLE chat_link_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT, link_id TEXT NOT NULL REFERENCES chat_links(id),
        state TEXT NOT NULL, actor TEXT NOT NULL, at REAL NOT NULL
    );
    CREATE TABLE chat_link_previews (
        nonce TEXT PRIMARY KEY, approval_id TEXT NOT NULL REFERENCES approvals(id),
        snapshot TEXT NOT NULL, request TEXT NOT NULL
    )"""),
    (21, False, """CREATE TABLE deliverable_date_moves (
        deliverable_id TEXT NOT NULL REFERENCES deliverables(id), field TEXT NOT NULL,
        old_date TEXT NOT NULL, new_date TEXT NOT NULL, reason TEXT NOT NULL,
        actor TEXT NOT NULL, recorded_at REAL NOT NULL
    ); CREATE TABLE deliverable_misses (
        deliverable_id TEXT NOT NULL REFERENCES deliverables(id), reason TEXT NOT NULL,
        actor TEXT NOT NULL, recorded_at REAL NOT NULL
    )"""),
    (22, True, """ALTER TABLE packages RENAME TO packages_old;
    CREATE TABLE packages (
        id TEXT PRIMARY KEY, deliverable_id TEXT NOT NULL REFERENCES deliverables(id),
        kind TEXT NOT NULL, from_person TEXT NOT NULL, destination TEXT NOT NULL,
        recipient TEXT NOT NULL, note TEXT, spec TEXT NOT NULL,
        checklist TEXT NOT NULL, state TEXT NOT NULL, reason TEXT, revision INTEGER NOT NULL DEFAULT 1
    );
    INSERT INTO packages SELECT * FROM packages_old;
    DROP TABLE packages_old;
    CREATE TABLE delivery_carriers (
        package_id TEXT PRIMARY KEY REFERENCES packages(id), kind TEXT NOT NULL, ref TEXT
    )"""),
    (23, False, """CREATE TABLE linked_chat_cursor (id INTEGER PRIMARY KEY CHECK (id = 1), seq INTEGER NOT NULL);
    INSERT INTO linked_chat_cursor VALUES (1, 0);
    CREATE TABLE linked_chat_messages (
        chat_id INTEGER NOT NULL, topic_id INTEGER, message_id INTEGER NOT NULL,
        sender_id INTEGER, person_id TEXT REFERENCES persons(id), has_file INTEGER NOT NULL,
        has_link INTEGER NOT NULL, PRIMARY KEY (chat_id, message_id)
    );
    CREATE TABLE linked_chat_members (
        chat_id INTEGER NOT NULL, telegram_id INTEGER NOT NULL, person_id TEXT REFERENCES persons(id),
        present INTEGER NOT NULL, PRIMARY KEY (chat_id, telegram_id)
    )"""),
    (24, False, """CREATE TABLE versions (id TEXT PRIMARY KEY, deliverable_id TEXT NOT NULL REFERENCES deliverables(id), frameio_file_id TEXT NOT NULL UNIQUE, frameio_stack_id TEXT, source_timeline_id TEXT, note TEXT);
        CREATE TABLE version_locks (deliverable_id TEXT PRIMARY KEY REFERENCES deliverables(id), version_id TEXT NOT NULL REFERENCES versions(id))"""),
    (25, False, """CREATE TABLE concern_milestones (
        concern_id TEXT PRIMARY KEY REFERENCES concerns(id), deliverable_id TEXT NOT NULL REFERENCES deliverables(id), milestone TEXT NOT NULL
    );
    CREATE TABLE concern_date_rules (
        project_id TEXT NOT NULL REFERENCES projects(id), rule TEXT NOT NULL, concern_id TEXT NOT NULL REFERENCES concerns(id),
        PRIMARY KEY (project_id, rule)
    );
    CREATE TABLE concern_dates (
        concern_id TEXT PRIMARY KEY REFERENCES concerns(id), project_id TEXT NOT NULL REFERENCES projects(id), field TEXT NOT NULL
    )"""),
    (26, False, """CREATE TABLE delivery_share_views (event_id TEXT PRIMARY KEY)"""),
    (27, True, """CREATE TABLE deliverable_templates (deliverable_id TEXT PRIMARY KEY REFERENCES deliverables(id), template TEXT NOT NULL)"""),
    (28, False, """CREATE TABLE linked_chat_topics (chat_id INTEGER NOT NULL, topic_id INTEGER NOT NULL, name TEXT NOT NULL, PRIMARY KEY (chat_id, topic_id));
    CREATE TABLE linked_chat_notices (key TEXT PRIMARY KEY, text TEXT NOT NULL);
    CREATE TABLE linked_chat_sends (ref INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, topic_id INTEGER NOT NULL, links TEXT NOT NULL)"""),
    (29, False, """CREATE TABLE abang_requests (
        id TEXT PRIMARY KEY, person_id TEXT NOT NULL, source_chat INTEGER NOT NULL, source_topic INTEGER,
        destination_chat INTEGER NOT NULL, destination_topic INTEGER, note TEXT, actor TEXT NOT NULL,
        state TEXT NOT NULL, notify_ref INTEGER, message_id INTEGER, reply_id INTEGER, forward_ref INTEGER, edit_ref INTEGER
    );
    CREATE TABLE abang_history (id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL, state TEXT NOT NULL, actor TEXT NOT NULL, at REAL NOT NULL);
    CREATE TABLE abang_previews (nonce TEXT PRIMARY KEY, plan TEXT NOT NULL)"""),
    (30, False, """CREATE TABLE abang_candidates (
        id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES abang_requests(id),
        message_id INTEGER NOT NULL, state TEXT NOT NULL, notify_ref INTEGER,
        UNIQUE(request_id, message_id)
    );
    CREATE TABLE abang_drafts (
        id TEXT PRIMARY KEY, actor TEXT NOT NULL, person_id TEXT NOT NULL, source_chat INTEGER NOT NULL,
        source_topic INTEGER, note TEXT, state TEXT NOT NULL, notify_ref INTEGER
    );
    CREATE TABLE abang_draft_options (
        draft_id TEXT NOT NULL REFERENCES abang_drafts(id), n INTEGER NOT NULL, chat INTEGER NOT NULL,
        topic INTEGER, label TEXT NOT NULL, PRIMARY KEY (draft_id, n)
    );
    INSERT INTO internal_jobs (queue, kind, dedupe_key, payload, run_at, state, created_at)
        SELECT 'main', 'abang_nudge', r.id, json_object('request', r.id), h.at + 259200, 'queued', h.at
        FROM abang_requests r JOIN abang_history h ON h.request_id = r.id AND h.state = 'requested'
        WHERE r.state = 'requested'"""),
    (31, False, """CREATE TABLE concern_pm_lines (project_id TEXT NOT NULL REFERENCES projects(id), as_of TEXT NOT NULL, line TEXT NOT NULL, PRIMARY KEY (project_id, as_of, line));
    CREATE TABLE concern_pm_pending (concern_id TEXT PRIMARY KEY REFERENCES concerns(id), as_of TEXT NOT NULL, quote TEXT NOT NULL, due REAL NOT NULL, revision INTEGER NOT NULL);
    CREATE TABLE concern_pm_changes (id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL REFERENCES projects(id), concern_id TEXT NOT NULL REFERENCES concerns(id), action TEXT NOT NULL, quote TEXT NOT NULL, at REAL NOT NULL)"""),
    (32, False, """CREATE TABLE concern_pm_attempts (project_id TEXT NOT NULL REFERENCES projects(id), as_of TEXT NOT NULL, line TEXT NOT NULL, attempts INTEGER NOT NULL, PRIMARY KEY (project_id, as_of, line))"""),
    (33, False, """CREATE TABLE bays (id TEXT PRIMARY KEY);
    CREATE TABLE bay_migration_receipts (digest TEXT PRIMARY KEY, snapshot TEXT NOT NULL, entries TEXT NOT NULL);
    CREATE TABLE bookings (id TEXT PRIMARY KEY, bay_id TEXT NOT NULL REFERENCES bays(id), person_id TEXT NOT NULL REFERENCES persons(id), project_id TEXT NOT NULL REFERENCES projects(id), start TEXT NOT NULL, end TEXT NOT NULL, forced INTEGER NOT NULL, cancelled INTEGER NOT NULL);
    CREATE TABLE booking_previews (nonce TEXT PRIMARY KEY, plan TEXT NOT NULL)"""),
    (34, False, """CREATE TABLE media_locations (id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, project_id TEXT NOT NULL REFERENCES projects(id), kind TEXT NOT NULL, ref TEXT NOT NULL, role TEXT NOT NULL, active INTEGER NOT NULL, source_page TEXT UNIQUE);
    CREATE TABLE media_history (id INTEGER PRIMARY KEY, media_id TEXT NOT NULL REFERENCES media_locations(id), action TEXT NOT NULL, actor TEXT NOT NULL, at REAL NOT NULL);
    CREATE TABLE media_previews (nonce TEXT PRIMARY KEY, snapshot TEXT NOT NULL);
    CREATE TABLE media_migration_receipts (digest TEXT PRIMARY KEY, snapshot TEXT NOT NULL, entries TEXT NOT NULL)"""),
    (35, False, """CREATE TABLE drives (id TEXT PRIMARY KEY, serial TEXT UNIQUE, suspected_serial TEXT, data TEXT NOT NULL);
    CREATE TABLE drive_assignments (id INTEGER PRIMARY KEY, drive_id TEXT NOT NULL REFERENCES drives(id), data TEXT NOT NULL);
    CREATE TABLE drive_events (id INTEGER PRIMARY KEY, drive_id TEXT NOT NULL REFERENCES drives(id), shipment_id TEXT, data TEXT NOT NULL);
    CREATE TABLE drive_imports (row_key TEXT PRIMARY KEY, drive_id TEXT NOT NULL REFERENCES drives(id), workbook TEXT, sheet TEXT, row INTEGER);
    CREATE TABLE drive_exceptions (workbook TEXT NOT NULL, sheet TEXT NOT NULL, row INTEGER NOT NULL, reason TEXT NOT NULL, detail TEXT NOT NULL, PRIMARY KEY(workbook, sheet, row, reason, detail))"""),
    (36, False, """CREATE TABLE transcripts (id TEXT PRIMARY KEY, media_id TEXT NOT NULL UNIQUE REFERENCES media_locations(id), deliverable_id TEXT REFERENCES deliverables(id), project_id TEXT NOT NULL REFERENCES projects(id), settings TEXT NOT NULL, state TEXT NOT NULL, progress TEXT NOT NULL, summary TEXT NOT NULL);
    CREATE TABLE transcript_history (id INTEGER PRIMARY KEY, transcript_id TEXT NOT NULL REFERENCES transcripts(id), action TEXT NOT NULL, actor TEXT NOT NULL, at REAL NOT NULL);
    CREATE TABLE transcript_previews (nonce TEXT PRIMARY KEY, snapshot TEXT NOT NULL)"""),
    (37, False, """CREATE TABLE transcript_speaker_maps (history_id INTEGER PRIMARY KEY REFERENCES transcript_history(id), csv TEXT NOT NULL)"""),
    (38, False, """CREATE TABLE drive_previews (nonce TEXT PRIMARY KEY, snapshot TEXT NOT NULL);
    CREATE TABLE delivery_drives (package_id TEXT NOT NULL REFERENCES packages(id), drive_id TEXT NOT NULL REFERENCES drives(id), PRIMARY KEY (package_id, drive_id))"""),
    (39, False, """CREATE TABLE timelines (id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), resolve_media_id TEXT NOT NULL REFERENCES media_locations(id), label TEXT NOT NULL);
    CREATE TABLE timeline_previews (nonce TEXT PRIMARY KEY, snapshot TEXT NOT NULL)"""),
    (40, False, """CREATE TABLE calendar_channels (channel_id TEXT PRIMARY KEY, calendar TEXT NOT NULL, resource_id TEXT NOT NULL, expires_at REAL NOT NULL, token_hash TEXT NOT NULL, active INTEGER NOT NULL);
    CREATE TABLE calendar_sync_tokens (calendar TEXT PRIMARY KEY, sync_token TEXT NOT NULL);
    CREATE TABLE calendar_event_versions (calendar TEXT NOT NULL, event TEXT NOT NULL, version TEXT NOT NULL, outcome TEXT NOT NULL, PRIMARY KEY (calendar, event, version))"""),
    (41, False, """CREATE TABLE frameio_held_activity (id INTEGER PRIMARY KEY AUTOINCREMENT, frameio_project_id TEXT NOT NULL, dedupe_key TEXT NOT NULL UNIQUE, payload TEXT NOT NULL, held_at REAL NOT NULL, released_at REAL);
    CREATE INDEX frameio_held_activity_by_project ON frameio_held_activity (frameio_project_id, released_at)"""),
    (42, False, """CREATE TABLE drive_shipment_facts (shipment_id TEXT NOT NULL, drive_id TEXT NOT NULL REFERENCES drives(id), recipient TEXT, data TEXT NOT NULL, PRIMARY KEY (shipment_id, drive_id))"""),
    (43, False, """CREATE TABLE project_starts (idempotency_key TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), approval_id TEXT NOT NULL UNIQUE REFERENCES approvals(id),
    actor TEXT NOT NULL, fingerprint TEXT NOT NULL, inputs TEXT NOT NULL, plan TEXT NOT NULL, nonce TEXT NOT NULL, state TEXT NOT NULL, reason TEXT)"""),
    # The Concern carries the text of the brief. The table is rebuilt so that `owner_id` may be NULL; every old column is kept and the
    # new ones have defaults, so the previous release still reads and writes the file. `text_key` and its unique index follow in Python.
    (44, False, """CREATE TABLE concerns_new (
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
        owner_id TEXT REFERENCES persons(id), waiting_on TEXT NOT NULL,
        anchor TEXT NOT NULL, as_of TEXT NOT NULL, opened_at REAL NOT NULL,
        evidence_at REAL NOT NULL, state TEXT NOT NULL, manual_override INTEGER NOT NULL DEFAULT 0,
        settles_id TEXT, revision INTEGER NOT NULL DEFAULT 1,
        text TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT 'person', text_key TEXT, closed_at REAL
    );
    INSERT INTO concerns_new (id, project_id, owner_id, waiting_on, anchor, as_of, opened_at, evidence_at, state, manual_override, settles_id, revision, text, source, closed_at)
        SELECT id, project_id, owner_id, waiting_on, anchor, as_of, opened_at, evidence_at, state, manual_override, settles_id, revision, anchor,
            CASE WHEN id IN (SELECT concern_id FROM concern_date_rules) THEN 'date_rule'
                 WHEN id IN (SELECT concern_id FROM concern_pm_changes WHERE action = 'concern_auto_raise') THEN 'pm_notes_scan' ELSE 'person' END,
            CASE WHEN state = 'closed' THEN (SELECT MAX(at) FROM audit WHERE audit.project_id = concerns.id AND action IN ('concern_resolve', 'concern_auto_resolve')) END
        FROM concerns;
    DROP TABLE concerns;
    ALTER TABLE concerns_new RENAME TO concerns"""),
    (45, False, """CREATE TABLE concern_listings (run_date TEXT NOT NULL, concern_id TEXT NOT NULL REFERENCES concerns(id), PRIMARY KEY (run_date, concern_id));
    ALTER TABLE concern_brief_messages ADD COLUMN brief_date TEXT"""),
    # Every open's activity backfill looks up activity by audit_id; without this it scans activity once per audit row (#502).
    (46, False, "CREATE INDEX IF NOT EXISTS activity_by_audit ON activity (audit_id)"),
    # Drives gain a QR code (found by it, held by one Drive) and dated catalog snapshots kept as the changes since the one before (#549).
    (47, False, """ALTER TABLE drives ADD COLUMN qr_code TEXT;
    CREATE UNIQUE INDEX drives_by_qr_code ON drives (qr_code) WHERE qr_code IS NOT NULL;
    CREATE TABLE drive_catalog_snapshots (id TEXT PRIMARY KEY, drive_id TEXT NOT NULL REFERENCES drives(id), captured_on TEXT NOT NULL, parent_snapshot_id TEXT,
        root_path TEXT NOT NULL, entry_count INTEGER NOT NULL, total_bytes INTEGER NOT NULL, created_at REAL NOT NULL, UNIQUE (drive_id, captured_on));
    CREATE TABLE drive_catalog_changes (snapshot_id TEXT NOT NULL REFERENCES drive_catalog_snapshots(id), path TEXT NOT NULL, kind TEXT NOT NULL, change TEXT NOT NULL,
        size_bytes INTEGER, mtime TEXT, checksum TEXT, PRIMARY KEY (snapshot_id, path))"""),
    # Timelines gain a stamp state: the Resolve worker writes the stamp (#582). Rows linked before were stamped by hand.
    # `replaces` is the duplicated stamp a re-stamp overwrites; `resolve_uuid` the Resolve unique id the stamp was verified on.
    (48, False, """ALTER TABLE timelines ADD COLUMN stamp_state TEXT NOT NULL DEFAULT 'stamped' CHECK (stamp_state IN ('pending', 'stamped', 'failed'));
    ALTER TABLE timelines ADD COLUMN stamp_reason TEXT;
    ALTER TABLE timelines ADD COLUMN replaces TEXT;
    ALTER TABLE timelines ADD COLUMN resolve_uuid TEXT"""),
    # The latest bay scan report of each Bay: what its local Resolve disk library held when the Mac mini last scanned it (#376).
    (49, False, """CREATE TABLE bay_scan_reports (bay_id TEXT PRIMARY KEY REFERENCES bays(id), serial TEXT NOT NULL, scanned_at REAL NOT NULL,
        received_at REAL NOT NULL, report TEXT NOT NULL)"""),
    # Index Drive child tables so inventory, shipment and drive-get reads avoid full table scans (t6-drives#19).
    (50, False, """CREATE INDEX IF NOT EXISTS drive_events_by_drive ON drive_events (drive_id, id);
    CREATE INDEX IF NOT EXISTS drive_events_by_shipment ON drive_events (shipment_id, id) WHERE shipment_id IS NOT NULL;
    CREATE INDEX IF NOT EXISTS drive_assignments_by_drive ON drive_assignments (drive_id, id);
    CREATE INDEX IF NOT EXISTS drive_imports_by_drive ON drive_imports (drive_id);
    CREATE INDEX IF NOT EXISTS delivery_drives_by_drive ON delivery_drives (drive_id)"""),
    (51, False, """CREATE TABLE drive_serial_ids (serial TEXT PRIMARY KEY, drive_id TEXT NOT NULL);
    CREATE TABLE drive_reload_reports (id INTEGER PRIMARY KEY, at REAL NOT NULL, actor TEXT NOT NULL, data TEXT NOT NULL)"""),
    (52, False, """CREATE TABLE drive_project_mapping (name TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id))"""),
    (53, False, """CREATE TABLE drive_movements (id TEXT PRIMARY KEY, corrects TEXT REFERENCES drive_movements(id), data TEXT NOT NULL);
    CREATE UNIQUE INDEX movement_correction ON drive_movements(corrects) WHERE corrects IS NOT NULL;
    CREATE TABLE drive_movement_events (event_id INTEGER PRIMARY KEY REFERENCES drive_events(id), movement_id TEXT NOT NULL REFERENCES drive_movements(id))"""),
    (54, False, """CREATE TABLE drive_practice (id INTEGER PRIMARY KEY CHECK (id=1), practice INTEGER NOT NULL CHECK (practice IN (0, 1)));
    INSERT INTO drive_practice VALUES (1, 1)"""),
    # Snooze is gone (#636): every snoozed Concern is reopened with no override. Rows are reopened, never deleted.
    (55, False, "UPDATE concerns SET state = 'open', manual_override = 0, revision = revision + 1 WHERE state = 'snoozed'"),
    # PM Notes history: every dated line each autoscan read, kept for good; `removed` is the first run the line was absent (#638).
    # The autoscan keeps the time of its last completed run, so a missed 21:00 is caught up once at start.
    (56, False, """CREATE TABLE pm_notes_history (project_id TEXT NOT NULL REFERENCES projects(id), as_of TEXT NOT NULL, line TEXT NOT NULL,
        first_seen REAL NOT NULL, last_seen REAL NOT NULL, removed REAL, PRIMARY KEY (project_id, as_of, line));
    CREATE TABLE concern_autoscan_runs (id INTEGER PRIMARY KEY CHECK (id = 1), completed_at REAL NOT NULL)"""),
    # A pending close is decided at the next PSR (#639): who closed (`actor`, NULL for a Done line), its `source` (`close` or
    # `done_line`) and when. The week timer's rows move here; `concern_pm_pending` stays, empty, so the previous release still opens the file.
    (57, False, """CREATE TABLE concern_pending_closes (concern_id TEXT PRIMARY KEY REFERENCES concerns(id), actor TEXT, source TEXT NOT NULL,
        as_of TEXT, quote TEXT, created_at REAL NOT NULL);
    INSERT INTO concern_pending_closes SELECT concern_id, NULL, 'done_line', as_of, quote, due - 604800 FROM concern_pm_pending;
    DELETE FROM concern_pm_pending"""),
    # A pending close keeps the settlement its Closer gave; the Concern takes it only when the close is applied.
    (58, False, "ALTER TABLE concern_pending_closes ADD COLUMN settles_id TEXT"),
)


def concern_key(text: str) -> str:
    """The key a Concern's text is known by: whitespace collapsed, case folded."""
    return " ".join(text.split()).casefold()


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
            if version == 44:
                seen: set[tuple[str, str]] = set()
                for concern, project, anchor, state in db.execute("SELECT id, project_id, anchor, state FROM concerns ORDER BY opened_at, id").fetchall():
                    key = concern_key(anchor)
                    # Two Concerns that are not closed may already share a line: the older keeps the key, so every row survives the unique index.
                    if state != "closed":
                        if (project, key) in seen:
                            continue
                        seen.add((project, key))
                    db.execute("UPDATE concerns SET text_key = ? WHERE id = ?", (key, concern))
                db.execute("CREATE UNIQUE INDEX concerns_open_text ON concerns (project_id, text_key) WHERE state != 'closed' AND text_key IS NOT NULL")
            if version == 42:
                for operation in ("UPDATE", "DELETE"):
                    db.execute(f"CREATE TRIGGER drive_shipment_facts_no_{operation.lower()} BEFORE {operation} ON drive_shipment_facts "
                               "BEGIN SELECT RAISE(ABORT, 'drive shipment facts are append-only'); END")
            if version == 53:
                for operation in ("UPDATE", "DELETE"):
                    db.execute(f"CREATE TRIGGER drive_movements_no_{operation.lower()} BEFORE {operation} ON drive_movements "
                               "BEGIN SELECT RAISE(ABORT, 'movements are append-only'); END")
            if version == 35:
                for operation in ("UPDATE", "DELETE"):
                    db.execute(f"CREATE TRIGGER drive_events_no_{operation.lower()} BEFORE {operation} ON drive_events "
                               "BEGIN SELECT RAISE(ABORT, 'drive events are append-only'); END")
            if version == 37:
                for operation in ("UPDATE", "DELETE"):
                    db.execute(f"CREATE TRIGGER transcript_speaker_maps_no_{operation.lower()} BEFORE {operation} ON transcript_speaker_maps "
                               "BEGIN SELECT RAISE(ABORT, 'transcript speaker maps are append-only'); END")
            if version == 36:
                for operation in ("UPDATE", "DELETE"):
                    db.execute(f"CREATE TRIGGER transcript_history_no_{operation.lower()} BEFORE {operation} ON transcript_history "
                               "BEGIN SELECT RAISE(ABORT, 'transcript history is append-only'); END")
            if version == 34:
                for operation in ("UPDATE", "DELETE"):
                    db.execute(f"CREATE TRIGGER media_history_no_{operation.lower()} BEFORE {operation} ON media_history "
                               "BEGIN SELECT RAISE(ABORT, 'media history is append-only'); END")
            if version == 17:
                for operation in ("UPDATE", "DELETE"):
                    db.execute(f"CREATE TRIGGER milestones_no_{operation.lower()} BEFORE {operation} ON milestones "
                               "BEGIN SELECT RAISE(ABORT, 'milestones are append-only'); END")
            if version == 15:
                for operation in ("UPDATE", "DELETE"):
                    db.execute(f"CREATE TRIGGER concern_notes_no_{operation.lower()} BEFORE {operation} ON concern_notes "
                               f"BEGIN SELECT RAISE(ABORT, 'concern_notes is append-only'); END")
            if version == 10:
                for operation in ("UPDATE", "DELETE"):
                    db.execute(f"CREATE TRIGGER job_order_notes_no_{operation.lower()} BEFORE {operation} ON job_order_notes "
                               f"BEGIN SELECT RAISE(ABORT, 'job_order_notes is append-only'); END")
            db.execute("INSERT INTO schema_version VALUES (?, ?)", (version, int(forward_only)))

    jo_filter = ("NOT EXISTS (SELECT 1 FROM job_orders WHERE job_orders.id = audit.project_id) AND "
                 if db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'job_orders'").fetchone() else "")
    jo_filter += "NOT EXISTS (SELECT 1 FROM bookings WHERE bookings.id = audit.project_id) AND " if db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'bookings'").fetchone() else ""
    jo_filter += "NOT EXISTS (SELECT 1 FROM bays WHERE bays.id = audit.project_id) AND " if db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'bays'").fetchone() else ""
    jo_filter += "NOT EXISTS (SELECT 1 FROM deliverables WHERE deliverables.id = audit.project_id) AND " if db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'deliverables'").fetchone() else ""
    jo_filter += "NOT EXISTS (SELECT 1 FROM media_locations WHERE media_locations.id = audit.project_id) AND " if db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'media_locations'").fetchone() else ""
    jo_filter += "NOT EXISTS (SELECT 1 FROM transcripts WHERE transcripts.id = audit.project_id) AND " if db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'transcripts'").fetchone() else ""
    jo_filter += "NOT EXISTS (SELECT 1 FROM drives WHERE drives.id = audit.project_id) AND " if db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'drives'").fetchone() else ""
    db.execute("INSERT INTO activity (project_id, at, actor, action, summary, audit_id) "
               "SELECT project_id, at, actor, action, REPLACE(action, '_', ' '), id FROM audit "
               f"WHERE {jo_filter}NOT EXISTS (SELECT 1 FROM activity WHERE activity.audit_id = audit.id)")


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
        page_id = _page(page_id) if system in (NOTION, "notion_user") else page_id
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
        page_id = _page(page_id) if system in (NOTION, "notion_user") else page_id
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
        return self._intents("done_at IS NULL AND dispatched_at IS NULL AND id NOT IN (SELECT intent_id FROM status_sync_intents) AND id NOT IN (SELECT intent_id FROM jo_sync_intents) AND id NOT IN (SELECT intent_id FROM deliverable_sync_intents)")

    def _intents(self, where: str) -> tuple[SyncIntent, ...]:
        with self._transaction() as db:
            rows = db.execute(
                f"SELECT id, project_id, revision, created_at, "
                f"(SELECT held FROM status_sync_intents WHERE intent_id = sync_intents.id) "
                f"FROM sync_intents WHERE {where} ORDER BY id"
            ).fetchall()
        return tuple(SyncIntent(row[0], row[1], row[2], row[3], None if row[4] is None else bool(row[4])) for row in rows)

    def undispatched_deliverable_sync(self) -> tuple[SyncIntent, ...]:
        return self._intents("done_at IS NULL AND dispatched_at IS NULL AND id IN (SELECT intent_id FROM deliverable_sync_intents)")

    def deliverable_sync_state(self, intent_id: int) -> dict[str, Any] | None:
        with self._transaction() as db:
            row = db.execute("SELECT d.id, d.project_id, d.code, d.episode, d.due, d.air, s.done_at, i.started FROM sync_intents s JOIN deliverable_sync_intents i ON i.intent_id=s.id JOIN deliverables d ON d.id=s.project_id WHERE s.id=?", (intent_id,)).fetchone()
            if not row:
                return None
            def page(identifier: str) -> str | None:
                found = db.execute("SELECT page_id FROM bindings WHERE system='notion' AND project_id=? AND active=1", (identifier,)).fetchone()
                return found[0] if found else None
            from t6.deliverable import deliverable_status
            return {'id': row[0], 'project_id': row[1], 'code': row[2], 'episode': row[3], 'due': row[4], 'air': row[5], 'status': deliverable_status(db, row[0]), 'done': row[6] is not None, 'started': bool(row[7]), 'page_id': page(row[0]), 'project_page': page(row[1])}

    def begin_deliverable_create(self, intent_id: int) -> bool:
        with self._transaction() as db:
            return db.execute('UPDATE deliverable_sync_intents SET started=1 WHERE intent_id=? AND started=0', (intent_id,)).rowcount == 1

    def report_deliverable_sync(self, intent_id: int, *, page_id: str | None = None, error: str | None = None) -> bool:
        with self._transaction() as db:
            row = db.execute('SELECT s.project_id FROM sync_intents s JOIN deliverable_sync_intents i ON i.intent_id=s.id WHERE s.id=?', (intent_id,)).fetchone()
            if not row:
                return False
            if page_id:
                page_id = normalize_page_id(page_id)
                bound = db.execute("SELECT project_id FROM bindings WHERE system='notion' AND page_id=?", (page_id,)).fetchone()
                active = db.execute("SELECT page_id FROM bindings WHERE system='notion' AND project_id=? AND active=1", (row[0],)).fetchone()
                if (bound and bound[0] != row[0]) or (active and active[0] != page_id):
                    raise RegistryError('Notion page binding conflicts with another entity')
                if not bound:
                    db.execute("INSERT INTO bindings (system, page_id, project_id, bound_at, active) VALUES ('notion', ?, ?, ?, 1)", (page_id, row[0], self._clock()))
            if error is not None:
                db.execute('UPDATE sync_intents SET error=?, error_final=1 WHERE id=?', (error, intent_id))
            else:
                db.execute('UPDATE sync_intents SET done_at=?, error=NULL WHERE id=? AND done_at IS NULL', (self._clock(), intent_id))
            return True

    def undispatched_jo_sync(self) -> tuple[SyncIntent, ...]:
        return self._intents("done_at IS NULL AND dispatched_at IS NULL AND id IN (SELECT intent_id FROM jo_sync_intents)")

    def undispatched_status_sync(self) -> tuple[SyncIntent, ...]:
        return self._intents("done_at IS NULL AND dispatched_at IS NULL AND id IN (SELECT intent_id FROM status_sync_intents)")

    def status_sync_state(self, intent_id: int) -> dict[str, Any] | None:
        with self._transaction() as db:
            row = db.execute("SELECT s.project_id, s.done_at, i.held, h.held, b.page_id, EXISTS(SELECT 1 FROM project_archives a WHERE a.project_id=s.project_id) FROM sync_intents s JOIN status_sync_intents i ON i.intent_id=s.id LEFT JOIN project_hold_state h ON h.project_id=s.project_id LEFT JOIN bindings b ON b.project_id=s.project_id AND b.system='notion' AND b.active=1 WHERE s.id=?", (intent_id,)).fetchone()
            return None if row is None else {'project_id': row[0], 'done': row[1] is not None, 'held': row[2] if row[2] == 2 else bool(row[2]), 'current_held': 2 if row[5] else bool(row[3]), 'page_id': row[4]}

    def report_status_sync(self, intent_id: int) -> None:
        with self._transaction() as db:
            db.execute('UPDATE sync_intents SET done_at=? WHERE id IN (SELECT intent_id FROM status_sync_intents WHERE intent_id=?)', (self._clock(), intent_id))

    def jo_sync_state(self, intent_id: int) -> dict[str, Any] | None:
        with self._transaction() as db:
            row = db.execute("SELECT s.project_id, s.revision, s.done_at, j.operation, j.started, o.project_id, o.assignee_id, o.fields, j.payload FROM sync_intents s JOIN jo_sync_intents j ON j.intent_id = s.id JOIN job_orders o ON o.id = s.project_id WHERE s.id = ?", (intent_id,)).fetchone()
            if not row:
                return None
            def page(entity_id: str | None) -> str | None:
                found = db.execute("SELECT page_id FROM bindings WHERE system = 'notion' AND project_id = ? AND active = 1", (entity_id,)).fetchone()
                return str(found[0]) if found else None
            notes = [n[0] for n in db.execute('SELECT text FROM job_order_notes WHERE job_order_id = ? ORDER BY id', (row[0],)).fetchall()]
            current = db.execute('SELECT status FROM jo_state WHERE job_order_id = ?', (row[0],)).fetchone()
            current_status = current[0] if current else json.loads(row[7]).get('status') or 'Needs Intake Review'
            return {'current_status': current_status, 'jo_id': row[0], 'revision': row[1], 'done': row[2] is not None, 'operation': row[3],
                    'started': bool(row[4]), 'page_id': page(row[0]), 'project_id': row[5], 'project_page': page(row[5]),
                    'assignee_id': row[6], 'assignee_page': page(row[6]), 'fields': json.loads(row[7]), 'notes': notes, 'payload': json.loads(row[8])}

    def begin_jo_create(self, intent_id: int) -> bool:
        with self._transaction() as db:
            return db.execute("UPDATE jo_sync_intents SET started = 1 WHERE intent_id = ? AND operation = 'create' AND started = 0", (intent_id,)).rowcount == 1

    def report_jo_sync(self, intent_id: int, *, page_id: str | None = None, error: str | None = None) -> bool:
        with self._transaction() as db:
            row = db.execute('SELECT s.project_id, s.done_at FROM sync_intents s JOIN jo_sync_intents j ON j.intent_id = s.id WHERE s.id = ?', (intent_id,)).fetchone()
            if not row:
                return False
            if page_id:
                page_id = normalize_page_id(page_id)
                bound = db.execute("SELECT project_id FROM bindings WHERE system = 'notion' AND page_id = ?", (page_id,)).fetchone()
                active = db.execute("SELECT page_id FROM bindings WHERE system = 'notion' AND project_id = ? AND active = 1", (row[0],)).fetchone()
                if bound and bound[0] != row[0] or active and active[0] != page_id:
                    raise RegistryError('Notion page binding conflicts with another entity')
                if not bound:
                    db.execute("INSERT INTO bindings (system, page_id, project_id, bound_at, active) VALUES ('notion', ?, ?, ?, 1)", (page_id, row[0], self._clock()))
            if error is not None:
                db.execute('UPDATE sync_intents SET error = ?, error_final = 1 WHERE id = ?', (error, intent_id))
            else:
                db.execute('UPDATE sync_intents SET done_at = ?, error = NULL WHERE id = ? AND done_at IS NULL', (self._clock(), intent_id))
            return True

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
            covered = "project_id = ? AND done_at IS NULL AND revision <= ? AND id NOT IN (SELECT intent_id FROM status_sync_intents) AND id NOT IN (SELECT intent_id FROM jo_sync_intents) AND id NOT IN (SELECT intent_id FROM deliverable_sync_intents)"
            if ok:
                db.execute(f"UPDATE sync_intents SET done_at = ? WHERE {covered}", (now, project_id, revision))
            else:
                db.execute(
                    f"UPDATE sync_intents SET error = ?, error_final = MAX(error_final, ?) WHERE {covered}",
                    (detail or "failed", int(final), project_id, revision),
                )
            return _read(db, project_id)

    def observe_page(
        self, notion_page_id: str, *, actor: str, available: bool, shown: Shown | None = None,
        editor: str | None = None, changed: tuple[str, ...] | None = None, editor_id: str | None = None
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
            if shown is not None and editor is not None:
                person = db.execute("SELECT p.id FROM persons p JOIN bindings b ON b.project_id = p.id WHERE b.system = 'notion_user' AND b.page_id = ?", (normalize_page_id(editor_id),)).fetchone() if editor_id else None
                edit_actor = str(person[0]) if person else editor
                for name, value, wanted in zip(('t6_id', 'code', 'rotation'),
                                               (shown.t6_id, shown.code, shown.rotation),
                                               (expected.t6_id, expected.code, expected.rotation)):
                    if changed is not None and name not in changed:
                        continue
                    previous = db.execute('SELECT value FROM notion_last_seen WHERE page_id = ? AND property = ?', (page, name)).fetchone()
                    old = json.loads(previous[0]) if previous else wanted
                    if value != old and value != wanted:
                        label = {'t6_id': 'T6 ID', 'code': 'Project Code', 'rotation': 'Rotation'}[name]
                        cursor = db.execute('INSERT INTO audit (project_id, action, actor, at, before, after) VALUES (?, ?, ?, ?, ?, ?)',
                                            (holder, 'notion_identity_edit', edit_actor, now, json.dumps({name: old}), json.dumps({name: value})))
                        db.execute('INSERT INTO activity (project_id, at, actor, action, summary, audit_id) VALUES (?, ?, ?, ?, ?, ?)',
                                   (holder, now, edit_actor, 'notion_identity_edit', f'{label} edited in Notion by {editor}, put back', cursor.lastrowid))
                    db.execute('INSERT INTO notion_last_seen VALUES (?, ?, ?) ON CONFLICT(page_id, property) DO UPDATE SET value=excluded.value',
                               (page, name, json.dumps(value)))
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
    db: sqlite3.Connection, project_id: str, action: str, actor: str, at: float, before: _Values | None, after: _Values, *, summary: str | None = None
) -> None:
    cursor = db.execute(
        "INSERT INTO audit (project_id, action, actor, at, before, after) VALUES (?, ?, ?, ?, ?, ?)",
        (project_id, action, actor, at, None if before is None else json.dumps(before), json.dumps(after)),
    )
    db.execute(
        "INSERT INTO activity (project_id, at, actor, action, summary, audit_id) VALUES (?, ?, ?, ?, ?, ?)",
        (project_id, at, actor, action, summary or action.replace("_", " "), cursor.lastrowid),
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
