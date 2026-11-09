"""Physical external drives. Pure data and an interface: nothing here does I/O.

A Drive is a physical external drive (portable or powered desktop), owned by
T6 or by a Studio. It is never Google Drive. A Drive is identified by its
normalized serial number, so one drive can be recorded only once. It is not a
Notion entity: it has no Notion row. Relations, such as the owning Studio, are
Notion page IDs.

Records live in a store that implements `DriveStore`. `t6.drives.testing`
provides an in-memory fake for tests.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

__all__ = [
    "Drive",
    "DriveStore",
    "DuplicateSerial",
    "FormFactor",
    "normalize_serial",
    "parse_capacity_tb",
]

_NOTE = re.compile(r"\([^)]*\)|\[[^\]]*\]")
_SPACES = re.compile(r"\s+")
_SIZE = re.compile(r"(\d+(?:\.\d+)?)\s*(TB|T|GB|G)(?![A-Z])", re.IGNORECASE)
_GB_PER_TB = 1000


def normalize_serial(raw: str) -> str:
    """The serial as stored: notes in brackets removed, trimmed, upper-cased.

    `" abc123 (old) "` and `"ABC123"` both give `"ABC123"`. Runs of spaces
    inside the serial become one space. A cell holding only notes gives `""`.
    """
    without_notes = _NOTE.sub(" ", raw)
    return _SPACES.sub(" ", without_notes).strip().upper()


def parse_capacity_tb(text: str) -> float:
    """Capacity text from a sheet as terabytes: `"4TB, P"` is 4.0, `"500GB"` is 0.5.

    The text must start with exactly one size in TB or GB (1 TB = 1000 GB).
    Anything after it, such as `", P"`, is a note and is ignored. Text with no
    size, or with more than one, is refused with a ValueError.
    """
    sizes = list(_SIZE.finditer(text))
    if len(sizes) != 1 or text[: sizes[0].start()].strip():
        raise ValueError(
            f"cannot read capacity {text!r}: expected one size such as '4TB' or '500GB'"
        )
    number, unit = sizes[0].groups()
    value = float(number)
    return value / _GB_PER_TB if unit.upper().startswith("G") else value


class FormFactor(enum.Enum):
    PORTABLE = "portable"
    POWERED_DESKTOP = "powered desktop"


def _blank_to_none(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    return value.strip()


@dataclass(frozen=True, slots=True, kw_only=True)
class Drive:
    """One physical external drive, identified by its normalized serial.

    Two Drives with the same serial are the same drive, whatever their other
    facts say. `owner_studio_id` is the owning Studio's Notion page ID; `None`
    means T6 owns the drive. Capacity is in terabytes; use `parse_capacity_tb`
    for sheet text.
    """

    serial: str
    brand: str | None = field(default=None, compare=False)
    model: str | None = field(default=None, compare=False)
    capacity_tb: float | None = field(default=None, compare=False)
    format: str | None = field(default=None, compare=False)
    form_factor: FormFactor | None = field(default=None, compare=False)
    owner_studio_id: str | None = field(default=None, compare=False)
    purchase_date: date | None = field(default=None, compare=False)
    warranty: str | None = field(default=None, compare=False)
    remarks: str | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        serial = normalize_serial(self.serial)
        if not serial:
            raise ValueError(f"a Drive needs a serial; {self.serial!r} has none")
        if self.capacity_tb is not None and self.capacity_tb <= 0:
            raise ValueError(f"capacity must be more than 0 TB, got {self.capacity_tb}")
        object.__setattr__(self, "serial", serial)
        for name in ("brand", "model", "format", "owner_studio_id", "warranty", "remarks"):
            object.__setattr__(self, name, _blank_to_none(getattr(self, name)))

    @property
    def owned_by_t6(self) -> bool:
        return self.owner_studio_id is None


class DuplicateSerial(ValueError):
    """A Drive with this serial is already recorded."""

    def __init__(self, serial: str) -> None:
        super().__init__(f"a Drive with serial {serial!r} is already recorded")
        self.serial = serial


class DriveStore(Protocol):
    """Where Drive records live. The custodian's web app implements it.

    Serials passed in are normalized by the store, so any spelling of a serial
    finds the same Drive.
    """

    def add_drive(self, drive: Drive) -> None:
        """Record a new Drive. Raises DuplicateSerial if its serial is taken."""
        ...

    def get_drive(self, serial: str) -> Drive | None:
        """The Drive with this serial, or None if none is recorded."""
        ...
