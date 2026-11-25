"""Physical external drives. Pure data and an interface: nothing here does I/O.

A Drive is a physical external drive (portable or powered desktop), owned by
T6, owned by a Studio, or of unknown ownership. It is never Google Drive. Identity is a permanent opaque ID;
accepted and suspected serials are correctable facts. It is not a
Notion entity: it has no Notion row. Relations, such as the owning Studio, may use Notion page IDs. Assignments in
the t6 database use registered T6 Project IDs.

Records live in a store that implements `DriveStore`. `t6.drives.testing`
provides an in-memory fake for tests.
"""

from __future__ import annotations

import enum
import hashlib
import json
import re
from uuid import uuid4
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Iterable, Literal, Protocol, Sequence

from t6.domain.studio_time import MANILA
from t6.vocabulary import DRIVE_ROLES

if TYPE_CHECKING:
    from t6.drives.importer import ReviewItem

__all__ = [
    "Assignment",
    "CatalogContents",
    "CatalogDiffEntry",
    "CatalogEntry",
    "CatalogOutOfOrder",
    "CatalogSnapshot",
    "CustodyEvent",
    "CustodyKind",
    "Drive",
    "DriveStatus",
    "DriveStore",
    "DuplicateAssignment",
    "DuplicateQrCode",
    "DuplicateSerial",
    "Ownership",
    "FormFactor",
    "ManifestLine",
    "Party",
    "PartyKind",
    "Ref",
    "Role",
    "SourceRow",
    "UnknownDrive",
    "catalog_entries",
    "derive_status",
    "diff_catalog",
    "fold_catalog",
    "new_catalog_snapshot",
    "normalize_qr_code",
    "normalize_serial",
    "open_duplicate",
    "parse_capacity_tb",
    "parse_catalog_time",
    "shipment_manifest",
]

_QR_CODE = re.compile(r"[A-Z0-9_-]{2,64}")
_NOTE = re.compile(r"\([^)]*\)|\[[^\]]*\]")
_SPACES = re.compile(r"\s+")
_SIZE = re.compile(r"(\d+(?:\.\d+)?)\s*(TB|T|GB|G)(?![A-Z])", re.IGNORECASE)
_BARE_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_GB_PER_TB = 1000


def normalize_serial(raw: str) -> str:
    """The serial as stored: notes in brackets removed, trimmed, upper-cased.

    `" abc123 (old) "` and `"ABC123"` both give `"ABC123"`. Runs of spaces
    inside the serial become one space. A cell holding only notes gives `""`.
    """
    without_notes = _NOTE.sub(" ", raw)
    return _SPACES.sub(" ", without_notes).strip().upper()


def normalize_qr_code(raw: str) -> str | None:
    """The QR code as stored: trimmed and upper-cased, so a scan in any case finds the Drive.

    A blank code is no code (None). Otherwise it must be 2 to 64 letters, digits, hyphens or underscores, so it
    is safe in a URL path (`/qr/<code>`) and in a printed label.
    """
    code = raw.strip().upper()
    if not code:
        return None
    if not _QR_CODE.fullmatch(code):
        raise ValueError("a QR code is 2 to 64 letters, digits, hyphens or underscores")
    return code


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


class Ownership(enum.Enum):
    """Who owns a Drive. `UNKNOWN` means nobody has said, which is not the same as T6."""

    T6 = "t6"
    STUDIO = "studio"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True, kw_only=True)
class Drive:
    """One physical Drive. Equality and references use only its permanent ID.

    IDs are generated at construction (UUID4, prefixed t6d_). Supplying an ID
    rehydrates an existing record; adapters must never change or reuse it.
    Ownership is T6 (no `owner_studio_id`), a Studio (`owner_studio_id`), or
    unknown (`ownership_known=False`, and no `owner_studio_id`). It defaults to
    T6, as before.
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
    ownership_known: bool = field(default=True, compare=False)
    purchase_date: date | None = field(default=None, compare=False)
    warranty: str | None = field(default=None, compare=False)
    remarks: str | None = field(default=None, compare=False)
    office_location: str | None = field(default=None, compare=False)
    qr_code: str | None = field(default=None, compare=False)

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
        for name in ("label", "description", "brand", "model", "format", "owner_studio_id", "warranty", "remarks", "review_note", "office_location"):
            object.__setattr__(self, name, _blank_to_none(getattr(self, name)))
        if self.qr_code is not None:
            object.__setattr__(self, "qr_code", normalize_qr_code(self.qr_code))
        if not self.ownership_known and self.owner_studio_id is not None:
            raise ValueError("a Drive of unknown ownership cannot have an owning Studio")
        if not any((self.serial, self.suspected_serial, self.label, self.description,
                    self.brand, self.model, self.remarks)):
            raise ValueError("a Drive needs meaningful identifying detail, not just an ID")

    @property
    def owned_by_t6(self) -> bool:
        """True only when ownership is known to be T6. Unknown ownership is not T6."""
        return self.ownership is Ownership.T6

    @property
    def ownership(self) -> Ownership:
        if not self.ownership_known:
            return Ownership.UNKNOWN
        return Ownership.T6 if self.owner_studio_id is None else Ownership.STUDIO

    @property
    def no_serial(self) -> bool:
        """The Drive has no accepted serial."""
        return self.serial is None

    @property
    def key(self) -> str:
        """Alias for the permanent ID, never the serial."""
        return self.id

    @property
    def version(self) -> str:
        """A short digest of every fact, so it changes whenever a fact changes. Assignments and custody are not facts."""
        return hashlib.sha256(json.dumps(asdict(self), default=str, sort_keys=True).encode()).hexdigest()[:12]


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
    project: Ref | None
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


assert tuple(role.value for role in Role) == DRIVE_ROLES, "Drive roles are listed once, in t6.vocabulary"


@dataclass(frozen=True, slots=True, kw_only=True)
class Assignment:
    """One stretch of a Drive's life on one Project, under one label.

    `project` is a `Ref`: a T6 Project ID in the t6 database, or the Project's
    name as text when it could not be resolved. It is None only for a Drive held
    by a Person alone (a `holder` and no Project, as in a swing lent to a
    person). An Assignment with no `end` is open: the Drive is
    still out for that Project. The same label may be on different Drives in
    different Projects. `role` and `start` are None when they were never
    recorded, as in rows imported from the old workbooks.

    A swing lent out has a `return_date`, the day it is due back; it is cleared
    when the swing is returned. `holder` is the T6 Person ID of the Person who
    has the Drive, when a Person (and not only the Project) holds it.
    """

    label: str | None
    project: Ref | None
    role: Role | None
    start: date | None
    end: date | None = None
    remarks: str | None = None
    return_date: date | None = None
    holder: str | None = None

    def __post_init__(self) -> None:
        label = _blank_to_none(self.label)
        if self.end is not None and self.start is not None and self.end < self.start:
            raise ValueError(f"an Assignment cannot end ({self.end}) before it starts ({self.start})")
        if self.return_date is not None and self.start is not None and self.return_date < self.start:
            raise ValueError(f"an Assignment cannot be due back ({self.return_date}) before it starts ({self.start})")
        holder = _blank_to_none(self.holder)
        if self.project is None and holder is None:
            raise ValueError("an Assignment with no Project needs a holder, the Person who has the Drive")
        object.__setattr__(self, "holder", holder)
        object.__setattr__(self, "label", label)
        object.__setattr__(self, "remarks", _blank_to_none(self.remarks))

    @property
    def is_open(self) -> bool:
        return self.end is None


@dataclass(frozen=True, slots=True)
class SourceRow:
    """Private workbook location of an imported row."""
    workbook: str
    sheet: str
    row: int


class UnknownDrive(LookupError):
    """No Drive with this permanent ID is recorded."""

    def __init__(self, drive_id: str) -> None:
        super().__init__(f"no Drive with ID {drive_id!r} is recorded")
        self.drive_id = drive_id


class DuplicateAssignment(ValueError):
    """The Drive already has an open Assignment for this Project and role."""

    def __init__(self, label: str | None) -> None:
        super().__init__(f"the Drive already has an open Assignment ({label!r}) for this Project and role")
        self.label = label


def open_duplicate(held: Iterable[Assignment], new: Assignment) -> Assignment | None:
    """The open Assignment that makes `new` a second one for its Project and role, if any.

    Only an open `new` with a role can clash; old rows with no role never do.
    Two Assignments with a Project clash on the same Project. A holder also
    clashes with itself: when either Assignment has no Project, the same
    holder and role is a second open one.
    """
    if not new.is_open or new.role is None:
        return None

    def clashes(a: Assignment) -> bool:
        if not a.is_open or a.role is not new.role:
            return False
        if new.project is not None and a.project == new.project:
            return True
        return new.holder is not None and a.holder == new.holder and (new.project is None or a.project is None)

    return next((a for a in held if clashes(a)), None)


class DuplicateSerial(ValueError):
    """A Drive with this serial is already recorded."""

    def __init__(self, serial: str) -> None:
        super().__init__(f"a Drive with serial {serial!r} is already recorded")
        self.serial = serial


class DuplicateQrCode(ValueError):
    """A Drive with this QR code is already recorded."""

    def __init__(self, qr_code: str) -> None:
        super().__init__(f"a Drive with QR code {qr_code!r} is already recorded")
        self.qr_code = qr_code


class CatalogOutOfOrder(ValueError):
    """A catalog snapshot dated on or before the Drive's latest one.

    Each snapshot is stored as the changes since the one before it, so scans are recorded in time order.
    """

    def __init__(self, captured_on: date | datetime, latest: date | datetime) -> None:
        super().__init__(f"a catalog scan must be dated after the latest one ({latest.isoformat()}), not {captured_on.isoformat()}")
        self.captured_on, self.latest = captured_on, latest


def parse_catalog_time(value: str | date | datetime, *, end_of_day: bool = False) -> datetime:
    """Parse or normalize a catalog scan timestamp (or bare date in Manila).

    A bare date (`YYYY-MM-DD` or `date`) becomes the start of that day in Manila (`00:00:00+08:00`), or the end of
    that day (`23:59:59.999999+08:00`) when `end_of_day=True` (for `contents_at`). An ISO 8601 datetime without a
    UTC offset is refused with `ValueError`.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("catalog timestamp must include a UTC offset")
        return value.astimezone(MANILA)
    if isinstance(value, date):
        return datetime.combine(value, time.max if end_of_day else time.min, MANILA)
    if isinstance(value, str):
        if _BARE_DATE.fullmatch(value):
            day = date.fromisoformat(value)
            return datetime.combine(day, time.max if end_of_day else time.min, MANILA)
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("catalog timestamp must include a UTC offset")
        return parsed.astimezone(MANILA)
    raise TypeError(f"expected date, datetime or ISO string, not {type(value).__name__}")


@dataclass(frozen=True, slots=True, kw_only=True)
class CatalogEntry:
    """One file or folder of a scanned Drive.

    `path` is relative to the scanned root, with forward slashes. A file has a size; a folder has none. `mtime` is
    the scan's own text (ISO 8601 is expected) and `checksum` is optional; both are kept as given.
    """

    path: str
    kind: Literal["file", "dir"]
    size_bytes: int | None = None
    mtime: str | None = None
    checksum: str | None = None

    def __post_init__(self) -> None:
        parts = [part.strip() for part in self.path.replace("\\", "/").strip().strip("/").split("/")]
        if self.path.strip().startswith("/") or not all(parts) or any(part in (".", "..") for part in parts) or "\x00" in self.path:
            raise ValueError(f"cannot read catalog path {self.path!r}: expected a relative path inside the Drive")
        object.__setattr__(self, "path", "/".join(parts))
        if self.kind not in ("file", "dir"):
            raise ValueError(f"catalog kind must be file or dir, not {self.kind!r}")
        if self.kind == "dir" and self.size_bytes is not None:
            raise ValueError("a folder has no size")
        if self.kind == "file" and (not isinstance(self.size_bytes, int) or isinstance(self.size_bytes, bool) or self.size_bytes < 0):
            raise ValueError("a file needs a size in bytes, 0 or more")
        for name in ("mtime", "checksum"):
            object.__setattr__(self, name, _blank_to_none(getattr(self, name)))


@dataclass(frozen=True, slots=True, kw_only=True)
class CatalogDiffEntry:
    """One change a snapshot made to the tree before it. A removed entry keeps the facts it had."""

    path: str
    kind: Literal["file", "dir"]
    change: Literal["added", "modified", "removed"]
    size_bytes: int | None = None
    mtime: str | None = None
    checksum: str | None = None

    @property
    def entry(self) -> CatalogEntry:
        return CatalogEntry(path=self.path, kind=self.kind, size_bytes=self.size_bytes, mtime=self.mtime, checksum=self.checksum)


@dataclass(frozen=True, slots=True, kw_only=True)
class CatalogSnapshot:
    """One timed scan of a Drive. Its entries are not stored whole: see `CatalogDiffEntry`.

    `entry_count` and `total_bytes` describe the whole tree as of this scan, not just the changes.
    """

    id: str
    drive_id: str
    captured_on: datetime
    parent_snapshot_id: str | None
    root_path: str
    entry_count: int
    total_bytes: int
    created_at: float


@dataclass(frozen=True, slots=True)
class CatalogContents:
    """What a Drive held at a time: the scan in effect then and the whole tree as of it, in path order."""

    snapshot: CatalogSnapshot
    entries: tuple[CatalogEntry, ...]


def catalog_entries(raw: Iterable[Mapping[str, Any]]) -> tuple[CatalogEntry, ...]:
    """Read a scan's manifest: each entry checked, a path listed twice refused, the result in path order."""
    entries = [CatalogEntry(**dict(item)) for item in raw]
    paths = [entry.path for entry in entries]
    if len(set(paths)) != len(paths):
        raise ValueError("a catalog path is listed more than once")
    return tuple(sorted(entries, key=lambda entry: entry.path))


def diff_catalog(previous: Mapping[str, CatalogEntry], incoming: Sequence[CatalogEntry]) -> tuple[CatalogDiffEntry, ...]:
    """What changed from `previous` (path to entry) to `incoming`, in path order."""
    now = {entry.path: entry for entry in incoming}
    changes: list[CatalogDiffEntry] = []
    for path in sorted(previous.keys() | now.keys()):
        old, new = previous.get(path), now.get(path)
        if new is not None and old is None:
            change = "added"
        elif new is None:
            change, new = "removed", old
        elif new != old:
            change = "modified"
        else:
            continue
        assert new is not None
        changes.append(CatalogDiffEntry(path=path, kind=new.kind, change=change, size_bytes=new.size_bytes, mtime=new.mtime, checksum=new.checksum))  # type: ignore[arg-type]
    return tuple(changes)


def fold_catalog(diffs: Iterable[Iterable[CatalogDiffEntry]]) -> dict[str, CatalogEntry]:
    """The tree after applying each snapshot's changes in order."""
    tree: dict[str, CatalogEntry] = {}
    for changes in diffs:
        for change in changes:
            if change.change == "removed":
                tree.pop(change.path, None)
            else:
                tree[change.path] = change.entry
    return tree


def new_catalog_snapshot(drive_id: str, history: Sequence[tuple[CatalogSnapshot, Sequence[CatalogDiffEntry]]], captured_on: date | datetime, root_path: str,
                         entries: Sequence[CatalogEntry], created_at: float) -> tuple[CatalogSnapshot, tuple[CatalogDiffEntry, ...]]:
    """The snapshot and the changes a new scan adds to a Drive's `history` (oldest first).

    Raises CatalogOutOfOrder when the scan is not dated after the latest one.
    """
    moment = parse_catalog_time(captured_on)
    latest = history[-1][0] if history else None
    if latest is not None and moment <= latest.captured_on:
        raise CatalogOutOfOrder(moment, latest.captured_on)
    diff = diff_catalog(fold_catalog(changes for _, changes in history), entries)
    snapshot = CatalogSnapshot(id=f"snap_{uuid4().hex}", drive_id=drive_id, captured_on=moment, parent_snapshot_id=latest.id if latest else None,
                               root_path=root_path.strip() or "/", entry_count=len(entries), total_bytes=sum(entry.size_bytes or 0 for entry in entries),
                               created_at=created_at)
    return snapshot, diff


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

    def list_drives(self) -> tuple[Drive, ...]:
        """Every recorded Drive, in the order they were added."""
        ...

    def find_by_serial(self, serial: str) -> Drive | None:
        """Normalize and match accepted serials only; never suspected values."""
        ...

    def find_by_qr_code(self, qr_code: str) -> Drive | None:
        """Find by QR code, in any case. Unknown codes return None."""
        ...

    def record_catalog(self, drive_id: str, captured_on: date | datetime, root_path: str, entries: Sequence[CatalogEntry], created_at: float
                       ) -> tuple[CatalogSnapshot, tuple[CatalogDiffEntry, ...]]:
        """Record a timed scan as the changes since the Drive's latest one, and return the snapshot with those changes.

        Raises UnknownDrive, or CatalogOutOfOrder when `captured_on` is not after the latest snapshot.
        """
        ...

    def catalog_snapshots(self, drive_id: str) -> tuple[CatalogSnapshot, ...]:
        """The Drive's snapshots, oldest first. Raises UnknownDrive."""
        ...

    def catalog_diff(self, drive_id: str, snapshot_id: str) -> tuple[CatalogDiffEntry, ...] | None:
        """What one snapshot changed, in path order; None when the Drive has no such snapshot."""
        ...

    def contents_at(self, drive_id: str, on: date | datetime) -> CatalogContents | None:
        """The tree as of the latest snapshot at or before `on` (a bare date means end of day in Manila); None when the Drive had none by then. Raises UnknownDrive."""
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

    def set_label(self, drive_id: str, label: str | None) -> Drive:
        """Correct or clear the label. Clearing the only identifying detail raises ValueError."""
        ...

    def set_form_factor(self, drive_id: str, form_factor: FormFactor | None) -> Drive:
        """Correct or clear the form factor."""
        ...

    def set_facts(self, drive_id: str, changes: Mapping[str, Any]) -> Drive:
        """Change several Drive facts at once, named as the Drive fields are.

        The Drive is checked as it will be after all the changes, so one fact can be
        cleared while another is set. Raises ValueError (nothing changes) if the result is
        not a valid Drive, and DuplicateSerial if a serial is already held.
        """
        ...

    def save_preview(self, nonce: str, snapshot: str) -> None:
        """Keep what a Drive verb planned under its preview nonce, so the confirm can check nothing changed."""
        ...

    def preview(self, nonce: str) -> str | None:
        """What `save_preview` kept for the nonce, or None."""
        ...

    def imported_drive(self, row_key: str) -> str | None:
        """Importer receipt: ID previously associated with an opaque row key.

        This is importer bookkeeping, not identity evidence or field provenance.
        Persistent adapters must persist receipts with the imported changes.
        """
        ...

    def import_row(
        self, row_key: str, drive: Drive, assignment: Assignment | None, events: Sequence[CustodyEvent],
        *, source: SourceRow | None = None, deduplicate_assignment: bool = True
    ) -> None:
        """Atomically import a row and its receipt, or do nothing if already recorded.

        Match only an accepted serial, otherwise add the new permanent ID.
        Preserve existing physical facts, combine review notes, add an Assignment
        unless equal to one already held (reload disables deduplication; None adds none), and append every supplied
        event in order.
        Distinct row keys preserve repeated events, even with identical facts.
        Persist all changes and the receipt in one transaction; failure writes nothing.
        """
        ...

    def resolve_project(self, project: Ref, fallback: str) -> Ref:
        """Resolve a source Project reference or preserve the original text."""
        ...

    def record_exceptions(self, items: Sequence[ReviewItem]) -> None:
        """Preserve traceable import review evidence without duplicating a rerun."""
        ...

    def exceptions(self) -> tuple[ReviewItem, ...]:
        """Return private import review evidence."""
        ...

    def sources(self, drive_id: str) -> tuple[SourceRow, ...]:
        """Return source rows that produced this Drive."""
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

    def statuses(self, drive_ids: Sequence[str] | None = None) -> dict[str, DriveStatus | None]:
        """Current derived status for each requested Drive ID (or every recorded Drive when None).

        Drives with no custody events map to None. Unknown IDs in `drive_ids` are omitted.
        """
        ...

    def open_assignments_by_drive(
        self, drive_ids: Sequence[str] | None = None
    ) -> dict[str, tuple[tuple[int, Assignment], ...]]:
        """Open Assignments keyed by Drive ID, omitting Drives with none.

        Each entry holds `(number, assignment)` pairs where `number` is the 1-based position
        in the Drive's full Assignment list (matching `list_assignments`).
        """
        ...

    def release_shipment(
        self, drive_ids: Iterable[str], *, on: date, party: Party, project: Ref, logged_by: Ref,
        shipment_id: str | None = None, reason: str | None = None, recipient: str | None = None
    ) -> str:
        """Release several Drives in one handover and return the new shipment ID.

        A caller that must give the same shipment on a retry passes its own
        `shipment_id`; otherwise the store makes a new one. `reason` goes on
        every released event. `recipient` is the free-text name of the person
        who took the Drives. The store also keeps each Drive's facts as they are
        now (see `shipment_facts`).

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

    def shipment_facts(self, shipment_id: str) -> tuple[str | None, dict[str, Drive]] | None:
        """The recipient of a shipment and each Drive's facts as they were at release, by Drive ID.

        None for a shipment with no kept facts: an unknown ID, or one made from
        events alone (the workbook importers, or a release before facts were kept).
        """
        ...

    def list_shipments(self) -> tuple[str, ...]:
        """The ID of every shipment, once each, in the order each was first released."""
        ...

    def add_assignment(self, drive_id: str, assignment: Assignment) -> None:
        """Assign a recorded Drive. Raises UnknownDrive if the ID is not recorded.

        An open Assignment with a role is refused (DuplicateAssignment) while the
        Drive holds another open one for the same Project and role, or for the
        same holder and role when either has no Project.

        Earlier Assignments are kept: relabelling a reused Drive adds a new
        Assignment and never edits or removes an old one. Nothing is closed
        automatically.
        """
        ...

    def list_assignments(self, drive_id: str) -> tuple[Assignment, ...]:
        """A Drive's Assignments in the order they were added.

        The place in this list, counted from 1, is the Assignment number: it
        never changes, because an Assignment is never removed. Raises
        UnknownDrive if the ID is not recorded.
        """
        ...

    def end_assignment(self, drive_id: str, number: int, end: date) -> Assignment:
        """End the open Assignment with this number and return it as ended.

        Raises ValueError if there is no such Assignment, it is already ended,
        or `end` is before its start; UnknownDrive if the ID is not recorded.
        Nothing else changes.
        """
        ...

    def check_project(self, project: Ref) -> None:
        """Raise ValueError unless `project` can be used on an Assignment or event.

        Unresolved text is always accepted. An ID must be a T6 Project ID, and
        a store on the t6 database also needs the Project to be registered.
        """
        ...

    def drop_return_date(self, drive_id: str, number: int) -> Assignment:
        """Clear the return date of the Assignment with this number and return it.

        Raises ValueError if there is no such Assignment; UnknownDrive if the ID
        is not recorded. Clearing an Assignment with no return date changes nothing.
        """
        ...

    def overdue_returns(self, today: date) -> list[tuple[Drive, Assignment]]:
        """The Drives still out whose open Assignment was due back before `today`.

        Ordered by return date, then label, then Drive ID. An ended Assignment,
        or one with no return date, is never overdue.
        """
        ...

    def resolve_person(self, contact_page_id: str) -> str | None:
        """The T6 Person ID bound to a Contacts page ID, or None if no Person is bound to it."""
        ...

    def check_person(self, person_id: str) -> None:
        """Raise ValueError unless `person_id` can be the holder of an Assignment.

        An ID must be a T6 Person ID, and a store on the t6 database also needs
        the Person to be registered.
        """
        ...

    def link_delivery(self, delivery_id: str, drive_ids: Iterable[str]) -> None:
        """Link Drives to a Delivery. Linking again changes nothing.

        Raises UnknownDrive if any ID is not recorded; then nothing is linked.
        """
        ...

    def delivery_drives(self, delivery_id: str) -> tuple[str, ...]:
        """The IDs of the Drives linked to a Delivery, sorted. Empty if none."""
        ...

    def drive_deliveries(self, drive_id: str) -> tuple[str, ...]:
        """The IDs of the Deliveries a Drive is linked to, sorted. Raises UnknownDrive if the ID is not recorded."""
        ...

    def delivery_carrier(self, delivery_id: str) -> tuple[str, str] | None:
        """The carrier kind and Project ID of a Delivery, or None if it is not recorded."""
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
