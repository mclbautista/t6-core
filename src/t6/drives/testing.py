"""An in-memory DriveStore for tests. Nothing is kept after the process ends."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import replace
from datetime import date

from t6.drives import (
    Assignment,
    CustodyEvent,
    CustodyKind,
    Drive,
    DriveStatus,
    DuplicateSerial,
    Party,
    Ref,
    UnknownDrive,
    derive_status,
    normalize_serial,
)


class InMemoryDriveStore:
    """In-memory ID-addressed fake; optional same_project resolves Project aliases."""

    def __init__(self, same_project: Callable[[str | None, str | None], bool] | None = None) -> None:
        self._same_project = same_project or (lambda a, b: a == b)
        self._drives: dict[str, Drive] = {}
        self._events: dict[str, list[CustodyEvent]] = {}
        self._assignments: dict[str, list[Assignment]] = {}
        self._imports: dict[str, str] = {}
        self._shipments = 0
        self._shipped: list[tuple[str, CustodyEvent]] = []

    def add_drive(self, drive: Drive) -> Drive:
        if drive.id in self._drives:
            raise ValueError(f"Drive ID {drive.id!r} is already recorded")
        self._check_serial(drive)
        self._drives[drive.id] = drive
        self._events[drive.id] = []
        self._assignments[drive.id] = []
        return drive

    def _check_serial(self, drive: Drive) -> None:
        if drive.serial is not None:
            held = self.find_by_serial(drive.serial)
            if held is not None and held.id != drive.id:
                raise DuplicateSerial(drive.serial)

    def _drive(self, drive_id: str) -> Drive:
        drive = self.get_drive(drive_id)
        if drive is None:
            raise UnknownDrive(drive_id)
        return drive

    def set_serial(self, drive_id: str, serial: str | None) -> Drive:
        drive = replace(self._drive(drive_id), serial=serial)
        self._check_serial(drive)
        self._drives[drive_id] = drive
        return drive

    def set_suspected_serial(self, drive_id: str, serial: str | None) -> Drive:
        drive = replace(self._drive(drive_id), suspected_serial=serial)
        self._drives[drive_id] = drive
        return drive

    def promote_suspected_serial(self, drive_id: str) -> Drive:
        old = self._drive(drive_id)
        if old.suspected_serial is None:
            raise ValueError("Drive has no suspected serial to promote")
        drive = replace(old, serial=old.suspected_serial, suspected_serial=None)
        self._check_serial(drive)
        self._drives[drive_id] = drive
        return drive

    def set_review(self, drive_id: str, *, needs_review: bool, note: str | None = None) -> Drive:
        drive = replace(self._drive(drive_id), needs_review=needs_review, review_note=note)
        self._drives[drive_id] = drive
        return drive

    def get_drive(self, drive_id: str) -> Drive | None:
        return self._drives.get(drive_id)

    def find_by_serial(self, serial: str) -> Drive | None:
        normalized = normalize_serial(serial)
        return next((d for d in self._drives.values() if d.serial == normalized), None)

    def imported_drive(self, row_key: str) -> str | None:
        return self._imports.get(row_key)

    def has_suspected_serial(self, serial: str) -> bool:
        normalized = normalize_serial(serial)
        return any(d.suspected_serial == normalized for d in self._drives.values())

    def import_row(
        self, row_key: str, drive: Drive, assignment: Assignment, events: Sequence[CustodyEvent]
    ) -> None:
        if row_key in self._imports:
            return
        held = self.find_by_serial(drive.serial) if drive.serial else None
        if held is None:
            if drive.id in self._drives:
                raise ValueError(f"Drive ID {drive.id!r} is already recorded")
            self._check_serial(drive)
            held = drive
        else:
            notes = list(dict.fromkeys(filter(None, (held.review_note, drive.review_note))))
            held = replace(held, needs_review=held.needs_review or drive.needs_review,
                           review_note="; ".join(notes) or None)
        # Consume the complete source and stage every collection before publishing.
        incoming_events = list(events)
        assignments = list(self._assignments.get(held.id, ()))
        if assignment not in assignments:
            assignments.append(assignment)
        history = [*self._events.get(held.id, ()), *incoming_events]
        shipped = self._shipped + [
            (held.id, event) for event in incoming_events if event.shipment_id is not None
        ]
        self._drives[held.id] = held
        self._assignments[held.id] = assignments
        self._events[held.id] = history
        self._shipped = shipped
        self._imports[row_key] = held.id

    def append_event(self, drive_id: str, event: CustodyEvent) -> None:
        self._log(drive_id).append(event)
        if event.shipment_id is not None:
            self._shipped.append((drive_id, event))

    def release_shipment(
        self, drive_ids: Iterable[str], *, on: date, party: Party, project: Ref, logged_by: Ref
    ) -> str:
        keys = list(dict.fromkeys(drive_id for drive_id in drive_ids))
        if not keys:
            raise ValueError("a shipment needs at least one Drive")
        for key in keys:
            self._log(key)  # refuse the whole shipment if any Drive is unknown
        self._shipments += 1
        event = CustodyEvent(
            kind=CustodyKind.RELEASED,
            on=on,
            party=party,
            project=project,
            logged_by=logged_by,
            shipment_id=f"shipment-{self._shipments}",
        )
        for key in keys:
            self.append_event(key, event)
        assert event.shipment_id is not None
        return event.shipment_id

    def shipment_events(self, shipment_id: str) -> tuple[tuple[Drive, CustodyEvent], ...]:
        return tuple(
            (self._drives[key], event)
            for key, event in self._shipped
            if event.shipment_id == shipment_id
        )

    def list_events(self, drive_id: str) -> tuple[CustodyEvent, ...]:
        return tuple(self._log(drive_id))

    def status(self, drive_id: str) -> DriveStatus | None:
        return derive_status(self._log(drive_id))

    def add_assignment(self, drive_id: str, assignment: Assignment) -> None:
        self._assigned(drive_id).append(assignment)

    def open_assignments(self, project: Ref) -> list[tuple[Drive, Assignment]]:
        found = [
            (self._drives[key], assignment)
            for key, assignments in self._assignments.items()
            for assignment in assignments
            if assignment.is_open and self._matches(assignment.project, project)
        ]
        return sorted(
            found, key=lambda pair: (pair[1].start or date.min, pair[1].label or "", pair[0].key)
        )

    def _matches(self, held: Ref, asked: Ref) -> bool:
        if held.page_id is None or asked.page_id is None:
            return held == asked  # unresolved text is compared as text
        return self._same_project(held.page_id, asked.page_id)

    def assignment_history(self, drive_id: str) -> list[Assignment]:
        # sorted() is stable: assignments with the same start keep entry order
        return sorted(self._assigned(drive_id), key=lambda a: a.start or date.min)

    def _assigned(self, drive_id: str) -> list[Assignment]:
        key = drive_id
        if key not in self._assignments:
            raise UnknownDrive(key)
        return self._assignments[key]

    def _log(self, drive_id: str) -> list[CustodyEvent]:
        key = drive_id
        if key not in self._events:
            raise UnknownDrive(key)
        return self._events[key]
