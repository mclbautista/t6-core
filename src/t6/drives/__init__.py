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
from typing import Protocol, Sequence

__all__ = [
    "Assignment",
    "CustodyEvent",
    "CustodyKind",
    "Drive",
    "DriveStatus",
    "DriveStore",
    "DuplicateSerial",
    "FormFactor",
    "Party",
    "PartyKind",
    "Ref",
    "Role",
    "UnknownDrive",
    "derive_status",
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


@dataclass(frozen=True, slots=True, kw_only=True)
class Ref:
    """A link to a Notion page, or the text of a name that could not be resolved.

    A resolved Ref holds a Notion page ID. When a name could not be matched to a
    page, the Ref keeps the name as text and is flagged: `unresolved` is True so
    a reviewer can find it. Exactly one of `page_id` and `text` is set.
    """

    page_id: str | None = None
    text: str | None = None

    def __post_init__(self) -> None:
        page_id = _blank_to_none(self.page_id)
        text = _blank_to_none(self.text)
        if (page_id is None) == (text is None):
            raise ValueError("a Ref needs exactly one of a page ID or unresolved text")
        object.__setattr__(self, "page_id", page_id)
        object.__setattr__(self, "text", text)

    @property
    def unresolved(self) -> bool:
        return self.page_id is None


class PartyKind(enum.Enum):
    CONTACT = "contact"
    STUDIO = "studio"


@dataclass(frozen=True, slots=True)
class Party:
    """The Contact or Studio a custody event involves."""

    kind: PartyKind
    ref: Ref


class CustodyKind(enum.Enum):
    RECEIVED = "received"
    RELEASED = "released"
    RETURNED = "returned"
    RETIRED = "retired"


class DriveStatus(enum.Enum):
    """Where a Drive stands now. Derived from its custody log; never stored."""

    IN_CUSTODY = "in custody"
    RELEASED = "released"
    RETIRED = "retired"


@dataclass(frozen=True, slots=True, kw_only=True)
class CustodyEvent:
    """One entry in a Drive's custody log: received, released, returned or retired.

    Holds the date, the Contact or Studio involved, the Project, who logged it
    and an optional reason. Retired needs a reason (for example dead, lost,
    sold). An event is never edited: a correction is a new event.
    """

    kind: CustodyKind
    on: date
    party: Party
    project: Ref
    logged_by: Ref
    reason: str | None = None

    def __post_init__(self) -> None:
        reason = _blank_to_none(self.reason)
        if self.kind is CustodyKind.RETIRED and reason is None:
            raise ValueError("a retired event needs a reason, such as dead, lost or sold")
        object.__setattr__(self, "reason", reason)


_STATUS_AFTER = {
    CustodyKind.RECEIVED: DriveStatus.IN_CUSTODY,
    CustodyKind.RETURNED: DriveStatus.IN_CUSTODY,
    CustodyKind.RELEASED: DriveStatus.RELEASED,
    CustodyKind.RETIRED: DriveStatus.RETIRED,
}


def derive_status(events: Sequence[CustodyEvent]) -> DriveStatus | None:
    """The status after the last event in the log, or None if there are none.

    The log is in the order events were appended, so a correction logged later
    wins whatever date it carries.
    """
    if not events:
        return None
    return _STATUS_AFTER[events[-1].kind]


class Role(enum.Enum):
    """What a Drive does for a Project while it is assigned."""

    MASTER = "master"
    BACKUP = "backup"
    OFFLINE = "offline"
    SWING = "swing"
    ARCHIVE = "archive"


@dataclass(frozen=True, slots=True, kw_only=True)
class Assignment:
    """One stretch of a Drive's life on one Project, under one label.

    `project` is a `Ref`: a Notion page ID, or the Project's name as text when
    it could not be resolved. An Assignment with no `end` is open: the Drive is
    still out for that Project. The same label may be on different Drives in
    different Projects.
    """

    label: str
    project: Ref
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

    def append_event(self, serial: str, event: CustodyEvent) -> None:
        """Add an event to the end of a Drive's custody log.

        Raises UnknownDrive if no Drive has this serial. The store has no way to
        edit or delete an event: a correction is a new event.
        """
        ...

    def list_events(self, serial: str) -> tuple[CustodyEvent, ...]:
        """The Drive's custody events in the order they were appended.

        Raises UnknownDrive if no Drive has this serial. A Drive with no events
        gives an empty tuple.
        """
        ...

    def status(self, serial: str) -> DriveStatus | None:
        """The Drive's current status, worked out from its log.

        None if the Drive has no events yet. Raises UnknownDrive if no Drive
        has this serial.
        """
        ...

    def add_assignment(self, serial: str, assignment: Assignment) -> None:
        """Assign a recorded Drive. Raises UnknownDrive if the serial is not recorded.

        Earlier Assignments are kept: relabelling a reused Drive adds a new
        Assignment and never edits or removes an old one. Nothing is closed
        automatically.
        """
        ...

    def open_assignments(self, project: Ref) -> list[tuple[Drive, Assignment]]:
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
