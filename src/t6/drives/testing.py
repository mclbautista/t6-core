"""An in-memory DriveStore for tests. Nothing is kept after the process ends."""

from __future__ import annotations

from t6.drives import (
    CustodyEvent,
    Drive,
    DriveStatus,
    DuplicateSerial,
    UnknownDrive,
    derive_status,
    normalize_serial,
)


class InMemoryDriveStore:
    """A DriveStore that keeps Drives in a dict keyed by normalized serial."""

    def __init__(self) -> None:
        self._drives: dict[str, Drive] = {}
        self._events: dict[str, list[CustodyEvent]] = {}

    def add_drive(self, drive: Drive) -> None:
        if drive.serial in self._drives:
            raise DuplicateSerial(drive.serial)
        self._drives[drive.serial] = drive
        self._events[drive.serial] = []

    def get_drive(self, serial: str) -> Drive | None:
        return self._drives.get(normalize_serial(serial))

    def append_event(self, serial: str, event: CustodyEvent) -> None:
        self._log(serial).append(event)

    def list_events(self, serial: str) -> tuple[CustodyEvent, ...]:
        return tuple(self._log(serial))

    def status(self, serial: str) -> DriveStatus | None:
        return derive_status(self._log(serial))

    def _log(self, serial: str) -> list[CustodyEvent]:
        key = normalize_serial(serial)
        if key not in self._events:
            raise UnknownDrive(key)
        return self._events[key]
