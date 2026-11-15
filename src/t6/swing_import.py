"""Import of the Notion Swing Drives database into t6.drives.

Preview first, apply second, safe twice. A row is identified by its Notion page
ID and its serial only: a name or label never matches a Drive or a row. A row
that cannot be placed (no usable serial, an ambiguous serial, no or unresolved
Project) goes to the durable exception queue with its page ID, and nothing is
guessed. The Notion data is stale, so nothing is applied without the digest of
a preview of the same data.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import date
from typing import Any

from t6.drives import Assignment, Drive, DriveStore, Ref, Role, SourceRow, normalize_serial, open_duplicate
from t6.drives.importer import ReviewItem
from t6.registry.core import normalize_page_id

SWING_SOURCE = 'notion:swing_drives'
"""The `workbook` of an import receipt or exception from this database; the page ID is its `sheet`."""


def _row_key(page_id: str) -> str:
    return hashlib.sha256(json.dumps([SWING_SOURCE, page_id]).encode()).hexdigest()


def _page(value: str) -> str:
    return normalize_page_id(value)


def _entries(rows: Sequence[dict[str, Any]], store: DriveStore) -> list[dict[str, Any]]:
    rows = sorted(rows, key=lambda row: _page(row['page_id']))
    counts: dict[str, int] = {}
    for row in rows:
        serial = _serial(row)
        if serial:
            counts[serial] = counts.get(serial, 0) + 1
    entries = []
    for row in rows:
        page, serial = _page(row['page_id']), _serial(row)
        entry: dict[str, Any] = {'page_id': page, 'serial': serial, 'label': row.get('name'), 'action': 'exception', 'reason': None, 'project_id': None}

        def exception(reason: str) -> dict[str, Any]:
            entry['reason'] = reason
            return entry
        entries.append(entry)
        if not row.get('in_use'):
            entry.update(action='skip', reason='not in use')
        elif not (row.get('serial') or '').strip() or not serial:
            exception('no usable serial')
        elif normalize_serial(row['serial']) != row['serial'].strip().upper() and any(c in row['serial'] for c in '([)]'):
            exception('serial has notes; review it')
        elif counts[serial] > 1:
            exception('ambiguous: serial is on several rows')
        elif store.has_suspected_serial(serial):
            exception('ambiguous: serial is only a suspected serial of a Drive')
        elif len(row.get('projects') or []) != 1:
            exception('no Project' if not row.get('projects') else 'ambiguous: several Projects')
        else:
            project = store.resolve_project(Ref(page_id=_page(row['projects'][0])), 'unresolved')
            if project.page_id is None:
                exception('Project is not registered in t6')
                continue
            entry['project_id'] = project.page_id
            held = store.find_by_serial(serial)
            if store.imported_drive(_row_key(page)) is not None:
                entry['action'] = 'already'
            elif held is not None and open_duplicate(store.list_assignments(held.id), _assignment(row, project)) is not None:
                entry['action'] = 'already'
            else:
                entry['action'] = 'bind' if held is not None else 'create'
    return entries


def _serial(row: dict[str, Any]) -> str | None:
    return normalize_serial(row['serial']) or None if (row.get('serial') or '').strip() else None


def _assignment(row: dict[str, Any], project: Ref) -> Assignment:
    return Assignment(label=row.get('name'), project=project, role=Role.SWING,
                      start=date.fromisoformat(row['last_moved']) if row.get('last_moved') else None)


def import_swing_drives(rows: Sequence[dict[str, Any]], store: DriveStore, *, apply: bool = False, digest: str | None = None) -> dict[str, Any]:
    """Plan the import of Swing Drives rows; with `apply` and the digest of a preview, carry it out.

    The digest covers the Notion rows, not their outcome, so the same preview
    applies again and the second apply changes nothing. Notion data that
    changed since the preview raises ValueError.
    """
    fingerprint = hashlib.sha256(json.dumps(sorted(rows, key=lambda row: _page(row['page_id'])), sort_keys=True).encode()).hexdigest()
    if apply and digest != fingerprint:
        raise ValueError('Swing Drives changed since preview, or no preview was given; preview again')
    entries = _entries(rows, store)
    changed = 0
    if apply:
        by_page = {_page(row['page_id']): row for row in rows}
        queue: list[ReviewItem] = []
        known = len(store.exceptions())
        for entry in entries:
            row = by_page[entry['page_id']]
            if entry['action'] == 'exception':
                queue.append(ReviewItem(sheet=entry['page_id'], row=0, reason=entry['reason'], workbook=SWING_SOURCE,
                                        detail=json.dumps({'serial': row.get('serial'), 'name': row.get('name')}, sort_keys=True)))
            elif entry['action'] in ('bind', 'create'):
                project = Ref(page_id=entry['project_id'])
                store.import_row(_row_key(entry['page_id']), Drive(serial=entry['serial'], ownership_known=False), _assignment(row, project), [],
                                 source=SourceRow(SWING_SOURCE, entry['page_id'], 0))
                entry['action'] = 'bound' if entry['action'] == 'bind' else 'created'
                changed += 1
        store.record_exceptions(queue)
        changed += len(store.exceptions()) - known
    return {'digest': fingerprint, 'applied': apply, 'changed': changed, 'entries': entries}
