"""An in-memory DriveStore for tests. Nothing is kept after the process ends."""

from __future__ import annotations

from collections.abc import Callable, Iterable
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
    """A DriveStore that keeps Drives in a dict keyed by normalized serial.

    `same_project` says whether two Project page IDs name one Project. The
    default is equality; pass `t6.registry.ProjectRefs.same` to find a Project's
    Assignments under its T6 ID and under any page it was bound to.
    """

    def __init__(self, same_project: Callable[[str | None, str | None], bool] | None = None) -> None:
        self._same_project = same_project or (lambda a, b: a == b)
        self._drives: dict[str, Drive] = {}
        self._events: dict[str, list[CustodyEvent]] = {}
        self._assignments: dict[str, list[Assignment]] = {}
        self._shipments = 0
        self._shipped: list[tuple[str, CustodyEvent]] = []  # shipment events, in append order

    def add_drive(self, drive: Drive) -> None:
        if drive.serial in self._drives:
            raise DuplicateSerial(drive.serial)
        self._drives[drive.serial] = drive
        self._events[drive.serial] = []
        self._assignments[drive.serial] = []

    def get_drive(self, serial: str) -> Drive | None:
        return self._drives.get(normalize_serial(serial))

    def append_event(self, serial: str, event: CustodyEvent) -> None:
        self._log(serial).append(event)
        if event.shipment_id is not None:
            self._shipped.append((normalize_serial(serial), event))

    def release_shipment(
        self, serials: Iterable[str], *, on: date, party: Party, project: Ref, logged_by: Ref
    ) -> str:
        keys = list(dict.fromkeys(normalize_serial(serial) for serial in serials))
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

    def list_events(self, serial: str) -> tuple[CustodyEvent, ...]:
        return tuple(self._log(serial))

    def status(self, serial: str) -> DriveStatus | None:
        return derive_status(self._log(serial))

    def add_assignment(self, serial: str, assignment: Assignment) -> None:
        self._assigned(serial).append(assignment)

    def open_assignments(self, project: Ref) -> list[tuple[Drive, Assignment]]:
        found = [
            (self._drives[key], assignment)
            for key, assignments in self._assignments.items()
            for assignment in assignments
            if assignment.is_open and self._matches(assignment.project, project)
        ]
        return sorted(found, key=lambda pair: (pair[1].start, pair[1].label, pair[0].serial))

    def _matches(self, held: Ref, asked: Ref) -> bool:
        if held.page_id is None or asked.page_id is None:
            return held == asked  # unresolved text is compared as text
        return self._same_project(held.page_id, asked.page_id)

    def assignment_history(self, serial: str) -> list[Assignment]:
        # sorted() is stable: assignments with the same start keep entry order
        return sorted(self._assigned(serial), key=lambda a: a.start)

    def _assigned(self, serial: str) -> list[Assignment]:
        key = normalize_serial(serial)
        if key not in self._assignments:
            raise UnknownDrive(key)
        return self._assignments[key]

    def _log(self, serial: str) -> list[CustodyEvent]:
        key = normalize_serial(serial)
        if key not in self._events:
            raise UnknownDrive(key)
        return self._events[key]
