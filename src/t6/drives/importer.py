"""One-off import of the legacy drive workbooks into a `DriveStore`.

Needs the `drives` extra (`pip install 't6[drives]'`) for the spreadsheet
reader. The importer writes only through `DriveStore` and never guesses: every
row it cannot read with confidence is listed in the result's review report.

The per-project workbook has one tab per Project. A tab holds an optional
`PROJECT NAME: ...` row, a header row starting with `HARD DRIVE LABEL`, then
one row per drive. A tab can repeat that pattern.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
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

__all__ = ["ImportResult", "ReviewItem", "import_per_project_workbook"]

_PROJECT_NAME = re.compile(r"PROJECT NAME\s*:\s*(.*)", re.IGNORECASE | re.DOTALL)
_HEADER = "HARD DRIVE LABEL"
_SERIAL = re.compile(r"[A-Z0-9-]+")
_OLD_NEW = re.compile(r"\b(OLD|NEW)\b")
_DATE_FORMATS = ("%B %d %Y", "%b %d %Y")
_ROLE_WORDS = {
    Role.MASTER: r"MASTER",
    Role.BACKUP: r"BACK ?UP|B\. ?UP|\bBU ?\d*\b",
    Role.OFFLINE: r"OFFLINE",
    Role.SWING: r"SWING",
    Role.ARCHIVE: r"ARCHIVE",
}


@dataclass(frozen=True, slots=True)
class ReviewItem:
    """One thing in the workbook a person has to look at, with the row it came from.

    `reason` is one of: `no label`, `note in serial cell`, `shared serial` and
    `unreadable` (the row was not imported); `blank serial` (imported as a
    provisional Drive); `old and new labels` (imported with its label as
    written); `unresolved project` (its rows were imported with the Project
    name as text; `row` is where the Project's block starts).
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

    def text(self, column: str) -> str | None:
        value = self.cells.get(column)
        if value is None or not str(value).strip():
            return None
        return str(value).strip()


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
    rows = _read_rows(Path(path))
    sharing = defaultdict(list)
    for row in rows:
        sharing[_serial(row)].append(f"{row.sheet} row {row.number}")
    pairs = _old_new_pairs(rows)
    blank_seen: Counter[Assignment] = Counter()
    unresolved: set[tuple[str, int]] = set()
    imported = 0
    review = []

    def flag(row: _Row, reason: str, detail: str = "") -> None:
        review.append(ReviewItem(row.sheet, row.number, reason, detail))

    for row in rows:
        serial, label = _serial(row), row.text(_HEADER)
        if label is None:
            flag(row, "no label")
        elif serial is not None and not _SERIAL.fullmatch(serial):
            flag(row, "note in serial cell", row.text("SERIAL NUMBER") or "")
        elif serial is not None and len(sharing[serial]) > 1:
            others = [place for place in sharing[serial] if place != f"{row.sheet} row {row.number}"]
            flag(row, "shared serial", f"{serial} is also on {', '.join(others)}")
        else:
            try:
                project = _import_row(row, store, lookup, logged_by, blank_seen)
            except ValueError as error:
                flag(row, "unreadable", str(error))
                continue
            imported += 1
            if project.unresolved and (row.sheet, row.project_row) not in unresolved:
                unresolved.add((row.sheet, row.project_row))
                review.append(ReviewItem(row.sheet, row.project_row, "unresolved project", row.project))
            if serial is None:
                flag(row, "blank serial", f"{label} is recorded as a provisional Drive")
            if id(row) in pairs:
                flag(row, "old and new labels", f"{label} pairs with {pairs[id(row)]}")
    return ImportResult(imported, tuple(review))


def _read_rows(path: Path) -> list[_Row]:
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
            elif first.upper() == _HEADER:
                columns = [str(value or "").strip().upper() for value in values]
                if project is None:
                    project_row = number
            elif columns is not None and any(value is not None and str(value).strip() for value in values):
                rows.append(
                    _Row(sheet.title, number, project or sheet.title, project_row, dict(zip(columns, values)))
                )
    workbook.close()
    return rows


def _serial(row: _Row) -> str | None:
    text = row.text("SERIAL NUMBER")
    return text.upper() if text else None


def _old_new_pairs(rows: list[_Row]) -> dict[int, str]:
    """Rows whose label, apart from an OLD or NEW mark, is on another row of the Project.

    Maps `id(row)` to the other labels in its group.
    """
    groups = defaultdict(list)
    for row in rows:
        label = row.text(_HEADER)
        if label is not None:
            base = " ".join(re.sub(r"[^A-Z0-9 ]", " ", _OLD_NEW.sub(" ", label.upper())).split())
            groups[(row.sheet, row.project_row, base)].append((row, label))
    pairs = {}
    for group in groups.values():
        if len(group) > 1 and any(_OLD_NEW.search(label.upper()) for _, label in group):
            for row, label in group:
                pairs[id(row)] = ", ".join(other for mate, other in group if mate is not row)
    return pairs


def _import_row(
    row: _Row, store: DriveStore, lookup: Lookup, logged_by: Ref, blank_seen: Counter[Assignment]
) -> Ref:
    """Write one row to the store and return the Project it was filed under."""
    serial, label = _serial(row), row.text(_HEADER)
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
        serial=serial,
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

    if serial is None:
        # A provisional Drive has no serial to find it by on a second run, so
        # it is found by its Assignment. The nth identical row needs n Drives.
        blank_seen[assignment] += 1
        recorded = sum(
            1 for held, at in store.open_assignments(project) if held.no_serial and at == assignment
        )
        if recorded >= blank_seen[assignment]:
            return project
        key = store.add_drive(drive).key
    else:
        key = serial
        if store.get_drive(serial) is None:
            store.add_drive(drive)
    if assignment not in store.assignment_history(key):
        store.add_assignment(key, assignment)
    logged = store.list_events(key)
    for event in events:
        if event not in logged:
            store.append_event(key, event)
    return project


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
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = " ".join(re.sub(r"[.,]", " ", str(value)).split())
    for known in _DATE_FORMATS:
        try:
            return datetime.strptime(text, known).date()
        except ValueError:
            continue
    raise ValueError(f"cannot read the date {value!r}")
