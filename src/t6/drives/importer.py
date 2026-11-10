"""One-off import of the legacy drive workbooks into a `DriveStore`.

Needs the `drives` extra (`pip install 't6[drives]'`) for the spreadsheet
reader. The importer writes only through `DriveStore` and never guesses: a row
it cannot read with confidence is skipped and listed in the result.

The per-project workbook has one tab per Project. A tab holds an optional
`PROJECT NAME: ...` row, a header row starting with `HARD DRIVE LABEL`, then
one row per drive. A tab can repeat that pattern.
"""

from __future__ import annotations

import re
from collections import Counter
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

__all__ = ["ImportResult", "SkippedRow", "import_per_project_workbook"]

_PROJECT_NAME = re.compile(r"PROJECT NAME\s*:\s*(.*)", re.IGNORECASE | re.DOTALL)
_HEADER = "HARD DRIVE LABEL"
_SERIAL = re.compile(r"[A-Z0-9-]+")
_DATE_FORMATS = ("%B %d %Y", "%b %d %Y")
_ROLE_WORDS = {
    Role.MASTER: r"MASTER",
    Role.BACKUP: r"BACK ?UP|B\. ?UP|\bBU ?\d*\b",
    Role.OFFLINE: r"OFFLINE",
    Role.SWING: r"SWING",
    Role.ARCHIVE: r"ARCHIVE",
}


@dataclass(frozen=True, slots=True)
class SkippedRow:
    """A workbook row that was not imported, and why."""

    sheet: str
    row: int
    reason: str


@dataclass(frozen=True, slots=True)
class ImportResult:
    """What one import run did: how many rows it read cleanly, and what it skipped."""

    imported: int
    skipped: tuple[SkippedRow, ...]


@dataclass(frozen=True, slots=True)
class _Row:
    sheet: str
    number: int
    project: str
    cells: dict[str, Any]

    def text(self, column: str) -> str | None:
        value = self.cells.get(column)
        if value is None or not str(value).strip():
            return None
        return str(value).strip()


def import_per_project_workbook(
    path: str | Path, store: DriveStore, *, lookup: Lookup, logged_by: Ref
) -> ImportResult:
    """Import every clean row of the per-project workbook into `store`.

    A clean row becomes a Drive with an Assignment for its tab's Project, a
    received event when it has a received date and a released event when it
    has a release date. The recipient of a release is the row's person in
    charge or, failing that, its remarks, kept as text. `logged_by` is who the
    imported events are logged under.

    A row is skipped, and listed in the result, when it has no label, a blank
    serial, anything but one serial in the serial cell, a serial that is on
    more than one row, or a capacity or date that cannot be read. Running the
    import again adds nothing that is already in the store.
    """
    rows = _read_rows(Path(path))
    seen = Counter(_serial(row) for row in rows)
    imported = 0
    skipped = []
    for row in rows:
        reason = _doubt(row, seen)
        if reason is None:
            try:
                _import_row(row, store, lookup, logged_by)
                imported += 1
                continue
            except ValueError as error:
                reason = str(error)
        skipped.append(SkippedRow(row.sheet, row.number, reason))
    return ImportResult(imported, tuple(skipped))


def _read_rows(path: Path) -> list[_Row]:
    rows = []
    workbook = load_workbook(path, read_only=True, data_only=True)
    for sheet in workbook.worksheets:
        project: str | None = None
        columns: list[str] | None = None
        for number, values in enumerate(sheet.iter_rows(values_only=True), start=1):
            first = str(values[0] or "").strip() if values else ""
            named = _PROJECT_NAME.search(first)
            if named:
                project, columns = named.group(1).strip() or None, None
            elif first.upper() == _HEADER:
                columns = [str(value or "").strip().upper() for value in values]
            elif columns is not None and any(value is not None and str(value).strip() for value in values):
                rows.append(_Row(sheet.title, number, project or sheet.title, dict(zip(columns, values))))
    workbook.close()
    return rows


def _serial(row: _Row) -> str | None:
    text = row.text("SERIAL NUMBER")
    return text.upper() if text else None


def _doubt(row: _Row, seen: Counter[str | None]) -> str | None:
    serial = _serial(row)
    if row.text(_HEADER) is None:
        return "no label"
    if serial is None:
        return "blank serial"
    if not _SERIAL.fullmatch(serial):
        return "the serial cell holds more than a serial"
    if seen[serial] > 1:
        return "the serial is on more than one row"
    return None


def _import_row(row: _Row, store: DriveStore, lookup: Lookup, logged_by: Ref) -> None:
    serial, label = _serial(row), row.text(_HEADER)
    assert serial is not None and label is not None
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

    if store.get_drive(serial) is None:
        store.add_drive(drive)
    if assignment not in store.assignment_history(serial):
        store.add_assignment(serial, assignment)
    logged = store.list_events(serial)
    for event in events:
        if event not in logged:
            store.append_event(serial, event)


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
