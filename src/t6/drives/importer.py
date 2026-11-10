"""One-off import of the legacy drive workbooks into a `DriveStore`.

Needs the `drives` extra (`pip install 't6[drives]'`) for the spreadsheet
reader. The importer writes only through `DriveStore` and never guesses: every
row it cannot read with confidence is listed in the result's review report.

The per-project workbook has one tab per Project. A tab holds an optional
`PROJECT NAME: ...` row, a header row starting with `HARD DRIVE LABEL`, then
one row per drive. A tab can repeat that pattern.

The log sheet workbook has two kinds of tab, told apart by their header row.
An inventory tab (swing drives, archive drives, a batch that was released)
has one row per drive with a serial column. A monthly log tab has one row per
movement, with a date and an `IN/OUT` column; it has no serial column, so a
movement can be imported only when its remarks hold `S/N: <serial>`.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from t6.drives import (
    Assignment,
    CustodyEvent,
    CustodyKind,
    Drive,
    DriveStore,
    Party,
    PartyKind,
    Ref,
    Role,
    parse_capacity_tb,
)
from t6.lookup import Lookup

__all__ = [
    "ImportResult",
    "ReviewItem",
    "import_log_sheet_workbook",
    "import_per_project_workbook",
]

_PROJECT_NAME = re.compile(r"PROJECT NAME\s*:\s*(.*)", re.IGNORECASE | re.DOTALL)
_TAB_DATE = re.compile(r"DATE\s*:\s*(.+)", re.IGNORECASE)
_LABEL = "HARD DRIVE LABEL"
_SERIAL_COLUMN = "SERIAL NUMBER"
_SERIAL = re.compile(r"[A-Z0-9-]+")
_SERIAL_IN_REMARKS = re.compile(r"S/N\s*:?\s*([A-Z0-9-]+)", re.IGNORECASE)
_OLD_NEW = re.compile(r"\b(OLD|NEW)\b")
_DEAD = re.compile(r"\bDEAD\b", re.IGNORECASE)
_ROLE_WORDS = {
    Role.MASTER: r"MASTER",
    Role.BACKUP: r"BACK ?UP|B\. ?UP|\bBU ?\d*\b",
    Role.OFFLINE: r"OFFLINE",
    Role.SWING: r"SWING",
    Role.ARCHIVE: r"ARCHIVE",
}


@dataclass(frozen=True, slots=True)
class ReviewItem:
    """One thing in a workbook a person has to look at, with the row it came from.

    `reason` is one of: `no label`, `note in serial cell`, `shared serial` and
    `unreadable` (the row was not imported); `blank serial` (imported as a
    provisional Drive); `old and new labels` (imported with its label as
    written); `unresolved project` (its rows were imported with the Project
    name as text; `row` is where the Project's rows start); `movements with no
    serial` (one per monthly log tab: the detail counts the rows that were not
    imported).
    """

    sheet: str
    row: int
    reason: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ImportResult:
    """What one import run did: how many rows it imported, and what needs review."""

    imported: int
    review: tuple[ReviewItem, ...]


@dataclass(frozen=True, slots=True)
class _Row:
    sheet: str
    number: int
    project: str
    project_row: int
    cells: dict[str, Any]
    released_on: date | None = None  # the date on a tab that lists one released batch

    def text(self, column: str) -> str | None:
        value = self.cells.get(column)
        if value is None or not str(value).strip():
            return None
        return str(value).strip()

    @property
    def place(self) -> str:
        return f"{self.sheet} row {self.number}"


_Facts = tuple[Drive, Assignment, list[CustodyEvent]]


def import_per_project_workbook(
    path: str | Path, store: DriveStore, *, lookup: Lookup, logged_by: Ref
) -> ImportResult:
    """Import the per-project workbook into `store` and report what needs review.

    A row becomes a Drive with an Assignment for its tab's Project, a received
    event when it has a received date and a released event when it has a
    release date. The recipient of a release is the row's person in charge or,
    failing that, its remarks, kept as text. `logged_by` is who the imported
    events are logged under. A row with a blank serial becomes a provisional
    Drive.

    Nothing is guessed. A row is not imported when it has no label, anything
    but one serial in the serial cell, a serial that is on more than one row,
    or a capacity or date that cannot be read. Those rows, blank serials,
    labels that come in an OLD and NEW pair and Projects that could not be
    resolved are all listed in the result. Running the import again adds
    nothing that is already in the store.
    """
    review: list[ReviewItem] = []
    imported = _import_drive_rows(
        _read_per_project(Path(path)),
        lambda row: _per_project_facts(row, lookup, logged_by),
        store,
        review,
    )
    return ImportResult(imported, tuple(review))


def import_log_sheet_workbook(
    path: str | Path, store: DriveStore, *, lookup: Lookup, logged_by: Ref
) -> ImportResult:
    """Import the log sheet workbook into `store` and report what needs review.

    Inventory tabs: a row becomes a Drive with an Assignment under the tab's
    title (or under the group heading above it in a released-batch tab). A row
    whose remarks say DEAD gets a retired event with the remark as the reason.
    On a tab that carries a `DATE: ...` line, every Drive gets a released event
    on that date, and they all share one shipment ID.

    Monthly log tabs: a movement whose remarks hold `S/N: <serial>` becomes a
    received (IN) or released (OUT) event on that Drive. Releases on one date
    to one recipient share a shipment ID. Movements with no serial are not
    imported; each tab's count is in the review report.

    A Drive already in the store, for example from the per-project workbook,
    keeps its record and gains the new history. The doubtful rows are reported
    as in `import_per_project_workbook`, and a second run adds nothing.
    """
    drive_rows, movements = _read_log_sheet(Path(path))
    review: list[ReviewItem] = []
    imported = _import_drive_rows(
        drive_rows, lambda row: _inventory_facts(row, lookup, logged_by), store, review
    )
    imported += _import_movements(movements, store, lookup, logged_by, review)
    return ImportResult(imported, tuple(review))


# Reading the workbooks


def _filled(values: tuple[Any, ...]) -> bool:
    return any(value is not None and str(value).strip() for value in values)


def _columns(values: tuple[Any, ...]) -> list[str]:
    names = [" ".join(str(value or "").upper().split()) for value in values]
    return [_SERIAL_COLUMN if name in {"SERIAL #", "SERIAL NO", "SERIAL NO."} else name for name in names]


def _read_per_project(path: Path) -> list[_Row]:
    rows = []
    workbook = load_workbook(path, read_only=True, data_only=True)
    for sheet in workbook.worksheets:
        project: str | None = None
        project_row = 1
        columns: list[str] | None = None
        for number, values in enumerate(sheet.iter_rows(values_only=True), start=1):
            first = str(values[0] or "").strip() if values else ""
            named = _PROJECT_NAME.search(first)
            if named:
                project, project_row, columns = named.group(1).strip() or None, number, None
            elif first.upper() == _LABEL:
                columns = _columns(values)
                if project is None:
                    project_row = number
            elif columns is not None and _filled(values):
                rows.append(
                    _Row(sheet.title, number, project or sheet.title, project_row, dict(zip(columns, values)))
                )
    workbook.close()
    return rows


def _read_log_sheet(path: Path) -> tuple[list[_Row], list[_Row]]:
    """The inventory rows and the movement rows of the log sheet workbook."""
    drive_rows, movements = [], []
    workbook = load_workbook(path, read_only=True, data_only=True)
    for sheet in workbook.worksheets:
        project, project_row = sheet.title, 1
        columns: list[str] | None = None
        released_on: date | None = None
        for number, values in enumerate(sheet.iter_rows(values_only=True), start=1):
            texts = [str(value).strip() for value in values if value is not None]
            dated = next((m for m in map(_TAB_DATE.fullmatch, texts) if m), None)
            if dated and columns is None:
                try:
                    released_on = _date(dated.group(1))
                except ValueError:
                    pass  # not a date line after all
            elif _LABEL in (text.upper() for text in texts):
                columns = _columns(values)
            elif columns is not None and _filled(values):
                row = _Row(sheet.title, number, project, project_row, dict(zip(columns, values)), released_on)
                if "IN/OUT" in columns:
                    movements.append(row)
                elif row.text(_LABEL) and not row.text(_SERIAL_COLUMN) and row.text("QUANTITY"):
                    project, project_row = row.text(_LABEL) or project, number  # a group heading
                elif row.text(_LABEL) or row.text(_SERIAL_COLUMN):
                    drive_rows.append(row)
    workbook.close()
    return drive_rows, movements


# Importing rows that each describe one drive


def _import_drive_rows(
    rows: list[_Row], facts: Callable[[_Row], _Facts], store: DriveStore, review: list[ReviewItem]
) -> int:
    sharing = defaultdict(list)
    for row in rows:
        sharing[_serial(row)].append(row.place)
    pairs = _old_new_pairs(rows)
    blank_seen: Counter[Assignment] = Counter()
    unresolved: set[tuple[str, int]] = set()
    imported = 0

    def flag(row: _Row, reason: str, detail: str = "") -> None:
        review.append(ReviewItem(row.sheet, row.number, reason, detail))

    for row in rows:
        serial, label = _serial(row), row.text(_LABEL)
        if label is None:
            flag(row, "no label")
        elif serial is not None and not _SERIAL.fullmatch(serial):
            flag(row, "note in serial cell", row.text(_SERIAL_COLUMN) or "")
        elif serial is not None and len(sharing[serial]) > 1:
            others = [place for place in sharing[serial] if place != row.place]
            flag(row, "shared serial", f"{serial} is also on {', '.join(others)}")
        else:
            try:
                drive, assignment, events = facts(row)
            except ValueError as error:
                flag(row, "unreadable", str(error))
                continue
            _write(store, drive, assignment, events, blank_seen)
            imported += 1
            if assignment.project.unresolved and (row.sheet, row.project_row) not in unresolved:
                unresolved.add((row.sheet, row.project_row))
                review.append(ReviewItem(row.sheet, row.project_row, "unresolved project", row.project))
            if serial is None:
                flag(row, "blank serial", f"{label} is recorded as a provisional Drive")
            if id(row) in pairs:
                flag(row, "old and new labels", f"{label} pairs with {pairs[id(row)]}")
    return imported


def _serial(row: _Row) -> str | None:
    text = row.text(_SERIAL_COLUMN)
    return text.upper() if text else None


def _old_new_pairs(rows: list[_Row]) -> dict[int, str]:
    """Rows whose label, apart from an OLD or NEW mark, is on another row of the Project.

    Maps `id(row)` to the other labels in its group.
    """
    groups = defaultdict(list)
    for row in rows:
        label = row.text(_LABEL)
        if label is not None:
            base = " ".join(re.sub(r"[^A-Z0-9 ]", " ", _OLD_NEW.sub(" ", label.upper())).split())
            groups[(row.sheet, row.project_row, base)].append((row, label))
    pairs = {}
    for group in groups.values():
        if len(group) > 1 and any(_OLD_NEW.search(label.upper()) for _, label in group):
            for row, label in group:
                pairs[id(row)] = ", ".join(other for mate, other in group if mate is not row)
    return pairs


def _per_project_facts(row: _Row, lookup: Lookup, logged_by: Ref) -> _Facts:
    label = row.text(_LABEL)
    assert label is not None
    size = row.text("TOTAL DISK SPACE")
    received = _date(row.cells.get("DATE RECEIVED"))
    released = _date(row.cells.get("DATE RELEASED"))
    remarks = row.text("REMARKS")
    recipient = (row.text("PERSON IN CHARGE") or remarks) if released else None
    if recipient == remarks:
        remarks = None
    project = _project(row.project, lookup)
    drive = Drive(
        serial=_serial(row),
        brand=row.text("BRAND NAME"),
        capacity_tb=parse_capacity_tb(size) if size else None,
        format=row.text("FORMAT"),
    )
    assignment = Assignment(
        label=label, project=project, role=_role(label), start=received, remarks=remarks
    )
    events = []
    if received:
        events.append(
            CustodyEvent(
                kind=CustodyKind.RECEIVED, on=received, party=None, project=project, logged_by=logged_by
            )
        )
    if released:
        party = Party(PartyKind.CONTACT, Ref(text=recipient)) if recipient else None
        events.append(
            CustodyEvent(
                kind=CustodyKind.RELEASED, on=released, party=party, project=project, logged_by=logged_by
            )
        )
    return drive, assignment, events


def _inventory_facts(row: _Row, lookup: Lookup, logged_by: Ref) -> _Facts:
    label = row.text(_LABEL)
    assert label is not None
    size = row.text("TOTAL DISK SPACE")
    remarks = row.text("REMARKS")
    project = _project(row.project, lookup)
    drive = Drive(
        serial=_serial(row),
        capacity_tb=parse_capacity_tb(size) if size else None,
        format=row.text("FORMAT"),
    )
    assignment = Assignment(
        label=label, project=project, role=_role(label), start=None, remarks=remarks
    )
    events = []
    if row.released_on:
        events.append(
            CustodyEvent(
                kind=CustodyKind.RELEASED,
                on=row.released_on,
                party=None,
                project=project,
                logged_by=logged_by,
                shipment_id=f"{row.released_on.isoformat()} {row.sheet}",
            )
        )
    if remarks and _DEAD.search(remarks):
        events.append(
            CustodyEvent(
                kind=CustodyKind.RETIRED,
                on=None,
                party=None,
                project=project,
                logged_by=logged_by,
                reason=remarks,
            )
        )
    return drive, assignment, events


def _write(
    store: DriveStore,
    drive: Drive,
    assignment: Assignment,
    events: list[CustodyEvent],
    blank_seen: Counter[Assignment],
) -> None:
    """Add to the store whatever of this Drive, Assignment and events it lacks."""
    if drive.serial is None:
        # A provisional Drive has no serial to find it by on a second run, so
        # it is found by its Assignment. The nth identical row needs n Drives.
        blank_seen[assignment] += 1
        recorded = sum(
            1
            for held, at in store.open_assignments(assignment.project)
            if held.no_serial and at == assignment
        )
        if recorded >= blank_seen[assignment]:
            return
        key = store.add_drive(drive).key
    else:
        key = drive.serial
        if store.get_drive(key) is None:
            store.add_drive(drive)
    if assignment not in store.assignment_history(key):
        store.add_assignment(key, assignment)
    logged = store.list_events(key)
    for event in events:
        if event not in logged:
            store.append_event(key, event)


# Importing the monthly log: one row per movement


def _import_movements(
    rows: list[_Row], store: DriveStore, lookup: Lookup, logged_by: Ref, review: list[ReviewItem]
) -> int:
    imported = 0
    no_serial: Counter[str] = Counter()
    unresolved: set[str] = set()
    for row in rows:
        found = _SERIAL_IN_REMARKS.search(row.text("REMARKS") or "")
        if found is None:
            no_serial[row.sheet] += 1
            continue
        label = row.text(_LABEL)
        if label is None:
            review.append(ReviewItem(row.sheet, row.number, "no label"))
            continue
        try:
            drive, assignment, event = _movement_facts(row, found.group(1), label, lookup, logged_by)
        except ValueError as error:
            review.append(ReviewItem(row.sheet, row.number, "unreadable", str(error)))
            continue
        _write(store, drive, assignment, [event], Counter())
        imported += 1
        name = assignment.project.text
        if name is not None and name not in unresolved:
            unresolved.add(name)
            review.append(ReviewItem(row.sheet, row.number, "unresolved project", name))
    for sheet, count in no_serial.items():
        review.append(
            ReviewItem(sheet, 1, "movements with no serial", f"{count} rows have no S/N in remarks and were not imported")
        )
    return imported


def _movement_facts(
    row: _Row, serial: str, label: str, lookup: Lookup, logged_by: Ref
) -> tuple[Drive, Assignment, CustodyEvent]:
    direction = (row.text("IN/OUT") or "").upper()
    if direction not in {"IN", "OUT"}:
        raise ValueError(f"cannot read IN/OUT {row.text('IN/OUT')!r}")
    on = _date(row.cells.get("DATE"))
    name = row.text("RECEIVED BY")
    project = _project(row.text("PROJECT NAME") or row.sheet, lookup)
    try:
        capacity = parse_capacity_tb(row.text("HD DESCRIPTION") or "")
    except ValueError:
        capacity = None  # the description is free text and often has no size
    assignment = Assignment(label=label, project=project, role=_role(label), start=None)
    if direction == "IN":
        event = CustodyEvent(
            kind=CustodyKind.RECEIVED,
            on=on,
            party=None,
            project=project,
            logged_by=logged_by,
            reason=f"received by {name}" if name else None,
        )
    else:
        event = CustodyEvent(
            kind=CustodyKind.RELEASED,
            on=on,
            party=Party(PartyKind.CONTACT, Ref(text=name)) if name else None,
            project=project,
            logged_by=logged_by,
            shipment_id=f"{on.isoformat()} to {name.upper()}" if on and name else None,
        )
    return Drive(serial=serial, capacity_tb=capacity), assignment, event


# Reading cells


def _project(name: str, lookup: Lookup) -> Ref:
    """The Project page when exactly one Project has this name, else the name as text."""
    exact = [c for c in lookup.project(None, name=name).candidates if c.score == 1.0]
    if len(exact) == 1:
        return Ref(page_id=exact[0].entity.page_id)
    return Ref(text=name)


def _role(label: str) -> Role | None:
    """The role the label names, or None when it names none or more than one."""
    named = [role for role, words in _ROLE_WORDS.items() if re.search(words, label.upper())]
    return named[0] if len(named) == 1 else None


def _date(value: Any) -> date | None:
    """A date cell, or text such as "October 10, 2022", "Jan 12,2023" or "SEPT 1, 2024"."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    month, *rest = re.sub(r"[.,]", " ", str(value)).split() or [""]
    try:
        return datetime.strptime(" ".join([month[:3], *rest]), "%b %d %Y").date()
    except ValueError:
        raise ValueError(f"cannot read the date {value!r}") from None
