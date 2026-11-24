"""Preview and apply a complete sheet reload in the t6 database."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from datetime import date
from typing import Any

from t6 import verbflow
from t6.drives import CustodyEvent, CustodyKind, DriveStatus, Ref
from t6.drives.importer import import_per_project_workbook, reload_tracker_inventory
from t6.drives.photos import forget_all, photo_dir, remove_files
from t6.drives.mapping import load_mapping, project_key, project_names
from t6.drives.sheets import SheetSource
from t6.drives.sqlite import SqliteDriveStore
from t6.lookup import Index, Lookup
from t6.preview import PreviewTokens
from t6.registry.sqlite import SqliteProjectRegistry
from t6.verbs import Outcome, Refused


def last_reload_report(db: sqlite3.Connection) -> tuple[float, dict[str, Any]] | None:
    row = db.execute('SELECT at, data FROM drive_reload_reports ORDER BY id DESC LIMIT 1').fetchone()
    return (row[0], json.loads(row[1])) if row else None


def reload_drives(registry: SqliteProjectRegistry, tokens: PreviewTokens, source: SheetSource, *,
                  tracker: SheetSource | None = None, actor: str, idempotency_key: str | None, confirm: str | None) -> Outcome:
    from t6.drives.practice import practice_mode
    if not practice_mode(registry):
        return Refused(rule='cutover', reason='Drive reload is disabled: the system is live')
    digest = hashlib.sha256(json.dumps([source.tabs(), tracker.tabs() if tracker else None], default=str, sort_keys=True).encode()).hexdigest()
    request = verbflow.begin(tokens, 'drive reload', actor, {'workbook': source.workbook, 'tracker': tracker.workbook if tracker else None}, idempotency_key, confirm)
    if isinstance(request, Refused):
        return request
    with registry._transaction() as db:
        if not db.execute('SELECT practice FROM drive_practice WHERE id=1').fetchone()[0]:
            return Refused(rule='cutover', reason='Drive reload is disabled: the system is live')
        settled = verbflow.settle(db, tokens, request)
        if settled:
            return settled
        store = SqliteDriveStore(registry, within=db)
        if not request.nonce:
            preview, nonce = verbflow.issue(tokens, request, {'workbook': source.workbook, 'digest': digest})
            store.save_preview(nonce, digest)
            return preview
        if store.preview(request.nonce) != digest:
            return Refused(rule='sheet_changed', reason='Sheet changed since preview; preview again')
        db.execute('INSERT OR IGNORE INTO drive_serial_ids SELECT serial, id FROM drives WHERE serial IS NOT NULL')
        serial_ids = dict(db.execute('SELECT serial, drive_id FROM drive_serial_ids'))
        # Reload is the deliberate exception to append-only custody. Restore the guards
        # before loading; SQLite rolls back both DDL and data if any step fails.
        triggers = db.execute("SELECT name, sql FROM sqlite_master WHERE type='trigger' AND tbl_name IN ('drive_events', 'drive_shipment_facts', 'drive_movements') AND name LIKE '%_no_delete'").fetchall()
        for name, _ in triggers:
            db.execute(f'DROP TRIGGER {name}')
        stale_photos = forget_all(db)
        for table in ('drive_catalog_changes', 'drive_catalog_snapshots', 'delivery_drives', 'drive_shipment_facts',
                      'drive_status_resets', 'drive_captures', 'drive_movement_events', 'drive_movements', 'drive_events', 'drive_imports', 'drive_assignments', 'drive_exceptions', 'drive_previews', 'drives'):
            db.execute(f'DELETE FROM {table}')
        for _, sql in triggers:
            db.execute(sql)
        mapping = load_mapping(db)
        timeline: dict[str, list[CustodyEvent]] = defaultdict(list)
        result = import_per_project_workbook(source, store, lookup=Lookup(Index()), logged_by=Ref(text=actor), reload=True,
                                             serial_ids=serial_ids, project_mapping=mapping, timeline=timeline)
        from t6.drives.movements import load_tracker
        tracker_rows = reload_tracker_inventory(tracker, store, Ref(text=actor), mapping, serial_ids, timeline) if tracker else 0
        event_origins: dict[int, str] = {}
        movements = load_tracker(tracker, db, mapping, store, timeline, Ref(text=actor), event_origins) if tracker else []
        for drive_id, events in timeline.items():
            for event in sorted(events, key=lambda e: (e.kind is CustodyKind.RETIRED, e.on or date.min)):
                store.append_event(drive_id, event)
                if id(event) in event_origins:
                    db.execute('INSERT INTO drive_movement_events VALUES (last_insert_rowid(), ?)', (event_origins[id(event)],))
        matched = sum(m.drive is not None for m in movements)
        db.execute('INSERT OR IGNORE INTO drive_serial_ids SELECT serial, id FROM drives WHERE serial IS NOT NULL')
        drives = store.list_drives()
        statuses = [store.status(d.id) for d in drives]
        report: dict[str, Any] = {'movements_read': len(movements), 'movements_matched': matched, 'movements_unmatched': len(movements) - matched,
                                  'rows_read': result.imported + len(result.review) + tracker_rows + len(movements), 'drives': len(drives),
                                  'assignments': sum(len(store.list_assignments(d.id)) for d in drives),
                                  'released': statuses.count(DriveStatus.RELEASED), 'retired': statuses.count(DriveStatus.RETIRED),
                                  'drives_without_serial': sum(d.serial is None for d in drives),
                                  'without_serial': [{'id': d.id, 'label': d.label} for d in drives if d.serial is None],
                                  'exceptions': len(store.exceptions()),
                                  'unmapped_projects': sorted(name for name in project_names(source, tracker)
                                                              if project_key(name) not in mapping)}
        report.update(practice=True, nightly_enabled=True)
        db.execute('INSERT INTO drive_reload_reports (at, actor, data) VALUES (?, ?, ?)',
                   (registry._clock(), actor, json.dumps(report)))
        done = verbflow.finish(db, request, report)
    remove_files(stale_photos, photo_dir())  # after the commit: a failed reload keeps its photos
    return done
