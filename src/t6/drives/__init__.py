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
    "Assignment",
    "Drive",
    "DriveStore",
    "DuplicateSerial",
    "FormFactor",
    "ProjectRef",
    "Role",
    "UnknownDrive",
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


class Role(enum.Enum):
    """What a Drive does for a Project while it is assigned."""

    MASTER = "master"
    BACKUP = "backup"
    OFFLINE = "offline"
    SWING = "swing"
    ARCHIVE = "archive"


@dataclass(frozen=True, slots=True)
class ProjectRef:
    """The Project an Assignment is for: a Notion page ID, or text when unresolved.

    Build one with `ProjectRef.page(...)` or `ProjectRef.text(...)`; exactly one
    of the two is set. Two refs are equal only when both are the same kind and
    the same value, so a page ID never matches free text.
    """

    page_id: str | None = None
    unresolved_text: str | None = None

    def __post_init__(self) -> None:
        page_id = _blank_to_none(self.page_id)
        text = _blank_to_none(self.unresolved_text)
        if (page_id is None) == (text is None):
            raise ValueError("a ProjectRef needs exactly one of a page ID or unresolved text")
        object.__setattr__(self, "page_id", page_id)
        object.__setattr__(self, "unresolved_text", text)

    @classmethod
    def page(cls, page_id: str) -> ProjectRef:
        return cls(page_id=page_id)

    @classmethod
    def text(cls, text: str) -> ProjectRef:
        return cls(unresolved_text=text)

    @property
    def resolved(self) -> bool:
        return self.page_id is not None


@dataclass(frozen=True, slots=True, kw_only=True)
class Assignment:
    """One stretch of a Drive's life on one Project, under one label.

    An Assignment with no `end` is open: the Drive is still out for that
    Project. The label is only unique within a Project's own use of it; the
    same label may be on different Drives in different Projects.
    """

    label: str
    project: ProjectRef
    role: Role
    start: date
    end: date | None = None
    remarks: str | None = None

    def __post_init__(self) -> None:
        label = _blank_to_none(self.label)
        if label is None:
            raise ValueError("an Assignment needs a label")
        if self.end is not None and self.end < self.start:
            raise ValueError(f"an Assignment cannot end ({self.end}) before it starts ({self.start})")
        object.__setattr__(self, "label", label)
        object.__setattr__(self, "remarks", _blank_to_none(self.remarks))

    @property
    def is_open(self) -> bool:
        return self.end is None


class UnknownDrive(LookupError):
    """No Drive with this serial is recorded."""

    def __init__(self, serial: str) -> None:
        super().__init__(f"no Drive with serial {serial!r} is recorded")
        self.serial = serial


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

    def add_assignment(self, serial: str, assignment: Assignment) -> None:
        """Assign a recorded Drive. Raises UnknownDrive if the serial is not recorded.

        Earlier Assignments are kept: relabelling a reused Drive adds a new
        Assignment and never edits or removes an old one. Nothing is closed
        automatically.
        """
        ...

    def open_assignments(self, project: ProjectRef) -> list[tuple[Drive, Assignment]]:
        """The Drives still out for a Project, with their open Assignments.

        Ordered by start date, then label. Another Project that has the same
        label is not included.
        """
        ...

    def assignment_history(self, serial: str) -> list[Assignment]:
        """A Drive's Assignments across all Projects, oldest start first.

        Returns an empty list for a recorded Drive with none. Raises
        UnknownDrive if the serial is not recorded.
        """
        ...
