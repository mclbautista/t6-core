"""Durable DriveStore on the t6 database."""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import asdict, replace
from datetime import date
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from t6.drives import (Assignment, CustodyEvent, CustodyKind, Drive, DriveStatus,
                       DuplicateSerial, FormFactor, Party, PartyKind, Ref, Role, SourceRow,
                       UnknownDrive, derive_status, normalize_serial)
from t6.ids import EntityKind, T6Id
from t6.registry.core import normalize_page_id
from t6.registry.sqlite import SqliteProjectRegistry

if TYPE_CHECKING:
    from t6.drives.importer import ReviewItem


def _dump(value: Any) -> str:
    def encode(item: Any) -> Any:
        if isinstance(item, date):
            return item.isoformat()
        return item.value
    return json.dumps(asdict(value), default=encode, sort_keys=True)


def _date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def _drive(data: str) -> Drive:
    value = json.loads(data)
    value['purchase_date'] = _date(value['purchase_date'])
    if value['form_factor']:
        value['form_factor'] = FormFactor(value['form_factor'])
    return Drive(**value)


def _assignment(data: str) -> Assignment:
    value = json.loads(data)
    value['project'] = Ref(**value['project'])
    value['start'], value['end'] = _date(value['start']), _date(value['end'])
    if value['role']:
        value['role'] = Role(value['role'])
    return Assignment(**value)


def _event(data: str) -> CustodyEvent:
    value = json.loads(data)
    value['kind'] = CustodyKind(value['kind'])
    value['on'] = _date(value['on'])
    value['project'], value['logged_by'] = Ref(**value['project']), Ref(**value['logged_by'])
    if value['party']:
        value['party'] = Party(PartyKind(value['party']['kind']), Ref(**value['party']['ref']))
    return CustodyEvent(**value)


class SqliteDriveStore:
    def __init__(self, registry: SqliteProjectRegistry) -> None:
        self.registry = registry

    @staticmethod
    def _get(db: sqlite3.Connection, drive_id: str) -> Drive:
        row = db.execute('SELECT data FROM drives WHERE id=?', (drive_id,)).fetchone()
        if not row:
            raise UnknownDrive(drive_id)
        return _drive(row[0])

    @staticmethod
    def _save(db: sqlite3.Connection, drive: Drive, *, new: bool = False) -> None:
        held = db.execute('SELECT id FROM drives WHERE serial=?', (drive.serial,)).fetchone() if drive.serial else None
        if held and held[0] != drive.id:
            raise DuplicateSerial(drive.serial or '')
        if new:
            db.execute('INSERT INTO drives VALUES (?, ?, ?, ?)', (drive.id, drive.serial, drive.suspected_serial, _dump(drive)))
        else:
            db.execute('UPDATE drives SET serial=?, suspected_serial=?, data=? WHERE id=?', (drive.serial, drive.suspected_serial, _dump(drive), drive.id))

    def add_drive(self, drive: Drive) -> Drive:
        with self.registry._transaction() as db:
            if db.execute('SELECT 1 FROM drives WHERE id=?', (drive.id,)).fetchone():
                raise ValueError('Drive ID is already recorded')
            self._save(db, drive, new=True)
        return drive

    def get_drive(self, drive_id: str) -> Drive | None:
        with self.registry._transaction() as db:
            row = db.execute('SELECT data FROM drives WHERE id=?', (drive_id,)).fetchone()
            return _drive(row[0]) if row else None

    def find_by_serial(self, serial: str) -> Drive | None:
        with self.registry._transaction() as db:
            row = db.execute('SELECT data FROM drives WHERE serial=?', (normalize_serial(serial),)).fetchone()
            return _drive(row[0]) if row else None

    def has_suspected_serial(self, serial: str) -> bool:
        with self.registry._transaction() as db:
            return bool(db.execute('SELECT 1 FROM drives WHERE suspected_serial=?', (normalize_serial(serial),)).fetchone())

    def _edit(self, drive_id: str, **values: Any) -> Drive:
        with self.registry._transaction() as db:
            drive = replace(self._get(db, drive_id), **values)
            self._save(db, drive)
            return drive

    def set_serial(self, drive_id: str, serial: str | None) -> Drive:
        return self._edit(drive_id, serial=serial)

    def set_suspected_serial(self, drive_id: str, serial: str | None) -> Drive:
        return self._edit(drive_id, suspected_serial=serial)

    def promote_suspected_serial(self, drive_id: str) -> Drive:
        with self.registry._transaction() as db:
            old = self._get(db, drive_id)
            if old.suspected_serial is None:
                raise ValueError('Drive has no suspected serial to promote')
            drive = replace(old, serial=old.suspected_serial, suspected_serial=None)
            self._save(db, drive)
            return drive

    def set_review(self, drive_id: str, *, needs_review: bool, note: str | None = None) -> Drive:
        return self._edit(drive_id, needs_review=needs_review, review_note=note)

    def imported_drive(self, row_key: str) -> str | None:
        with self.registry._transaction() as db:
            row = db.execute('SELECT drive_id FROM drive_imports WHERE row_key=?', (row_key,)).fetchone()
            return str(row[0]) if row else None

    @staticmethod
    def _append(db: sqlite3.Connection, drive_id: str, event: CustodyEvent) -> None:
        db.execute('INSERT INTO drive_events (drive_id, shipment_id, data) VALUES (?, ?, ?)', (drive_id, event.shipment_id, _dump(event)))

    @staticmethod
    def _check_project(db: sqlite3.Connection, project: Ref) -> None:
        if project.page_id is not None:
            T6Id.parse(project.page_id, EntityKind.PROJECT)
            if not db.execute('SELECT 1 FROM projects WHERE id=?', (project.page_id,)).fetchone():
                raise ValueError('assignment Project must be a registered t6 Project ID or unresolved text')

    def import_row(self, row_key: str, drive: Drive, assignment: Assignment, events: Sequence[CustodyEvent], *, source: SourceRow | None = None) -> None:
        with self.registry._transaction() as db:
            if db.execute('SELECT 1 FROM drive_imports WHERE row_key=?', (row_key,)).fetchone():
                return
            self._check_project(db, assignment.project)
            row = db.execute('SELECT data FROM drives WHERE serial=?', (drive.serial,)).fetchone() if drive.serial else None
            if row:
                held = _drive(row[0])
                notes = list(dict.fromkeys(filter(None, (held.review_note, drive.review_note))))
                held = replace(held, needs_review=held.needs_review or drive.needs_review, review_note='; '.join(notes) or None)
                self._save(db, held)
            else:
                held = drive
                if db.execute('SELECT 1 FROM drives WHERE id=?', (held.id,)).fetchone():
                    raise ValueError('Drive ID is already recorded')
                self._save(db, held, new=True)
            data = _dump(assignment)
            if not db.execute('SELECT 1 FROM drive_assignments WHERE drive_id=? AND data=?', (held.id, data)).fetchone():
                db.execute('INSERT INTO drive_assignments (drive_id, data) VALUES (?, ?)', (held.id, data))
            for event in events:
                self._append(db, held.id, event)
            db.execute('INSERT INTO drive_imports VALUES (?, ?, ?, ?, ?)', (row_key, held.id, source.workbook if source else None, source.sheet if source else None, source.row if source else None))

    def append_event(self, drive_id: str, event: CustodyEvent) -> None:
        with self.registry._transaction() as db:
            self._get(db, drive_id)
            self._append(db, drive_id, event)

    def list_events(self, drive_id: str) -> tuple[CustodyEvent, ...]:
        with self.registry._transaction() as db:
            self._get(db, drive_id)
            return tuple(_event(row[0]) for row in db.execute('SELECT data FROM drive_events WHERE drive_id=? ORDER BY id', (drive_id,)))

    def status(self, drive_id: str) -> DriveStatus | None:
        return derive_status(self.list_events(drive_id))

    def release_shipment(self, drive_ids: Iterable[str], *, on: date, party: Party, project: Ref, logged_by: Ref) -> str:
        keys = list(dict.fromkeys(drive_ids))
        if not keys:
            raise ValueError('a shipment needs at least one Drive')
        shipment = f'shipment-{uuid4().hex}'
        event = CustodyEvent(kind=CustodyKind.RELEASED, on=on, party=party, project=project, logged_by=logged_by, shipment_id=shipment)
        with self.registry._transaction() as db:
            for key in keys:
                self._get(db, key)
            for key in keys:
                self._append(db, key, event)
        return shipment

    def shipment_events(self, shipment_id: str) -> tuple[tuple[Drive, CustodyEvent], ...]:
        with self.registry._transaction() as db:
            return tuple((_drive(row[0]), _event(row[1])) for row in db.execute('SELECT drives.data, drive_events.data FROM drive_events JOIN drives ON drives.id=drive_events.drive_id WHERE shipment_id=? ORDER BY drive_events.id', (shipment_id,)))

    def add_assignment(self, drive_id: str, assignment: Assignment) -> None:
        with self.registry._transaction() as db:
            self._get(db, drive_id)
            self._check_project(db, assignment.project)
            db.execute('INSERT INTO drive_assignments (drive_id, data) VALUES (?, ?)', (drive_id, _dump(assignment)))

    def assignment_history(self, drive_id: str) -> list[Assignment]:
        with self.registry._transaction() as db:
            self._get(db, drive_id)
            rows = db.execute('SELECT data FROM drive_assignments WHERE drive_id=? ORDER BY id', (drive_id,)).fetchall()
            return sorted((_assignment(row[0]) for row in rows), key=lambda a: a.start or date.min)

    def open_assignments(self, project: Ref) -> list[tuple[Drive, Assignment]]:
        with self.registry._transaction() as db:
            if project.page_id is not None:
                binding = db.execute("SELECT project_id FROM bindings WHERE system='notion' AND page_id=?", (normalize_page_id(project.page_id),)).fetchone()
                if binding:
                    project = Ref(page_id=binding[0])
            rows = db.execute('SELECT drives.data, drive_assignments.data FROM drive_assignments JOIN drives ON drives.id=drive_assignments.drive_id ORDER BY drive_assignments.id').fetchall()
            pairs = [(_drive(d), _assignment(a)) for d, a in rows]
            return sorted((pair for pair in pairs if pair[1].is_open and pair[1].project == project), key=lambda p: (p[1].start or date.min, p[1].label or '', p[0].id))

    def record_exceptions(self, items: Sequence[ReviewItem]) -> None:
        with self.registry._transaction() as db:
            db.executemany('INSERT OR IGNORE INTO drive_exceptions VALUES (?, ?, ?, ?, ?)',
                           ((item.workbook, item.sheet, item.row, item.reason, item.detail) for item in items))

    def exceptions(self) -> tuple[ReviewItem, ...]:
        from t6.drives.importer import ReviewItem
        with self.registry._transaction() as db:
            return tuple(ReviewItem(sheet=row[1], row=row[2], reason=row[3], detail=row[4], workbook=row[0])
                         for row in db.execute('SELECT workbook, sheet, row, reason, detail FROM drive_exceptions ORDER BY workbook, sheet, row, reason, detail'))

    def resolve_project(self, project: Ref, fallback: str) -> Ref:
        if project.page_id is None:
            return project
        found = self.registry.get(project.page_id) or self.registry.get_by_page(project.page_id)
        return Ref(page_id=found.id) if found else Ref(text=fallback)

    def sources(self, drive_id: str) -> tuple[SourceRow, ...]:
        with self.registry._transaction() as db:
            self._get(db, drive_id)
            return tuple(SourceRow(*row) for row in db.execute(
                'SELECT workbook, sheet, row FROM drive_imports WHERE drive_id=? AND workbook IS NOT NULL ORDER BY rowid', (drive_id,)))
