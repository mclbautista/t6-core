"""An in-memory DriveStore for tests. Nothing is kept after the process ends."""

from __future__ import annotations

from t6.drives import Drive, DuplicateSerial, normalize_serial


class InMemoryDriveStore:
    """A DriveStore that keeps Drives in a dict keyed by normalized serial."""

    def __init__(self) -> None:
        self._drives: dict[str, Drive] = {}

    def add_drive(self, drive: Drive) -> None:
        if drive.serial in self._drives:
            raise DuplicateSerial(drive.serial)
        self._drives[drive.serial] = drive

    def get_drive(self, serial: str) -> Drive | None:
        return self._drives.get(normalize_serial(serial))
