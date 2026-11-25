"""An in-memory DriveStore for tests. Nothing is kept after the process ends."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from t6.drives.importer import ReviewItem

from t6.ids import EntityKind, T6Id
from t6.registry.core import normalize_page_id

from t6.drives import (
    Assignment,
    CatalogContents,
    CatalogDiffEntry,
    CatalogEntry,
    CatalogSnapshot,
    CustodyEvent,
    CustodyKind,
    Drive,
    DriveStatus,
    DuplicateAssignment,
    DuplicateQrCode,
    DuplicateSerial,
    FormFactor,
    Party,
    Ref,
    SourceRow,
    UnknownDrive,
    derive_status,
    fold_catalog,
    new_catalog_snapshot,
    normalize_qr_code,
    normalize_serial,
    open_duplicate,
    parse_catalog_time,
)


class InMemoryDriveStore:
    """In-memory ID-addressed fake; optional same_project resolves Project aliases."""

    def __init__(self, same_project: Callable[[str | None, str | None], bool] | None = None, *,
                 project_bindings: dict[str, str] | None = None,
                 person_bindings: dict[str, str] | None = None,
                 deliveries: dict[str, tuple[str, str]] | None = None) -> None:
        self._same_project = same_project or (lambda a, b: a == b)
        self._project_bindings = dict(project_bindings or {})
        for identifier in self._project_bindings.values():
            T6Id.parse(identifier, EntityKind.PROJECT)
        self._person_bindings = dict(person_bindings or {})
        for identifier in self._person_bindings.values():
            T6Id.parse(identifier, EntityKind.PERSON)
        self._deliveries = dict(deliveries or {})
        self._delivery_drives: dict[str, set[str]] = {}
        self._drives: dict[str, Drive] = {}
        self._events: dict[str, list[CustodyEvent]] = {}
        self._assignments: dict[str, list[Assignment]] = {}
        self._imports: dict[str, str] = {}
        self._sources: dict[str, list[SourceRow]] = {}
        self._exceptions: list[ReviewItem] = []
        self._previews: dict[str, str] = {}
        self._catalog: dict[str, list[tuple[CatalogSnapshot, tuple[CatalogDiffEntry, ...]]]] = {}
        self._shipments = 0
        self._shipped: list[tuple[str, CustodyEvent]] = []
        self._shipment_facts: dict[str, tuple[str | None, dict[str, Drive]]] = {}

    def save_preview(self, nonce: str, snapshot: str) -> None:
        self._previews[nonce] = snapshot

    def preview(self, nonce: str) -> str | None:
        return self._previews.get(nonce)

    def add_drive(self, drive: Drive) -> Drive:
        if drive.id in self._drives:
            raise ValueError(f"Drive ID {drive.id!r} is already recorded")
        self._check_serial(drive)
        self._check_qr_code(drive)
        self._drives[drive.id] = drive
        self._events[drive.id] = []
        self._assignments[drive.id] = []
        self._catalog[drive.id] = []
        return drive

    def _check_qr_code(self, drive: Drive) -> None:
        if drive.qr_code is not None:
            held = self.find_by_qr_code(drive.qr_code)
            if held is not None and held.id != drive.id:
                raise DuplicateQrCode(drive.qr_code)

    def find_by_qr_code(self, qr_code: str) -> Drive | None:
        try:
            code = normalize_qr_code(qr_code)
        except ValueError:
            return None
        return next((drive for drive in self._drives.values() if code and drive.qr_code == code), None)

    def record_catalog(self, drive_id: str, captured_on: date | datetime, root_path: str, entries: Sequence[CatalogEntry], created_at: float
                       ) -> tuple[CatalogSnapshot, tuple[CatalogDiffEntry, ...]]:
        self._drive(drive_id)
        history = self._catalog[drive_id]
        snapshot, diff = new_catalog_snapshot(drive_id, history, captured_on, root_path, entries, created_at)
        history.append((snapshot, diff))
        return snapshot, diff

    def catalog_snapshots(self, drive_id: str) -> tuple[CatalogSnapshot, ...]:
        self._drive(drive_id)
        return tuple(snapshot for snapshot, _ in self._catalog[drive_id])

    def catalog_diff(self, drive_id: str, snapshot_id: str) -> tuple[CatalogDiffEntry, ...] | None:
        self._drive(drive_id)
        return next((diff for snapshot, diff in self._catalog[drive_id] if snapshot.id == snapshot_id), None)

    def contents_at(self, drive_id: str, on: date | datetime) -> CatalogContents | None:
        self._drive(drive_id)
        cutoff = parse_catalog_time(on, end_of_day=True)
        history = [item for item in self._catalog[drive_id] if item[0].captured_on <= cutoff]
        if not history:
            return None
        tree = fold_catalog(diff for _, diff in history)
        return CatalogContents(snapshot=history[-1][0], entries=tuple(tree[path] for path in sorted(tree)))

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

    def set_facts(self, drive_id: str, changes: Mapping[str, Any]) -> Drive:
        drive = replace(self._drive(drive_id), **changes)
        self._check_serial(drive)
        self._check_qr_code(drive)
        self._drives[drive_id] = drive
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

    def set_label(self, drive_id: str, label: str | None) -> Drive:
        drive = replace(self._drive(drive_id), label=label)
        self._drives[drive_id] = drive
        return drive

    def set_form_factor(self, drive_id: str, form_factor: FormFactor | None) -> Drive:
        drive = replace(self._drive(drive_id), form_factor=form_factor)
        self._drives[drive_id] = drive
        return drive

    def get_drive(self, drive_id: str) -> Drive | None:
        return self._drives.get(drive_id)

    def list_drives(self) -> tuple[Drive, ...]:
        return tuple(self._drives.values())

    def find_by_serial(self, serial: str) -> Drive | None:
        normalized = normalize_serial(serial)
        return next((d for d in self._drives.values() if d.serial == normalized), None)

    def imported_drive(self, row_key: str) -> str | None:
        return self._imports.get(row_key)

    def has_suspected_serial(self, serial: str) -> bool:
        normalized = normalize_serial(serial)
        return any(d.suspected_serial == normalized for d in self._drives.values())

    def import_row(
        self, row_key: str, drive: Drive, assignment: Assignment | None, events: Sequence[CustodyEvent],
        *, source: SourceRow | None = None, deduplicate_assignment: bool = True
    ) -> None:
        if row_key in self._imports:
            return
        if assignment is not None:
            self._check_project(assignment.project)
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
        if assignment is not None and (not deduplicate_assignment or assignment not in assignments):
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
        if source is not None:
            self._sources.setdefault(held.id, []).append(source)

    def append_event(self, drive_id: str, event: CustodyEvent) -> None:
        self._log(drive_id).append(event)
        if event.shipment_id is not None:
            self._shipped.append((drive_id, event))

    def release_shipment(
        self, drive_ids: Iterable[str], *, on: date, party: Party, project: Ref, logged_by: Ref,
        shipment_id: str | None = None, reason: str | None = None, recipient: str | None = None
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
            reason=reason,
            shipment_id=shipment_id or f"shipment-{self._shipments}",
        )
        assert event.shipment_id is not None
        facts = self._shipment_facts.setdefault(event.shipment_id, (recipient, {}))[1]
        for key in keys:
            self.append_event(key, event)
            facts.setdefault(key, self._drives[key])
        return event.shipment_id

    def shipment_facts(self, shipment_id: str) -> tuple[str | None, dict[str, Drive]] | None:
        found = self._shipment_facts.get(shipment_id)
        return (found[0], dict(found[1])) if found else None

    def shipment_events(self, shipment_id: str) -> tuple[tuple[Drive, CustodyEvent], ...]:
        return tuple(
            (self._drives[key], event)
            for key, event in self._shipped
            if event.shipment_id == shipment_id
        )

    def list_shipments(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(event.shipment_id for _, event in self._shipped if event.shipment_id is not None))

    def list_events(self, drive_id: str) -> tuple[CustodyEvent, ...]:
        return tuple(self._log(drive_id))

    def status(self, drive_id: str) -> DriveStatus | None:
        return derive_status(self._log(drive_id))

    def statuses(self, drive_ids: Sequence[str] | None = None) -> dict[str, DriveStatus | None]:
        keys = self._drives.keys() if drive_ids is None else dict.fromkeys(drive_ids)
        return {drive_id: derive_status(self._events[drive_id]) for drive_id in keys if drive_id in self._drives}

    def open_assignments_by_drive(
        self, drive_ids: Sequence[str] | None = None
    ) -> dict[str, tuple[tuple[int, Assignment], ...]]:
        keys = self._assignments.keys() if drive_ids is None else dict.fromkeys(drive_ids)
        result: dict[str, tuple[tuple[int, Assignment], ...]] = {}
        for drive_id in keys:
            items = self._assignments.get(drive_id)
            if not items:
                continue
            opened = tuple((number, a) for number, a in enumerate(items, 1) if a.is_open)
            if opened:
                result[drive_id] = opened
        return result

    @staticmethod
    def _check_project(project: Ref | None) -> None:
        if project is not None and project.page_id is not None:
            T6Id.parse(project.page_id, EntityKind.PROJECT)

    def add_assignment(self, drive_id: str, assignment: Assignment) -> None:
        assigned = self._assigned(drive_id)
        self._check_project(assignment.project)
        clash = open_duplicate(assigned, assignment)
        if clash is not None:
            raise DuplicateAssignment(clash.label)
        assigned.append(assignment)

    def check_project(self, project: Ref) -> None:
        self._check_project(project)

    def list_assignments(self, drive_id: str) -> tuple[Assignment, ...]:
        return tuple(self._assigned(drive_id))

    def end_assignment(self, drive_id: str, number: int, end: date) -> Assignment:
        assigned = self._assigned(drive_id)
        if not 1 <= number <= len(assigned):
            raise ValueError(f"the Drive has no Assignment number {number}")
        if not assigned[number - 1].is_open:
            raise ValueError(f"Assignment number {number} is already ended")
        ended = replace(assigned[number - 1], end=end)
        assigned[number - 1] = ended
        return ended

    def drop_return_date(self, drive_id: str, number: int) -> Assignment:
        assigned = self._assigned(drive_id)
        if not 1 <= number <= len(assigned):
            raise ValueError(f"the Drive has no Assignment number {number}")
        assigned[number - 1] = replace(assigned[number - 1], return_date=None)
        return assigned[number - 1]

    def overdue_returns(self, today: date) -> list[tuple[Drive, Assignment]]:
        found = [(self._drives[key], a) for key, items in self._assignments.items() for a in items
                 if a.is_open and a.return_date is not None and a.return_date < today]
        return sorted(found, key=lambda pair: (pair[1].return_date or date.min, pair[1].label or "", pair[0].key))

    def resolve_person(self, contact_page_id: str) -> str | None:
        return self._person_bindings.get(normalize_page_id(contact_page_id))

    def check_person(self, person_id: str) -> None:
        T6Id.parse(person_id, EntityKind.PERSON)

    def link_delivery(self, delivery_id: str, drive_ids: Iterable[str]) -> None:
        keys = list(dict.fromkeys(drive_ids))
        for key in keys:
            self._drive(key)
        self._delivery_drives.setdefault(delivery_id, set()).update(keys)

    def delivery_drives(self, delivery_id: str) -> tuple[str, ...]:
        return tuple(sorted(self._delivery_drives.get(delivery_id, ())))

    def drive_deliveries(self, drive_id: str) -> tuple[str, ...]:
        self._drive(drive_id)
        return tuple(sorted(delivery for delivery, drives in self._delivery_drives.items() if drive_id in drives))

    def delivery_carrier(self, delivery_id: str) -> tuple[str, str] | None:
        return self._deliveries.get(delivery_id)

    def open_assignments(self, project: Ref) -> list[tuple[Drive, Assignment]]:
        found = [
            (self._drives[key], assignment)
            for key, assignments in self._assignments.items()
            for assignment in assignments
            if assignment.is_open and assignment.project is not None and self._matches(assignment.project, project)
        ]
        return sorted(
            found, key=lambda pair: (pair[1].start or date.min, pair[1].label or "", pair[0].key)
        )

    def _matches(self, held: Ref, asked: Ref) -> bool:
        if held.page_id is None or asked.page_id is None:
            return held == asked  # unresolved text is compared as text
        held_id = self._project_bindings.get(held.page_id, held.page_id)
        asked_id = self._project_bindings.get(asked.page_id, asked.page_id)
        return held_id == asked_id or self._same_project(held_id, asked_id)

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

    def sources(self, drive_id: str) -> tuple[SourceRow, ...]:
        self._drive(drive_id)
        return tuple(self._sources.get(drive_id, ()))

    def resolve_project(self, project: Ref, fallback: str) -> Ref:
        if project.page_id is None:
            return project
        identifier = self._project_bindings.get(project.page_id)
        if identifier is None and project.page_id in self._project_bindings.values():
            identifier = project.page_id
        return Ref(page_id=identifier) if identifier else Ref(text=fallback)

    def record_exceptions(self, items: Sequence[ReviewItem]) -> None:
        for item in items:
            if item not in self._exceptions:
                self._exceptions.append(item)

    def exceptions(self) -> tuple[ReviewItem, ...]:
        return tuple(self._exceptions)
