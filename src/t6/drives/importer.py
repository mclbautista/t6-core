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
import hashlib
import json
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, replace
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
    normalize_serial,
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
_SERIAL_IN_REMARKS = re.compile(r"(?<!\w)S/N\s*:?\s*(\S+)", re.IGNORECASE)
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
    """Local review evidence: workbook, sheet, row and preserved uncertain facts."""

    sheet: str
    row: int
    reason: str
    detail: str = ""
    workbook: str = ""


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
    workbook: str = ""

    @property
    def import_key(self) -> str:
        """Row receipt, not physical identity; distinct rows never share it."""
        raw = json.dumps([self.workbook, self.sheet, self.number])
        return hashlib.sha256(raw.encode()).hexdigest()

    def review(self, reason: str, detail: str = "") -> ReviewItem:
        return ReviewItem(self.sheet, self.number, reason, detail, self.workbook)

    def evidence(self) -> str:
        return json.dumps(self.cells, default=str, ensure_ascii=False)

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
    """Import per-project rows, preserving uncertain facts without guessing identity.

    Clean accepted serials may match existing Drives. Missing, noted or shared
    serials yield distinct review-flagged records, with candidates explicitly
    suspected. Unreadable rows remain traceable review items. Unchanged reruns
    use row receipts and append no duplicate history. See docs/drive-identity.md.
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
    """Import inventory and monthly movements; never associate by labels.

    Inventory rows use the same uncertainty rules as per-project rows. Monthly
    movements need one explicit S/N; unsafe associations are individually
    preserved in the local report. T6 IN means receipt, including undated ones.
    Distinct movement rows remain distinct events; unchanged reruns add nothing.
    """
    drive_rows, movements = _read_log_sheet(Path(path))
    review: list[ReviewItem] = []
    imported = _import_drive_rows(
        drive_rows, lambda row: _inventory_facts(row, lookup, logged_by), store, review
    )
    inventory_serials = [_serial(row) for row in drive_rows]
    uncertain_serials = {
        serial for row, serial in zip(drive_rows, inventory_serials)
        if serial and (inventory_serials.count(serial) > 1
                       or not _SERIAL.fullmatch((row.text(_SERIAL_COLUMN) or "").upper()))
    }
    imported += _import_movements(movements, store, lookup, logged_by, review, uncertain_serials)
    return ImportResult(imported, tuple(review))


# Reading the workbooks


def _filled(values: tuple[Any, ...]) -> bool:
    return any(value is not None and str(value).strip() for value in values)


def _columns(values: tuple[Any, ...]) -> list[str]:
    names = [" ".join(str(value or "").upper().split()) for value in values]
    return [_SERIAL_COLUMN if name in {"SERIAL #", "SERIAL NO", "SERIAL NO."} else name for name in names]


def _cells(columns: list[str], values: tuple[Any, ...]) -> dict[str, Any]:
    cells: dict[str, Any] = {}
    for column, value in zip(columns, values):
        if column == "REMARKS" and cells.get(column) is not None:
            if value is not None and str(value).strip():
                cells[column] = f"{cells[column]}\n{value}"
        else:
            cells[column] = value
    return cells


def _capacity(row: _Row) -> float | None:
    sizes = [parse_capacity_tb(text) for column in ("TOTAL DISK SPACE", "SPACE")
             if (text := row.text(column)) is not None]
    if len(set(sizes)) > 1:
        raise ValueError("conflicting TOTAL DISK SPACE and SPACE capacities")
    return sizes[0] if sizes else None


def _receipt(row: _Row) -> tuple[bool, date | None]:
    value = row.cells.get("T6 IN")
    if value is None or not str(value).strip():
        return False, None
    if str(value).strip().upper() in {"YES", "Y", "IN", "T6 IN", "TRUE", "✓"}:
        return True, None
    return True, _date(value)


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
                    _Row(sheet.title, number, project or sheet.title, project_row, _cells(columns, values))
                )
    workbook.close()
    return [replace(row, workbook=str(path.resolve())) for row in rows]


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
                row = _Row(sheet.title, number, project, project_row, _cells(columns, values), released_on)
                if "IN/OUT" in columns:
                    movements.append(row)
                elif row.text(_LABEL) and not row.text(_SERIAL_COLUMN) and row.text("QUANTITY"):
                    project, project_row = row.text(_LABEL) or project, number  # a group heading
                elif _filled(values):
                    drive_rows.append(row)
    workbook.close()
    return (
        [replace(row, workbook=str(path.resolve())) for row in drive_rows],
        [replace(row, workbook=str(path.resolve())) for row in movements],
    )


# Importing rows that each describe one drive


def _import_drive_rows(
    rows: list[_Row], facts: Callable[[_Row], _Facts], store: DriveStore, review: list[ReviewItem]
) -> int:
    sharing: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        serial = _serial(row)
        if serial:
            sharing[serial].append(row.place)
    pairs = _old_new_pairs(rows)
    unresolved: set[tuple[str, int]] = set()
    imported = 0
    for row in rows:
        if row.text(_SERIAL_COLUMN) == "TOTAL" and row.text("QUANTITY"):
            review.append(row.review("summary row", row.evidence()))
            continue
        serial, label = _serial(row), row.text(_LABEL)
        doubts: list[str] = []
        if label is None:
            review.append(row.review("no label"))
            doubts.append("no label")
        raw = row.text(_SERIAL_COLUMN)
        noted = raw is not None and not _SERIAL.fullmatch(raw.upper())
        shared = bool(serial and len(sharing[serial]) > 1)
        if noted:
            review.append(row.review("note in serial cell", raw or ""))
            doubts.append(raw or "note in serial cell")
        if shared:
            others = [place for place in sharing[serial or ""] if place != row.place]
            detail = f"{serial} is also on {', '.join(others)}"
            review.append(row.review("shared serial", detail))
            doubts.append(detail)
        if serial is None:
            review.append(row.review("blank serial", "no accepted serial recorded"))
            doubts.append("no accepted serial recorded")
        if id(row) in pairs:
            detail = f"{label} pairs with {pairs[id(row)]}"
            review.append(row.review("old and new labels", detail))
            doubts.append(detail)
        try:
            drive, assignment, events = facts(row)
            if noted or shared:
                drive = replace(drive, serial=None, suspected_serial=serial)
            if doubts:
                drive = replace(drive, needs_review=True, review_note="; ".join(doubts))
        except ValueError as error:
            review.append(row.review("unreadable", f"{error}\n{row.evidence()}"))
            continue
        _write(store, row, drive, assignment, events, review)
        imported += 1
        if assignment.project.unresolved and (row.sheet, row.project_row) not in unresolved:
            unresolved.add((row.sheet, row.project_row))
            review.append(ReviewItem(row.sheet, row.project_row, "unresolved project", row.project, row.workbook))
    return imported


def _serial(row: _Row) -> str | None:
    text = row.text(_SERIAL_COLUMN)
    if not text:
        return None
    normalized = normalize_serial(text)
    # The serial prefix is only a candidate when the cell also contains notes.
    found = re.match(r"[A-Z0-9-]+", normalized)
    return found.group(0) if found else None


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
    receipt, receipt_on = _receipt(row)
    received = _date(row.cells.get("DATE RECEIVED")) or receipt_on
    released = _date(row.cells.get("DATE RELEASED"))
    remarks = row.text("REMARKS")
    recipient = (row.text("PERSON IN CHARGE") or remarks) if released else None
    if recipient == remarks:
        remarks = None
    project = _project(row.project, lookup)
    drive = Drive(
        serial=_serial(row),
        brand=row.text("BRAND NAME"),
        capacity_tb=_capacity(row),
        label=label,
        description=row.text("HD DESCRIPTION"),
        remarks="\n".join(filter(None, (row.text("REMARKS"), row.text(_SERIAL_COLUMN)))) or None,
        format=row.text("FORMAT"),
    )
    assignment = Assignment(
        label=label, project=project, role=_role(label or ""), start=received, remarks=remarks
    )
    events = []
    if received or receipt:
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
    remarks = row.text("REMARKS")
    project = _project(row.project, lookup)
    drive = Drive(
        serial=_serial(row),
        capacity_tb=_capacity(row),
        label=label,
        description=row.text("HD DESCRIPTION"),
        remarks="\n".join(filter(None, (row.text("REMARKS"), row.text(_SERIAL_COLUMN)))) or None,
        format=row.text("FORMAT"),
    )
    assignment = Assignment(
        label=label, project=project, role=_role(label or ""), start=None, remarks=remarks
    )
    events = []
    receipt, receipt_on = _receipt(row)
    received = _date(row.cells.get("DATE RECEIVED")) or receipt_on
    if receipt or received:
        events.append(CustodyEvent(kind=CustodyKind.RECEIVED, on=received, party=None,
                                  project=project, logged_by=logged_by))
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
    store: DriveStore, row: _Row, drive: Drive, assignment: Assignment,
    events: list[CustodyEvent], review: list[ReviewItem]
) -> None:
    """A row receipt makes reruns independent of mutable facts or serial edits.

    Events from distinct rows are preserved even when their facts are identical.
    Matching a clean serial does not overwrite existing physical facts: additional
    observations stay traceable in the local review report instead.
    """
    held = store.find_by_serial(drive.serial) if drive.serial else None
    if held is not None and any(
        getattr(drive, field) is not None and getattr(drive, field) != getattr(held, field)
        for field in ("label", "description", "brand", "model", "capacity_tb", "format", "remarks")
    ):
        review.append(row.review("additional drive facts", row.evidence()))
    if store.imported_drive(row.import_key) is not None:
        return
    store.import_row(row.import_key, drive, assignment, events)


# Importing the monthly log: one row per movement


def _import_movements(
    rows: list[_Row], store: DriveStore, lookup: Lookup, logged_by: Ref,
    review: list[ReviewItem], uncertain_serials: set[str]
) -> int:
    imported = 0
    unresolved: set[str] = set()
    for row in rows:
        tokens = _SERIAL_IN_REMARKS.findall(row.text("REMARKS") or "")
        if any(not _SERIAL.fullmatch(token.upper()) for token in tokens):
            review.append(row.review("ambiguous serial", row.evidence()))
            continue
        serials = {normalize_serial(token) for token in tokens}
        if len(serials) != 1:
            reason = "movement with no serial" if not serials else "ambiguous serial"
            review.append(row.review(reason, row.evidence()))
            continue
        serial = next(iter(serials))
        if serial in uncertain_serials or store.has_suspected_serial(serial):
            review.append(row.review("ambiguous serial", row.evidence()))
            continue
        label = row.text(_LABEL)
        if label is None:
            review.append(row.review("no label"))
        try:
            drive, assignment, event = _movement_facts(row, serial, label, lookup, logged_by)
        except ValueError as error:
            review.append(row.review("unreadable", f"{error}\n{row.evidence()}"))
            continue
        _write(store, row, drive, assignment, [event], review)
        imported += 1
        name = assignment.project.text
        if name is not None and name not in unresolved:
            unresolved.add(name)
            review.append(row.review("unresolved project", name))
    return imported


def _movement_facts(
    row: _Row, serial: str, label: str | None, lookup: Lookup, logged_by: Ref
) -> tuple[Drive, Assignment, CustodyEvent]:
    direction = (row.text("IN/OUT") or "").upper()
    if direction == "T6 IN":
        direction = "IN"
    if direction not in {"IN", "OUT"}:
        raise ValueError(f"cannot read IN/OUT {row.text('IN/OUT')!r}")
    on = _date(row.cells.get("DATE"))
    name = row.text("RECEIVED BY")
    project = _project(row.text("PROJECT NAME") or row.sheet, lookup)
    try:
        capacity = parse_capacity_tb(row.text("HD DESCRIPTION") or "")
    except ValueError:
        capacity = None  # the description is free text and often has no size
    assignment = Assignment(label=label, project=project, role=_role(label or ""), start=None)
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
    remarks = row.text("REMARKS")
    reason = "\n".join(filter(None, (event.reason, remarks))) or None
    event = replace(event, reason=reason)
    return Drive(serial=serial, label=label, description=row.text("HD DESCRIPTION"),
                 remarks=remarks, capacity_tb=capacity, needs_review=label is None,
                 review_note="no label" if label is None else None), assignment, event


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
