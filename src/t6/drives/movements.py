"""Append-only Movement records and tracker loading."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from t6.drives import CustodyEvent, CustodyKind, Party, PartyKind, Ref, SourceRow
from t6.drives.sqlite import SqliteDriveStore
from t6.drives.importer import _cell_date, _read_log_sheet
from t6.drives.mapping import project_ref
from t6.drives.sheets import SheetSource
from t6.preview import PreviewTokens
from t6.registry.sqlite import SqliteProjectRegistry
from t6.verbs import Outcome


@dataclass(frozen=True, slots=True)
class Movement:
    id: str
    drive: str | None
    label: str | None
    on: date | None
    direction: Literal['IN', 'OUT']
    person: str | None
    recorder: str | None
    place: Literal['TABLE', 'CABINET', 'SHELF', 'BOX'] | None
    project: Ref | None
    origin: SourceRow | None = None
    capture: str | None = None
    corrects: str | None = None
    description: str | None = None
    remarks: str | None = None
    recorder_telegram_id: int | None = None
    shipment_id: str | None = None
    posted_at: str | None = None
    coordinators: tuple[str, ...] = ()  # the Project's T6 Post Coordinator Person IDs when the Movement was made; never refreshed


def append(db: sqlite3.Connection, movement: Movement) -> None:
    db.execute('INSERT INTO drive_movements (id, corrects, data) VALUES (?, ?, ?)',
               (movement.id, movement.corrects, json.dumps(asdict(movement), default=str)))


def list_movements(db: sqlite3.Connection, *, project: str | None = None, drive: str | None = None,
                   person: str | None = None, q: str | None = None, start: date | None = None,
                   end: date | None = None, needs_project: bool = False) -> list[dict[str, Any]]:
    rows = [json.loads(row[0]) for row in db.execute('SELECT data FROM drive_movements ORDER BY rowid')]
    return [row for row in rows if (not project or row['project'] and project in row['project'].values())
            and (not needs_project or not row['project'] and row['drive'] and not db.execute(
                'SELECT 1 FROM drive_movements WHERE corrects=?', (row['id'],)).fetchone() and not db.execute(
                "SELECT 1 FROM drive_captures WHERE id=? AND json_extract(data, '$.state')='undone'", (row['capture'],)).fetchone())
            and (not drive or row['drive'] == drive)
            and (not person or person.casefold() in (row['person'] or '').casefold())
            and (not q or any(q.casefold() in (row[key] or '').casefold()
                              for key in ('label', 'person', 'description', 'remarks')))
            and (not start or row['on'] is not None and row['on'] >= start.isoformat())
            and (not end or row['on'] is not None and row['on'] <= end.isoformat())]


def load_tracker(source: SheetSource, db: sqlite3.Connection, mapping: dict[str, str],
                 store: SqliteDriveStore, timeline: dict[str, list[CustodyEvent]], logged_by: Ref,
                 event_origins: dict[int, str]) -> list[Movement]:
    movements = []
    labels: dict[tuple[Ref | None, str], set[str]] = {}
    for drive in store.list_drives():
        for assignment in store.list_assignments(drive.id):
            if assignment.project and assignment.label:
                labels.setdefault((assignment.project, normalize_label(assignment.label)), set()).add(drive.id)
    _, rows, _ = _read_log_sheet(source)
    for row in rows:
        direction = (row.text('IN/OUT') or '').upper().replace('T6 IN', 'IN')
        problems: list[str] = []
        on = _cell_date(row.cells.get('DATE'), problems)
        if direction not in ('IN', 'OUT') or problems:
            store.record_exceptions([row.review('unreadable movement', row.evidence())])
            continue
        movement = Movement(id='t6m_' + uuid4().hex, drive=None, label=row.text('HARD DRIVE LABEL'),
                            on=on, direction='IN' if direction == 'IN' else 'OUT',
                            person=row.text('RECEIVED BY'), recorder=None, place=None,
                            project=project_ref(row.text('PROJECT NAME') or row.sheet, mapping),
                            origin=SourceRow(source.workbook, row.sheet, row.number),
                            description=row.text('HD DESCRIPTION'), remarks=row.text('REMARKS'))
        candidates = labels.get((movement.project, normalize_label(movement.label or '')), set())
        if len(candidates) == 1:
            movement = replace(movement, drive=next(iter(candidates)))
            assert movement.drive is not None
            event = custody_event(movement, logged_by)
            timeline.setdefault(movement.drive, []).append(event)
            event_origins[id(event)] = movement.id
        append(db, movement)
        movements.append(movement)
    return movements


def normalize_label(label: str) -> str:
    return ' '.join(label.split()).casefold()


def custody_event(movement: Movement, logged_by: Ref) -> CustodyEvent:
    person = movement.person
    party = Party(PartyKind.CONTACT, Ref(text=person)) if person and person.strip() != '-' else None
    return CustodyEvent(kind=CustodyKind.RELEASED if movement.direction == 'OUT' else CustodyKind.RECEIVED,
                        on=movement.on, party=party if movement.direction == 'OUT' else None,
                        project=movement.project, logged_by=logged_by, shipment_id=movement.shipment_id if movement.direction == 'OUT' else None)


def _movement(row: dict[str, Any]) -> Movement:
    values = dict(row)
    values['on'] = date.fromisoformat(row['on']) if row['on'] else None
    values['project'] = Ref(**row['project']) if row['project'] else None
    values['origin'] = SourceRow(**row['origin']) if row['origin'] else None
    values['coordinators'] = tuple(row.get('coordinators', ()))
    return Movement(**values)


def correct_movement(registry: SqliteProjectRegistry, tokens: PreviewTokens, movement_id: str, changes: dict[str, Any], *,
                     actor: str, idempotency_key: str | None, confirm: str | None) -> Outcome:
    from t6 import verbflow
    from t6.verbs import Refused
    request = verbflow.begin(tokens, 'movement correct', actor, {'movement': movement_id, **{k: v.isoformat() if isinstance(v, date) else v for k, v in changes.items()}},
                             idempotency_key, confirm)
    if isinstance(request, Refused):
        return request
    with registry._transaction() as db:
        settled = verbflow.settle(db, tokens, request, replay_spent=True)
        if settled:
            return settled
        row = db.execute('SELECT data FROM drive_movements WHERE id=?', (movement_id,)).fetchone()
        if row is None:
            return Refused(rule='movement', reason='Movement not found')
        if db.execute('SELECT 1 FROM drive_movements WHERE corrects=?', (movement_id,)).fetchone():
            return Refused(rule='movement', reason='Correct the latest entry in this correction chain')
        store = SqliteDriveStore(registry, within=db)
        if changes.get('drive') and store.get_drive(changes['drive']) is None:
            return Refused(rule='drive', reason='Drive not found')
        original = _movement(json.loads(row[0]))
        if changes.get('project'):
            project = changes['project'].strip()
            registered = db.execute('SELECT 1 FROM projects WHERE id=?', (project,)).fetchone()
            changes = {**changes, 'project': Ref(page_id=project) if registered else Ref(text=project)}
        corrected = replace(original, **changes, id='t6m_' + hashlib.sha256(request.print_.encode()).hexdigest()[:32], corrects=original.id, recorder=actor)
        result = {'movement': json.loads(json.dumps(asdict(corrected), default=str))}
        if not request.nonce:
            return verbflow.issue(tokens, request, result)[0]
        append(db, corrected)
        if corrected.drive:
            _append_event(db, store, corrected.drive, custody_event(corrected, Ref(page_id=actor)), corrected.id)
            if corrected.direction == 'OUT' or 'place' in changes or corrected.place is not None:
                store.set_facts(corrected.drive, {'office_location': corrected.place if corrected.direction == 'IN' else None})
        # Corrections replace facts in the effective timeline, but never erase history.
        # Append the resulting custody fact when the corrected event is not the last
        # dated event, or when moving it leaves the original Drive with earlier custody.
        for drive_id in dict.fromkeys(d for d in (original.drive, corrected.drive) if d):
            latest = latest_custody(db, drive_id)
            if latest and (drive_id != corrected.drive or latest != custody_event(corrected, Ref(page_id=actor))):
                _append_event(db, store, drive_id, latest, corrected.id)
        return verbflow.finish(db, request, result)


def _append_event(db: sqlite3.Connection, store: SqliteDriveStore, drive_id: str,
                  event: CustodyEvent, movement_id: str) -> None:
    store.append_event(drive_id, event)
    db.execute('INSERT INTO drive_movement_events VALUES (last_insert_rowid(), ?)', (movement_id,))


def active_movements(db: sqlite3.Connection) -> list[Movement]:
    undone = {row[0] for row in db.execute("SELECT id FROM drive_captures WHERE json_extract(data, '$.state')='undone'")}
    corrected = {row[0] for row in db.execute('SELECT corrects FROM drive_movements WHERE corrects IS NOT NULL')}
    return [_movement(m) for m in list_movements(db) if m['id'] not in corrected and m['capture'] not in undone]


def latest_movement_by_project(db: sqlite3.Connection) -> dict[Ref, str]:
    """The newest movement time of each Project (a capture's post time, else the dated day), as the movement states it."""
    best: dict[Ref, tuple[float, str]] = {}
    for m in active_movements(db):
        text = m.posted_at or m.on
        if m.project is None or not text:
            continue
        stamp = text if isinstance(text, str) else text.isoformat()
        try:
            when = datetime.fromisoformat(stamp)
        except ValueError:
            continue
        key = (when if when.tzinfo else when.replace(tzinfo=timezone.utc)).timestamp()
        if m.project not in best or key > best[m.project][0]:
            best[m.project] = (key, stamp)
    return {project: stamp for project, (_, stamp) in best.items()}


def latest_custody(db: sqlite3.Connection, drive_id: str, *, exclude_capture: str | None = None) -> CustodyEvent | None:
    from t6.drives.sqlite import _event
    candidates = [(_event(row[0]), 0.0) for row in db.execute(
        'SELECT data FROM drive_effective_events WHERE drive_id=? AND id NOT IN (SELECT event_id FROM drive_movement_events)',
        (drive_id,))]
    candidates.extend((custody_event(m, Ref(page_id=m.recorder) if m.recorder else Ref(text=f'telegram:{m.recorder_telegram_id}' if m.recorder_telegram_id is not None else 'movement log')),
                       datetime.fromisoformat(m.posted_at).timestamp() if m.posted_at else 0.0)
                      for m in active_movements(db) if m.drive == drive_id and (exclude_capture is None or m.capture != exclude_capture))
    return max(candidates, key=lambda item: (item[0].kind is CustodyKind.RETIRED, item[0].on or date.min, item[1]))[0] if candidates else None
