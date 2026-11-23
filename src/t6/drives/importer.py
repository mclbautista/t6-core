"""Import legacy drive workbooks and reload the per-project sheet into a `DriveStore`.

Needs the `drives` extra (`pip install 't6[drives]'`) for the spreadsheet
reader. The importer writes only through `DriveStore` and never guesses: every
row it cannot read with confidence is listed in the result's review report.

The per-project workbook has one tab per Project. A tab holds an optional
`PROJECT NAME: ...` or `PROJECT: ...` row, a header row starting with `HARD DRIVE LABEL`, then
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

from t6.drives import (
    Assignment,
    CustodyEvent,
    CustodyKind,
    Drive,
    DriveStore,
    Party,
    PartyKind,
    Ref,
    SourceRow,
    Role,
    parse_capacity_tb,
    normalize_serial,
)
from t6.lookup import Index, Lookup
from t6.drives.sheets import SheetSource, WorkbookSheetSource

__all__ = [
    "ImportResult",
    "ReviewItem",
    "import_log_sheet_workbook",
    "import_per_project_workbook",
]

_PROJECT_NAME = re.compile(r"\bPROJECT(?: NAME)?\s*:\s*(.*)", re.IGNORECASE)
_TAB_DATE = re.compile(r"DATE\s*:\s*(.+)", re.IGNORECASE)
_LABEL = "HARD DRIVE LABEL"
_SERIAL_COLUMN = "SERIAL NUMBER"
_SERIAL = re.compile(r"[A-Z0-9-]+")
_SERIAL_IN_REMARKS = re.compile(r"(?<!\w)S/N\s*:?\s*", re.IGNORECASE)
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
    raw_values: tuple[Any, ...] = ()
    previously_imported: bool = False

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
# Facts readers append each unreadable capacity or date to the `problems` list
# and leave that fact blank; they raise ValueError only when the row cannot be
# imported at all.
_FactsReader = Callable[[_Row, list[str]], _Facts]


def import_per_project_workbook(
    path: str | Path | SheetSource, store: DriveStore, *, lookup: Lookup, logged_by: Ref,
    reload: bool = False, serial_ids: dict[str, str] | None = None,
    project_mapping: dict[str, str] | None = None,
    timeline: dict[str, list[CustodyEvent]] | None = None,
) -> ImportResult:
    """Import per-project rows, preserving uncertain facts without guessing identity.

    Clean accepted serials may match existing Drives. Missing, noted or shared
    serials yield distinct review-flagged records, with candidates explicitly
    suspected. Unreadable rows remain traceable review items. Unchanged reruns
    use row receipts and append no duplicate history. See docs/drive-identity.md.

    Reload mode accepts normalized serials shared across tabs, keeps each row's
    Assignment to its approved Project (or tab name as text), and orders all custody events per Drive.
    Its caller owns the replacement transaction and the persistent serial-ID map.
    """
    source = WorkbookSheetSource(path) if isinstance(path, (str, Path)) else path
    rows, review = _read_per_project(source, reload=reload)
    if reload:
        imported = _reload_drive_rows(rows, store, logged_by, review, serial_ids or {}, project_mapping or {}, timeline)
        _preserve_exceptions(store, review, rows)
        return ImportResult(imported, tuple(review))
    rows = _mark_receipts(store, rows)
    imported = _import_drive_rows(
        rows,
        lambda row, problems: _per_project_facts(row, lookup, logged_by, problems),
        store,
        review,
    )
    _preserve_exceptions(store, review, rows)
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
    drive_rows, movements, unparsed = _read_log_sheet(Path(path))
    drive_rows = _mark_receipts(store, drive_rows)
    movements = _mark_receipts(store, movements)
    review: list[ReviewItem] = []
    imported = _import_drive_rows(
        drive_rows, lambda row, problems: _inventory_facts(row, lookup, logged_by, problems), store, review
    )
    inventory_serials = [_serial(row) for row in drive_rows]
    uncertain_serials = {
        serial for row, serial in zip(drive_rows, inventory_serials)
        if serial and (inventory_serials.count(serial) > 1
                       or not _SERIAL.fullmatch((row.text(_SERIAL_COLUMN) or "").upper()))
    }
    imported += _import_movements(movements, store, lookup, logged_by, review, uncertain_serials)
    _preserve_exceptions(store, [*unparsed, *review], [*drive_rows, *movements])
    return ImportResult(imported, tuple(review))


def _mark_receipts(store: DriveStore, rows: list[_Row]) -> list[_Row]:
    return [replace(row, previously_imported=store.imported_drive(row.import_key) is not None) for row in rows]


def _preserve_exceptions(store: DriveStore, review: list[ReviewItem], rows: list[_Row]) -> None:
    evidence = {(row.workbook, row.sheet, row.number): row.evidence() + "\n" + json.dumps(row.raw_values, default=str, ensure_ascii=False) for row in rows}
    existing = {(row.workbook, row.sheet, row.number) for row in rows if row.previously_imported}
    blocks: dict[tuple[str, str, int], list[bool]] = {}
    for row in rows:
        blocks.setdefault((row.workbook, row.sheet, row.project_row), []).append(row.previously_imported)
    completed_blocks = {key for key, receipts in blocks.items() if all(receipts)}
    stored = []
    for item in review:
        key = (item.workbook, item.sheet, item.row)
        if key in existing or (item.reason == "unresolved project" and key in completed_blocks):
            continue
        cells = evidence.get((item.workbook, item.sheet, item.row))
        detail = item.detail
        if cells and cells not in detail:
            detail = f'{detail}\n{cells}' if detail else cells
        stored.append(replace(item, detail=detail))
    store.record_exceptions(stored)


# Reading the workbooks


def _filled(values: tuple[Any, ...]) -> bool:
    return any(value is not None and str(value).strip() for value in values)


def _columns(values: tuple[Any, ...]) -> list[str]:
    names = [" ".join(str(value or "").upper().split()) for value in values]
    return [_SERIAL_COLUMN if name in {"SERIAL #", "SERIAL NO", "SERIAL NO."} else _LABEL if name == "LABEL" else name for name in names]


def _cells(columns: list[str], values: tuple[Any, ...]) -> dict[str, Any]:
    cells: dict[str, Any] = {}
    for column, value in zip(columns, values):
        if column == "REMARKS" and cells.get(column) is not None:
            if value is not None and str(value).strip():
                cells[column] = f"{cells[column]}\n{value}"
        else:
            cells[column] = value
    return cells


def _capacity(row: _Row, problems: list[str]) -> float | None:
    """The capacity, or None with the reason in `problems` when unreadable or conflicting."""
    try:
        sizes = [parse_capacity_tb(text) for column in ("TOTAL DISK SPACE", "SPACE")
                 if (text := row.text(column)) is not None]
    except ValueError as error:
        problems.append(str(error))
        return None
    if len(set(sizes)) > 1:
        problems.append("conflicting TOTAL DISK SPACE and SPACE capacities")
        return None
    return sizes[0] if sizes else None


def _receipt(row: _Row, problems: list[str]) -> tuple[bool, date | None]:
    value = row.cells.get("T6 IN")
    if value is None or not str(value).strip():
        return False, None
    if str(value).strip().upper() in {"YES", "Y", "IN", "T6 IN", "TRUE", "✓"}:
        return True, None
    return True, _cell_date(value, problems)


def _read_per_project(source: SheetSource, *, reload: bool = False) -> tuple[list[_Row], list[ReviewItem]]:
    rows = []
    review = []
    for title, sheet_rows in source.tabs().items():
        if reload and title.strip().upper() == "PROJECT TEMPLATE":
            continue
        project: str | None = None
        project_row = 1
        columns: list[str] | None = None
        for number, values in enumerate(sheet_rows, start=1):
            first = str(values[0] or "").strip() if values else ""
            first_lines = first.splitlines()
            named = _PROJECT_NAME.search(first_lines[0]) if first_lines else None
            if named:
                project, project_row, columns = named.group(1).strip() or None, number, None
            elif first.upper() == _LABEL:
                columns = _columns(values)
                if project is None:
                    project_row = number
            elif columns is not None and _filled(values):
                rows.append(
                    _Row(title, number, (title if reload else project or title), project_row, _cells(columns, values), raw_values=values)
                )
            if columns is None and not reload:
                unparsed = ([*first_lines[1:], *values[1:]] if named else values)
                for value in unparsed:
                    if value is not None:
                        for line in str(value).splitlines():
                            if line.strip():
                                review.append(ReviewItem(
                                    title, number, "pre-header line", line.strip(), source.workbook
                                ))
    return [replace(row, workbook=source.workbook) for row in rows], review


def _read_log_sheet(path: Path | SheetSource) -> tuple[list[_Row], list[_Row], list[ReviewItem]]:
    """The inventory rows and the movement rows of the log sheet workbook."""
    drive_rows, movements = [], []
    unparsed: list[ReviewItem] = []
    source = WorkbookSheetSource(path) if isinstance(path, Path) else path
    for sheet_name, sheet_rows in source.tabs().items():
        project, project_row = sheet_name, 1
        columns: list[str] | None = None
        released_on: date | None = None
        for number, values in enumerate(sheet_rows, start=1):
            texts = [str(value).strip() for value in values if value is not None]
            dated = next((m for m in map(_TAB_DATE.fullmatch, texts) if m), None)
            if dated:
                try:
                    released_on = _date(dated.group(1))
                except ValueError:
                    unparsed.append(ReviewItem(sheet_name, number, "unreadable", json.dumps(values, default=str, ensure_ascii=False), source.workbook))
            elif any(text.upper() in {_LABEL, "LABEL"} for text in texts):
                columns = _columns(values)
            elif columns is not None and _filled(values):
                row = _Row(sheet_name, number, project, project_row, _cells(columns, values), released_on, raw_values=values)
                if "IN/OUT" in columns:
                    movements.append(row)
                elif row.text(_LABEL) and not row.text(_SERIAL_COLUMN) and row.text("QUANTITY"):
                    project, project_row = row.text(_LABEL) or project, number  # a group heading
                elif _filled(values):
                    drive_rows.append(row)
            elif columns is None and _filled(values):
                unparsed.append(ReviewItem(sheet_name, number, "unreadable", json.dumps(values, default=str, ensure_ascii=False), source.workbook))
    return (
        [replace(row, workbook=source.workbook) for row in drive_rows],
        [replace(row, workbook=source.workbook) for row in movements],
        unparsed,
    )


# Importing rows that each describe one drive


def _import_drive_rows(
    rows: list[_Row], facts: _FactsReader, store: DriveStore, review: list[ReviewItem]
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
        problems: list[str] = []
        try:
            drive, assignment, events = facts(row, problems)
            doubts += problems
            if noted or shared:
                drive = replace(drive, serial=None, suspected_serial=serial)
            if doubts:
                drive = replace(drive, needs_review=True, review_note="; ".join(doubts))
        except ValueError as error:
            review.append(_unreadable(row, [*problems, str(error)]))
            continue
        if problems:
            review.append(_unreadable(row, problems))
        assignment = _write(store, row, drive, assignment, events, review)
        imported += 1
        if _held_project(assignment).unresolved and (row.sheet, row.project_row) not in unresolved:
            unresolved.add((row.sheet, row.project_row))
            review.append(ReviewItem(row.sheet, row.project_row, "unresolved project", row.project, row.workbook))
    return imported


def _reload_drive_rows(rows: list[_Row], store: DriveStore, logged_by: Ref, review: list[ReviewItem],
                       serial_ids: dict[str, str], project_mapping: dict[str, str],
                       pending: dict[str, list[CustodyEvent]] | None = None) -> int:
    from t6.drives.mapping import project_ref
    imported = 0
    timeline: dict[str, list[CustodyEvent]] = pending if pending is not None else defaultdict(list)
    for row in rows:
        problems: list[str] = []
        try:
            drive, assignment, events = _per_project_facts(row, None, logged_by, problems)
            if problems:
                raise ValueError("; ".join(problems))
        except ValueError as error:
            review.append(_unreadable(row, [str(error)]))
            continue
        if drive.serial in serial_ids:
            drive = replace(drive, id=serial_ids[drive.serial])
        project = project_ref(row.project, project_mapping)
        assignment = replace(assignment, project=project)
        events = [replace(event, project=project) for event in events]
        if not events:
            events.append(CustodyEvent(kind=CustodyKind.RECEIVED, on=None, party=None,
                                       project=project, logged_by=logged_by))
        if row.sheet.strip().upper() == "DEAD DRIVES" or "DEAD DRIVE" in (row.text("REMARKS") or "").upper():
            events.append(CustodyEvent(kind=CustodyKind.RETIRED, on=None, party=None,
                                       project=project, logged_by=logged_by, reason="DEAD DRIVE"))
        store.import_row(row.import_key, drive, assignment, [],
                         source=SourceRow(row.workbook, row.sheet, row.number), deduplicate_assignment=False)
        held = store.imported_drive(row.import_key)
        assert held is not None
        if "T6" in (drive.label or "").upper():
            store.set_facts(held, {"ownership_known": True})
        timeline[held].extend(events)
        imported += 1
    if pending is None:
        for drive_id, events in timeline.items():
            for event in sorted(events, key=lambda e: (e.kind is CustodyKind.RETIRED, e.on or date.min)):
                store.append_event(drive_id, event)
    return imported


def _unreadable(row: _Row, problems: list[str]) -> ReviewItem:
    return row.review("unreadable", f"{'; '.join(problems)}\n{row.evidence()}")


def _serial(row: _Row) -> str | None:
    text = row.text(_SERIAL_COLUMN)
    if not text:
        return None
    # Preserve the complete candidate, including internal spaces/punctuation.
    # The caller marks non-clean cells as suspected rather than accepting them.
    return normalize_serial(text) or None


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


def _per_project_facts(row: _Row, lookup: Lookup | None, logged_by: Ref, problems: list[str]) -> _Facts:
    label = row.text(_LABEL)
    receipt, receipt_on = _receipt(row, problems)
    received = _cell_date(row.cells.get("DATE RECEIVED"), problems) or receipt_on
    released = _cell_date(row.cells.get("DATE RELEASED"), problems)
    # A recorded but unreadable date still records the event, undated.
    receipt = receipt or row.text("DATE RECEIVED") is not None
    was_released = row.text("DATE RELEASED") is not None
    remarks = row.text("REMARKS")
    recipient = ((remarks or row.text("PERSON IN CHARGE")) if lookup is None else
                 (row.text("PERSON IN CHARGE") or remarks)) if was_released else None
    if recipient == remarks:
        remarks = None
    project = _project(row.project, lookup) if lookup is not None else Ref(text=row.project)
    drive = Drive(
        serial=_serial(row),
        brand=row.text("BRAND NAME"),
        capacity_tb=_capacity(row, problems),
        label=label,
        description=row.text("HD DESCRIPTION"),
        remarks=row.text("REMARKS"),
        format=row.text("FORMAT"),
        ownership_known=False,
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
    if was_released:
        party = Party(PartyKind.CONTACT, Ref(text=recipient)) if recipient else None
        events.append(
            CustodyEvent(
                kind=CustodyKind.RELEASED, on=released, party=party, project=project, logged_by=logged_by
            )
        )
    return drive, assignment, events


def _inventory_facts(row: _Row, lookup: Lookup, logged_by: Ref, problems: list[str]) -> _Facts:
    label = row.text(_LABEL)
    remarks = row.text("REMARKS")
    project = _project(row.project, lookup)
    drive = Drive(
        serial=_serial(row),
        capacity_tb=_capacity(row, problems),
        label=label,
        description=row.text("HD DESCRIPTION"),
        remarks=row.text("REMARKS"),
        format=row.text("FORMAT"),
        ownership_known=False,
    )
    assignment = Assignment(
        label=label, project=project, role=_role(label or ""), start=None, remarks=remarks
    )
    events = []
    receipt, receipt_on = _receipt(row, problems)
    received = _cell_date(row.cells.get("DATE RECEIVED"), problems) or receipt_on
    receipt = receipt or row.text("DATE RECEIVED") is not None
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


def _held_project(assignment: Assignment) -> Ref:
    """The Project of a workbook Assignment; a workbook row always names one."""
    assert assignment.project is not None
    return assignment.project


def _write(
    store: DriveStore, row: _Row, drive: Drive, assignment: Assignment,
    events: list[CustodyEvent], review: list[ReviewItem]
) -> Assignment:
    """A row receipt makes reruns independent of mutable facts or serial edits.

    Events from distinct rows are preserved even when their facts are identical.
    Matching a clean serial does not overwrite existing physical facts: additional
    observations stay traceable in the local review report instead.
    """
    project = store.resolve_project(_held_project(assignment), row.text("PROJECT NAME") or row.project)
    assignment = replace(assignment, project=project)
    events = [replace(event, project=project) for event in events]
    held = store.find_by_serial(drive.serial) if drive.serial else None
    if held is not None and any(
        getattr(drive, field) is not None and getattr(drive, field) != getattr(held, field)
        for field in ("label", "description", "brand", "model", "capacity_tb", "format", "remarks")
    ):
        review.append(row.review("additional drive facts", row.evidence()))
    if not row.previously_imported:
        pending = [item for item in review if (item.workbook, item.sheet, item.row) == (row.workbook, row.sheet, row.number)]
        if _held_project(assignment).unresolved:
            if "IN/OUT" in row.cells:
                pending.append(row.review("unresolved project", _held_project(assignment).text or ""))
            else:
                pending.append(ReviewItem(row.sheet, row.project_row, "unresolved project", row.project, row.workbook))
        _preserve_exceptions(store, pending, [row])
        store.import_row(row.import_key, drive, assignment, events, source=SourceRow(row.workbook, row.sheet, row.number))
    return assignment


# Importing the monthly log: one row per movement


def _import_movements(
    rows: list[_Row], store: DriveStore, lookup: Lookup, logged_by: Ref,
    review: list[ReviewItem], uncertain_serials: set[str]
) -> int:
    imported = 0
    unresolved: set[str] = set()
    for row in rows:
        remarks = row.text("REMARKS") or ""
        markers = list(_SERIAL_IN_REMARKS.finditer(remarks))
        if len(markers) != 1:
            reason = "movement with no serial" if not markers else "ambiguous serial"
            review.append(row.review(reason, row.evidence()))
            continue
        # The whole suffix must be clean: whitespace or punctuation may be part
        # of an uncertain serial, never evidence for accepting just its prefix.
        token = remarks[markers[0].end():].strip()
        if not _SERIAL.fullmatch(token.upper()):
            review.append(row.review("ambiguous serial", row.evidence()))
            continue
        serial = normalize_serial(token)
        if serial in uncertain_serials or store.has_suspected_serial(serial):
            review.append(row.review("ambiguous serial", row.evidence()))
            continue
        label = row.text(_LABEL)
        if label is None:
            review.append(row.review("no label"))
        problems: list[str] = []
        try:
            drive, assignment, event = _movement_facts(row, serial, label, lookup, logged_by, problems)
        except ValueError as error:
            review.append(_unreadable(row, [*problems, str(error)]))
            continue
        if problems:
            review.append(_unreadable(row, problems))
        assignment = _write(store, row, drive, assignment, [event], review)
        imported += 1
        name = _held_project(assignment).text
        if name is not None and name not in unresolved:
            unresolved.add(name)
            review.append(row.review("unresolved project", name))
    return imported


def _movement_facts(
    row: _Row, serial: str, label: str | None, lookup: Lookup, logged_by: Ref, problems: list[str]
) -> tuple[Drive, Assignment, CustodyEvent]:
    direction = (row.text("IN/OUT") or "").upper()
    if direction == "T6 IN":
        direction = "IN"
    if direction not in {"IN", "OUT"}:
        raise ValueError(f"cannot read IN/OUT {row.text('IN/OUT')!r}")
    on = _cell_date(row.cells.get("DATE"), problems)
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
    physical_remarks = _SERIAL_IN_REMARKS.split(remarks or "", maxsplit=1)[0].strip() or None
    doubts = (["no label"] if label is None else []) + problems
    return Drive(serial=serial, label=label, description=row.text("HD DESCRIPTION"),
                 remarks=physical_remarks, capacity_tb=capacity, needs_review=bool(doubts),
                 ownership_known=False, review_note="; ".join(doubts) or None), assignment, event


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


def _cell_date(value: Any, problems: list[str]) -> date | None:
    """A date cell, or None with the reason in `problems` when it cannot be read."""
    try:
        return _date(value)
    except ValueError as error:
        problems.append(str(error))
        return None


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


def reload_tracker_inventory(source: SheetSource, store: DriveStore, logged_by: Ref,
                             mapping: dict[str, str], serial_ids: dict[str, str],
                             timeline: dict[str, list[CustodyEvent]]) -> int:
    """Tracker inventory contributes missing serials and review/retirement hints."""
    from t6.drives.mapping import project_ref
    rows, _, review = _read_log_sheet(source)
    read = 0
    for row in rows:
        serial = _serial(row)
        if not serial:
            continue  # group headings and totals do not identify physical Drives
        read += 1
        held = store.find_by_serial(serial)
        project = project_ref(row.project, mapping)
        problems: list[str] = []
        if held is None:
            drive, assignment, events = _inventory_facts(row, Lookup(Index()), logged_by, problems)
            if problems:
                review.append(_unreadable(row, problems))
                continue
            drive = replace(drive, id=serial_ids.get(serial, drive.id), ownership_known='T6' in (drive.label or '').upper())
            assignment = replace(assignment, project=project)
            store.import_row(row.import_key, drive, assignment, [], source=SourceRow(row.workbook, row.sheet, row.number))
            held = store.find_by_serial(serial)
            assert held is not None
            timeline.setdefault(held.id, []).extend(replace(event, project=project) for event in events)
            if not events:
                timeline[held.id].append(CustodyEvent(kind=CustodyKind.RECEIVED, on=None, party=None,
                                                    project=project, logged_by=logged_by))
        remarks = row.text('REMARKS') or ''
        if 'DEFECTIVE' in remarks.upper():
            note = '; '.join(dict.fromkeys(filter(None, (held.review_note, remarks))))
            store.set_review(held.id, needs_review=True, note=note)
        if 'DEAD DRIVE' in remarks.upper() and not any(e.kind is CustodyKind.RETIRED for e in timeline.get(held.id, [])):
            timeline.setdefault(held.id, []).append(CustodyEvent(kind=CustodyKind.RETIRED, on=None, party=None,
                                                               project=project, logged_by=logged_by, reason='DEAD DRIVE'))
    store.record_exceptions(review)
    return read
