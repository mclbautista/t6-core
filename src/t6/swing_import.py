"""Import of the Notion Swing Drives database into t6.drives.

Preview first, apply second, safe twice. One Notion row is one t6 Drive, and
the row's page ID is the only key: a name or label never matches a Drive or a
row, even a Drive with the same label. The Drive gets a provisional ID, no
serial, unknown ownership and its label from Name. A row in use with exactly
one Collaborator that is a registered Person (a Contacts page bound to a
Person) also gets an open swing Assignment held by that Person, from Last
Moved, with no Project. Any other holder case still creates the Drive, and the
holder question goes to the durable exception queue with the page ID; nothing
is guessed. The Notion data is stale, so nothing is applied without the digest
of a preview of the same data.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import date
from typing import Any

from t6.drives import Assignment, Drive, DriveStore, Role, SourceRow
from t6.drives.importer import ReviewItem
from t6.registry.core import normalize_page_id

SWING_SOURCE = 'notion:swing_drives'
"""The `workbook` of an import receipt or exception from this database; the page ID is its `sheet`."""


def _row_key(page_id: str) -> str:
    return hashlib.sha256(json.dumps([SWING_SOURCE, page_id]).encode()).hexdigest()


def _page(value: str) -> str:
    return normalize_page_id(value)


def _holder(row: dict[str, Any], store: DriveStore) -> tuple[str | None, str | None]:
    """The registered Person who holds the swing, or the question for the exception queue."""
    if not row.get('in_use'):
        return None, None
    collaborators = row.get('collaborators') or []
    if not collaborators:
        return None, 'In Use but no Collaborator: who has the Drive?'
    if len(collaborators) > 1:
        return None, 'In Use with several Collaborators: who has the Drive?'
    person = store.resolve_person(collaborators[0])
    if person is None:
        return None, 'In Use, but the Collaborator is not a registered Person: who has the Drive?'
    if not row.get('last_moved'):
        return None, 'In Use, but there is no Last Moved date to start the swing: when did it move?'
    return person, None


def _drive(row: dict[str, Any]) -> Drive:
    """The Drive of a row. Format, HD Type and Short Notes go to existing fields: format, description and remarks."""
    return Drive(label=row['name'], format=row.get('format'), description=', '.join(row.get('hd_types') or []) or None,
                 remarks=row.get('notes'), ownership_known=False)


def _entries(rows: Sequence[dict[str, Any]], store: DriveStore) -> list[dict[str, Any]]:
    entries = []
    for row in sorted(rows, key=lambda row: _page(row['page_id'])):
        page = _page(row['page_id'])
        entry: dict[str, Any] = {'page_id': page, 'label': row.get('name'), 'action': 'create', 'reason': None, 'holder': None}
        entries.append(entry)
        if not (row.get('name') or '').strip():
            entry.update(action='exception', reason='no Name to label the Drive')
            continue
        entry['holder'], entry['reason'] = _holder(row, store)
        if store.imported_drive(_row_key(page)) is not None:
            entry['action'] = 'already'
    return entries


def _assignment(row: dict[str, Any], holder: str) -> Assignment:
    return Assignment(label=row['name'], project=None, role=Role.SWING, start=date.fromisoformat(row['last_moved']), holder=holder)


def import_swing_drives(rows: Sequence[dict[str, Any]], store: DriveStore, *, apply: bool = False, digest: str | None = None) -> dict[str, Any]:
    """Plan the import of Swing Drives rows (one Drive per page ID); with `apply` and the digest of a preview, carry it out.

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
            if entry['reason']:
                queue.append(ReviewItem(sheet=entry['page_id'], row=0, reason=entry['reason'], workbook=SWING_SOURCE,
                                        detail=json.dumps({'name': row.get('name')}, sort_keys=True)))
            if entry['action'] == 'create':
                holder = entry['holder']
                store.import_row(_row_key(entry['page_id']), _drive(row), _assignment(row, holder) if holder else None, [],
                                 source=SourceRow(SWING_SOURCE, entry['page_id'], 0))
                entry['action'] = 'created'
                changed += 1
        store.record_exceptions(queue)
        changed += len(store.exceptions()) - known
    return {'digest': fingerprint, 'applied': apply, 'changed': changed, 'entries': entries}
