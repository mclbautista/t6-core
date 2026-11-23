"""Preview and apply a complete sheet reload in the t6 database."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from t6 import verbflow
from t6.drives import DriveStatus, Ref
from t6.drives.importer import import_per_project_workbook
from t6.drives.mapping import load_mapping, project_key, project_names
from t6.drives.sheets import SheetSource
from t6.drives.sqlite import SqliteDriveStore
from t6.lookup import Index, Lookup
from t6.preview import PreviewTokens
from t6.registry.sqlite import SqliteProjectRegistry
from t6.verbs import Outcome, Refused


def reload_drives(registry: SqliteProjectRegistry, tokens: PreviewTokens, source: SheetSource, *,
                  actor: str, idempotency_key: str | None, confirm: str | None) -> Outcome:
    digest = hashlib.sha256(json.dumps(source.tabs(), default=str, sort_keys=True).encode()).hexdigest()
    request = verbflow.begin(tokens, 'drive reload', actor, {'workbook': source.workbook}, idempotency_key, confirm)
    if isinstance(request, Refused):
        return request
    with registry._transaction() as db:
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
        triggers = db.execute("SELECT name, sql FROM sqlite_master WHERE type='trigger' AND tbl_name IN ('drive_events', 'drive_shipment_facts') AND name LIKE '%_no_delete'").fetchall()
        for name, _ in triggers:
            db.execute(f'DROP TRIGGER {name}')
        for table in ('drive_catalog_changes', 'drive_catalog_snapshots', 'delivery_drives', 'drive_shipment_facts',
                      'drive_events', 'drive_imports', 'drive_assignments', 'drive_exceptions', 'drive_previews', 'drives'):
            db.execute(f'DELETE FROM {table}')
        for _, sql in triggers:
            db.execute(sql)
        mapping = load_mapping(db)
        result = import_per_project_workbook(source, store, lookup=Lookup(Index()), logged_by=Ref(text=actor), reload=True,
                                             serial_ids=serial_ids, project_mapping=mapping)
        db.execute('INSERT OR IGNORE INTO drive_serial_ids SELECT serial, id FROM drives WHERE serial IS NOT NULL')
        drives = store.list_drives()
        statuses = [store.status(d.id) for d in drives]
        report: dict[str, Any] = {'rows_read': result.imported + len(result.review), 'drives': len(drives),
                                  'assignments': sum(len(store.list_assignments(d.id)) for d in drives),
                                  'released': statuses.count(DriveStatus.RELEASED), 'retired': statuses.count(DriveStatus.RETIRED),
                                  'drives_without_serial': sum(d.serial is None for d in drives),
                                  'without_serial': [{'id': d.id, 'label': d.label} for d in drives if d.serial is None],
                                  'exceptions': len(store.exceptions()),
                                  'unmapped_projects': sorted(name for name in project_names(source)
                                                              if project_key(name) not in mapping)}
        db.execute('INSERT INTO drive_reload_reports (at, actor, data) VALUES (?, ?, ?)',
                   (registry._clock(), actor, json.dumps(report)))
        return verbflow.finish(db, request, report)
