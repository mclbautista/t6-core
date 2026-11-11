"""Physical external drives. Pure data and an interface: nothing here does I/O.

A Drive is a physical external drive (portable or powered desktop), owned by
T6 or by a Studio. It is never Google Drive. Identity is a permanent opaque ID;
accepted and suspected serials are correctable facts. It is not a
Notion entity: it has no Notion row. Relations, such as the owning Studio, are
Notion page IDs.

Records live in a store that implements `DriveStore`. `t6.drives.testing`
provides an in-memory fake for tests.
"""

from __future__ import annotations

import enum
import re
from uuid import uuid4
from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, Protocol, Sequence

__all__ = [
    "Assignment",
    "CustodyEvent",
    "CustodyKind",
    "Drive",
    "DriveStatus",
    "DriveStore",
    "DuplicateSerial",
    "FormFactor",
    "ManifestLine",
    "Party",
    "PartyKind",
    "Ref",
    "Role",
    "UnknownDrive",
    "derive_status",
    "normalize_serial",
    "parse_capacity_tb",
    "shipment_manifest",
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
    """One physical Drive. Equality and references use only its permanent ID.

    IDs are generated at construction (UUID4, prefixed t6d_). Supplying an ID
    rehydrates an existing record; adapters must never change or reuse it.
    A serial is an optional accepted fact, not identity. Suspected serials are
    explicitly uncertain and must never participate in exact serial matching.
    """

    id: str = field(default_factory=lambda: f"t6d_{uuid4().hex}")
    serial: str | None = field(default=None, compare=False)
    suspected_serial: str | None = field(default=None, compare=False)
    label: str | None = field(default=None, compare=False)
    description: str | None = field(default=None, compare=False)
    needs_review: bool = field(default=False, compare=False)
    review_note: str | None = field(default=None, compare=False)
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
        if not re.fullmatch(r"t6d_[0-9a-f]{32}", self.id):
            raise ValueError("invalid Drive ID: expected t6d_ followed by 32 lowercase hex digits")
        for name in ("serial", "suspected_serial"):
            value = getattr(self, name)
            if value is not None:
                normalized = normalize_serial(value)
                if not normalized:
                    raise ValueError(f"{name} holds no serial; use serial=None for no serial")
                object.__setattr__(self, name, normalized)
        if self.capacity_tb is not None and self.capacity_tb <= 0:
            raise ValueError(f"capacity must be more than 0 TB, got {self.capacity_tb}")
        for name in ("label", "description", "brand", "model", "format", "owner_studio_id", "warranty", "remarks", "review_note"):
            object.__setattr__(self, name, _blank_to_none(getattr(self, name)))
        if not any((self.serial, self.suspected_serial, self.label, self.description,
                    self.brand, self.model, self.remarks)):
            raise ValueError("a Drive needs meaningful identifying detail, not just an ID")

    @property
    def owned_by_t6(self) -> bool:
        return self.owner_studio_id is None

    @property
    def no_serial(self) -> bool:
        """The Drive has no accepted serial."""
        return self.serial is None

    @property
    def key(self) -> str:
        """Alias for the permanent ID, never the serial."""
        return self.id


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
    and an optional reason. `on` and `party` are None when the date or who
    was involved was never recorded, as in rows imported from the old
    workbooks. Retired needs a reason (for example dead, lost,
    sold). An event is never edited: a correction is a new event.

    Released events that share a `shipment_id` are one shipment: one handover
    of several Drives. Only a released event can carry one.
    """

    kind: CustodyKind
    on: date | None
    party: Party | None
    project: Ref
    logged_by: Ref
    reason: str | None = None
    shipment_id: str | None = None

    def __post_init__(self) -> None:
        reason = _blank_to_none(self.reason)
        if self.kind is CustodyKind.RETIRED and reason is None:
            raise ValueError("a retired event needs a reason, such as dead, lost or sold")
        shipment_id = _blank_to_none(self.shipment_id)
        if shipment_id is not None and self.kind is not CustodyKind.RELEASED:
            raise ValueError(f"only a released event can be part of a shipment, not {self.kind.value}")
        object.__setattr__(self, "reason", reason)
        object.__setattr__(self, "shipment_id", shipment_id)


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
    different Projects. `role` and `start` are None when they were never
    recorded, as in rows imported from the old workbooks.
    """

    label: str | None
    project: Ref
    role: Role | None
    start: date | None
    end: date | None = None
    remarks: str | None = None

    def __post_init__(self) -> None:
        label = _blank_to_none(self.label)
        if self.end is not None and self.start is not None and self.end < self.start:
            raise ValueError(f"an Assignment cannot end ({self.end}) before it starts ({self.start})")
        object.__setattr__(self, "label", label)
        object.__setattr__(self, "remarks", _blank_to_none(self.remarks))

    @property
    def is_open(self) -> bool:
        return self.end is None


class UnknownDrive(LookupError):
    """No Drive with this permanent ID is recorded."""

    def __init__(self, drive_id: str) -> None:
        super().__init__(f"no Drive with ID {drive_id!r} is recorded")
        self.drive_id = drive_id


class DuplicateSerial(ValueError):
    """A Drive with this serial is already recorded."""

    def __init__(self, serial: str) -> None:
        super().__init__(f"a Drive with serial {serial!r} is already recorded")
        self.serial = serial


class DriveStore(Protocol):
    """Store addressed by exact, case-sensitive permanent Drive IDs.

    Mutations fail atomically. Serial/review edits do not touch history or
    shipment membership and impose no extra workflow restrictions.
    """

    def add_drive(self, drive: Drive) -> Drive:
        """Record a new ID; reject a duplicate ID or accepted serial."""
        ...

    def get_drive(self, drive_id: str) -> Drive | None:
        """Find by permanent ID only. Unknown IDs return None."""
        ...

    def find_by_serial(self, serial: str) -> Drive | None:
        """Normalize and match accepted serials only; never suspected values."""
        ...

    def has_suspected_serial(self, serial: str) -> bool:
        """Whether a normalized candidate needs review; never establishes identity."""
        ...

    def set_serial(self, drive_id: str, serial: str | None) -> Drive:
        """Add/correct/remove the accepted serial; collisions raise DuplicateSerial.

        Blank serial strings are invalid. Removal uses None and requires another
        meaningful identifying detail; otherwise ValueError leaves state intact. Validation and
        uniqueness checks precede all changes; no automatic demotion or merge.
        """
        ...

    def set_suspected_serial(self, drive_id: str, serial: str | None) -> Drive:
        """Retain/remove an uncertain candidate, even if another Drive holds it."""
        ...

    def promote_suspected_serial(self, drive_id: str) -> Drive:
        """Explicitly accept the candidate and clear it, only if unique."""
        ...

    def set_review(self, drive_id: str, *, needs_review: bool, note: str | None = None) -> Drive:
        """Set general review state without changing serials or workflow rights."""
        ...

    def imported_drive(self, row_key: str) -> str | None:
        """Importer receipt: ID previously associated with an opaque row key.

        This is importer bookkeeping, not identity evidence or field provenance.
        Persistent adapters must persist receipts with the imported changes.
        """
        ...

    def import_row(
        self, row_key: str, drive: Drive, assignment: Assignment, events: Sequence[CustodyEvent]
    ) -> None:
        """Atomically import a row and its receipt, or do nothing if already recorded.

        Match only an accepted serial, otherwise add the new permanent ID.
        Preserve existing physical facts, combine review notes, add an Assignment
        unless equal to one already held, and append every supplied event in order.
        Distinct row keys preserve repeated events, even with identical facts.
        Persist all changes and the receipt in one transaction; failure writes nothing.
        """
        ...

    def append_event(self, drive_id: str, event: CustodyEvent) -> None:
        """Add an event to the end of a Drive's custody log.

        Raises UnknownDrive if no Drive has this ID. The store has no way to
        edit or delete an event: a correction is a new event.
        """
        ...

    def list_events(self, drive_id: str) -> tuple[CustodyEvent, ...]:
        """The Drive's custody events in the order they were appended.

        Raises UnknownDrive if no Drive has this ID. A Drive with no events
        gives an empty tuple.
        """
        ...

    def status(self, drive_id: str) -> DriveStatus | None:
        """The Drive's current status, worked out from its log.

        None if the Drive has no events yet. Raises UnknownDrive if no Drive
        has this ID.
        """
        ...

    def release_shipment(
        self, drive_ids: Iterable[str], *, on: date, party: Party, project: Ref, logged_by: Ref
    ) -> str:
        """Release several Drives in one handover and return the new shipment ID.

        Appends one released event per Drive, all with the same new shipment ID.
        An ID given twice is released once. Raises ValueError if no ID is
        given, and UnknownDrive if any ID is not recorded; then nothing is
        released.
        """
        ...

    def shipment_events(self, shipment_id: str) -> tuple[tuple[Drive, CustodyEvent], ...]:
        """The released events of a shipment, each with its Drive.

        In the order they were appended. An unknown shipment ID gives an empty
        tuple.
        """
        ...

    def add_assignment(self, drive_id: str, assignment: Assignment) -> None:
        """Assign a recorded Drive. Raises UnknownDrive if the ID is not recorded.

        Earlier Assignments are kept: relabelling a reused Drive adds a new
        Assignment and never edits or removes an old one. Nothing is closed
        automatically.
        """
        ...

    def open_assignments(self, project: Ref) -> list[tuple[Drive, Assignment]]:
        """The Drives still out for a Project, with their open Assignments.

        Ordered by start date (unknown starts first), then label. Another Project that has the same
        label is not included.
        """
        ...

    def assignment_history(self, drive_id: str) -> list[Assignment]:
        """A Drive's Assignments across all Projects, oldest start first.

        Assignments with no recorded start come first. Returns an empty list
        for a recorded Drive with none. Raises UnknownDrive if the ID is not
        recorded.
        """
        ...


@dataclass(frozen=True, slots=True, kw_only=True)
class ManifestLine:
    """One Drive on a shipment manifest: what went out, to whom, when, logged by whom.

    `label` is the label the Drive carried on the release date, or None if it
    had no Assignment then. Accepted and suspected serials remain distinct.
    Identifying facts reflect the current record, not a rewritten custody event.
    """

    drive_id: str
    serial: str | None
    suspected_serial: str | None = None
    description: str | None = None
    needs_review: bool = False
    review_note: str | None = None
    label: str | None
    capacity_tb: float | None
    recipient: Party | None
    on: date | None
    logged_by: Ref


def shipment_manifest(store: DriveStore, shipment_id: str) -> tuple[ManifestLine, ...]:
    """The manifest data for a shipment, one line per Drive, from the store alone.

    The label comes from the Assignment the Drive was on at the release date,
    so a manifest made later still shows the label that was on the sticker. If
    several Assignments cover that date, the one that started last is used; an
    Assignment with no recorded start counts as started before any date. When
    the release has no recorded date, the Drive's open Assignments are used. An
    unknown shipment ID gives an empty tuple.
    """
    lines = []
    for drive, event in store.shipment_events(shipment_id):
        on = event.on
        current = [
            a
            for a in store.assignment_history(drive.key)
            if (on is None or a.start is None or a.start <= on)
            and (a.end is None or (on is not None and a.end >= on))
        ]
        lines.append(
            ManifestLine(
                drive_id=drive.id,
                serial=drive.serial,
                suspected_serial=drive.suspected_serial,
                description=drive.description,
                needs_review=drive.needs_review,
                review_note=drive.review_note,
                label=(current[-1].label or drive.label) if current else drive.label,
                capacity_tb=drive.capacity_tb,
                recipient=event.party,
                on=event.on,
                logged_by=event.logged_by,
            )
        )
    return tuple(lines)
