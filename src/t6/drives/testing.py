"""An in-memory DriveStore for tests. Nothing is kept after the process ends."""

from __future__ import annotations

from t6.drives import (
    Assignment,
    CustodyEvent,
    Drive,
    DriveStatus,
    DuplicateSerial,
    Ref,
    UnknownDrive,
    derive_status,
    normalize_serial,
)


class InMemoryDriveStore:
    """A DriveStore that keeps Drives in a dict keyed by normalized serial."""

    def __init__(self) -> None:
        self._drives: dict[str, Drive] = {}
        self._events: dict[str, list[CustodyEvent]] = {}
        self._assignments: dict[str, list[Assignment]] = {}

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
            if assignment.is_open and assignment.project == project
        ]
        return sorted(found, key=lambda pair: (pair[1].start, pair[1].label, pair[0].serial))

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
