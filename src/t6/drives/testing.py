"""An in-memory DriveStore for tests. Nothing is kept after the process ends."""

from __future__ import annotations

from t6.drives import (
    Assignment,
    Drive,
    DuplicateSerial,
    ProjectRef,
    UnknownDrive,
    normalize_serial,
)


class InMemoryDriveStore:
    """A DriveStore that keeps Drives in a dict keyed by normalized serial."""

    def __init__(self) -> None:
        self._drives: dict[str, Drive] = {}
        self._assignments: dict[str, list[Assignment]] = {}

    def add_drive(self, drive: Drive) -> None:
        if drive.serial in self._drives:
            raise DuplicateSerial(drive.serial)
        self._drives[drive.serial] = drive
        self._assignments[drive.serial] = []

    def get_drive(self, serial: str) -> Drive | None:
        return self._drives.get(normalize_serial(serial))

    def add_assignment(self, serial: str, assignment: Assignment) -> None:
        key = normalize_serial(serial)
        if key not in self._drives:
            raise UnknownDrive(serial)
        self._assignments[key].append(assignment)

    def open_assignments(self, project: ProjectRef) -> list[tuple[Drive, Assignment]]:
        found = [
            (self._drives[key], assignment)
            for key, assignments in self._assignments.items()
            for assignment in assignments
            if assignment.is_open and assignment.project == project
        ]
        return sorted(found, key=lambda pair: (pair[1].start, pair[1].label, pair[0].serial))

    def assignment_history(self, serial: str) -> list[Assignment]:
        key = normalize_serial(serial)
        if key not in self._drives:
            raise UnknownDrive(serial)
        return sorted(self._assignments[key], key=lambda a: a.start)  # stable: ties keep entry order
