"""The extractor: cell values plus merge geometry in, ScheduleSnapshot/v1 out. Pure; no I/O."""

from __future__ import annotations

from typing import Literal

from t6.schedule.contract import (
    Candidate,
    DateEvidence,
    DayState,
    Diagnostic,
    Evidence,
    Group,
    GroupException,
    NonTask,
    Provenance,
    ScheduleRequest,
    ScheduleSnapshot,
    UnsupportedLayout,
    YearEvidence,
)
from t6.schedule.coverage import group_status, read_coverage, unread_days, with_source_uncertainty
from t6.schedule.dates import DayCell, aligns, best_start_years, block_years, make_date, span_dates, weekday_name
from t6.schedule.grid import Grid, a1, merge_range
from t6.schedule.layout import DAY_COLUMNS, LEFT_COLUMNS, MonthBlock, day_pairs, find_month_blocks
from t6.schedule.revision import source_ids, source_revision
from t6.schedule.text import clean_text, is_availability, is_break, tentative_marker


Method = Literal["supplied", "inferred_weekday_alignment"]


def extract_schedule(request: ScheduleRequest) -> ScheduleSnapshot | UnsupportedLayout:
    grid = Grid.from_request(request)
    scope = request.source_scope
    if grid.row_count == 0:
        return _unsupported(request, "empty_tab", "The tab has no cells.")
    coverage = read_coverage(request, grid)
    if request.merges is None:
        # Month headers are recognised by their merges, so none can be told apart: nothing is read, nothing is unsupported.
        return ScheduleSnapshot(source_scope=scope, source_revision=source_revision(request), coverage=coverage, groups=[])
    blocks = find_month_blocks(grid)
    if not blocks:
        return _unsupported(
            request,
            "no_month_header",
            "No month header found. Expected a month name in column A, merged across A:N, "
            "above a SUNDAY to SATURDAY header row.",
        )

    years, year_exceptions, method = _resolve_years(request, blocks)
    groups: list[Group] = []
    candidates: list[Candidate] = []
    non_tasks: list[NonTask] = []
    for block in blocks:
        key = f"m{block.index + 1:02d}-{block.name[:3].lower()}"
        year = years.get(block.index)
        exceptions = [block.problem] if block.problem else list(year_exceptions.get(block.index, []))
        evidence: list[Evidence] = []
        days = _blank_and_invalid_days(grid, block, year)
        if block.problem is None:
            exceptions.extend(_invalid_day_exceptions(days))
        if year is not None and not exceptions:
            evidence.append(Evidence(kind="year", detail=f"{year} {method}", anchor=block.header_range))
            read = _read_entries(grid, request, block, key, YearEvidence(year=year, method=method), year)
            non_tasks.extend(read.non_tasks)
            exceptions.extend(read.exceptions)
            days = _mark_unread(days, read.unread)
            if group_status(exceptions) != "held":  # a held group carries no candidates
                candidates.extend(read.candidates)
        non_tasks.extend(_month_notes(grid, block, key))
        exceptions = with_source_uncertainty(exceptions, coverage)
        groups.append(
            Group(
                group_key=key,
                label=block.name,
                status=group_status(exceptions),
                anchor=block.header_range,
                year=year,
                evidence=evidence,
                exceptions=exceptions,
                days=unread_days(days, coverage),
            )
        )
    numbered = [c.model_copy(update={"candidate_key": f"c{n}"}) for n, c in enumerate(candidates, 1)]
    return ScheduleSnapshot(
        source_scope=scope,
        source_revision=source_revision(request),
        coverage=coverage,
        groups=groups,
        candidates=numbered,
        non_tasks=non_tasks,
    )


def _unsupported(request: ScheduleRequest, code: str, message: str) -> UnsupportedLayout:
    return UnsupportedLayout.model_validate(
        {
            "source_scope": request.source_scope,
            "source_revision": source_revision(request),
            "diagnostic": {"code": code, "message": message},
        }
    )


def _day_cells(block: MonthBlock) -> list[DayCell]:
    return [DayCell(block.month, day, col) for band in block.bands for col, day in band.days.items()]


def _mismatch(block: MonthBlock, year: int | None = None) -> list[GroupException]:
    where = f" in {year}" if year is not None else " in the years around the reference date"
    return [
        GroupException(
            code="weekday_mismatch",
            detail=f"day numbers do not fall on their weekdays{where}",
            anchor=block.header_range,
        )
    ]


def _resolve_years(
    request: ScheduleRequest, blocks: list[MonthBlock]
) -> tuple[dict[int, int], dict[int, list[GroupException]], Method]:
    """Year per readable block, or the hold that stops it. Returns (years, holds, method).

    A supplied year is the first block's. Otherwise the start year that aligns the most blocks, within a year
    of the reference date, must be unique; a block that does not align with it is held alone.
    """
    months = [b.month for b in blocks]
    readable = [b for b in blocks if b.problem is None]
    method: Method = "supplied" if request.year is not None else "inferred_weekday_alignment"
    if request.year is not None:
        start = request.year
    elif request.reference_date is None:
        hold = GroupException(code="ambiguous_year", detail="no year and no reference date to infer the year from")
        return {}, {b.index: [hold] for b in readable}, method
    else:
        fits = best_start_years(
            months, [_day_cells(b) if b.problem is None else None for b in blocks], request.reference_date.year
        )
        if len(fits) > 1:
            hold = GroupException(
                code="ambiguous_year",
                detail=f"more than one year fits the weekday alignment equally well, starting {', '.join(map(str, fits))}",
            )
            return {}, {b.index: [hold] for b in readable}, method
        if not fits:
            return {}, {b.index: _mismatch(b) for b in readable}, method
        start = fits[0]
    by_block = block_years(start, months)
    years: dict[int, int] = {}
    holds: dict[int, list[GroupException]] = {}
    for block in readable:
        year = by_block[block.index]
        if aligns(year, _day_cells(block)):
            years[block.index] = year
        else:
            holds[block.index] = _mismatch(block, year)
    return years, holds, method


def _blank_and_invalid_days(grid: Grid, block: MonthBlock, year: int | None) -> list[DayState]:
    """One state per day-number cell: `unread` until its entries are read, `blank` outside the month."""
    if block.problem is not None:
        if block.problem.code == "invalid_day_number":
            return [DayState(anchor=block.problem.anchor or block.header_range, state="invalid")]
        return []
    days: list[DayState] = []
    for band in block.bands:
        for col in LEFT_COLUMNS:
            anchor = a1(band.number_row, col)
            day = band.days.get(col)
            if day is not None:
                made = make_date(year, block.month, day) if year is not None else None
                days.append(DayState(anchor=anchor, state="unread", day=day, date=made.isoformat() if made else None))
            elif clean_text(grid.value(band.number_row, col)):
                days.append(DayState(anchor=anchor, state="invalid"))
            else:
                days.append(DayState(anchor=anchor, state="blank"))
    return days


def _invalid_day_exceptions(days: list[DayState]) -> list[GroupException]:
    return [
        GroupException(code="invalid_day_number", detail="not a day of the month", anchor=d.anchor)
        for d in days
        if d.state == "invalid"
    ]


def _mark_unread(days: list[DayState], unread: set[str]) -> list[DayState]:
    """Entries were read for the group: a numbered day is `read` unless one of its entries was not."""
    return [
        d.model_copy(update={"state": "unread" if d.anchor in unread else "read"}) if d.state == "unread" else d
        for d in days
    ]


class _Read:
    def __init__(self) -> None:
        self.candidates: list[Candidate] = []
        self.non_tasks: list[NonTask] = []
        self.exceptions: list[GroupException] = []
        self.unread: set[str] = set()  # day-number anchors of days with an entry that was not read


def _read_entries(
    grid: Grid, request: ScheduleRequest, block: MonthBlock, key: str, year_evidence: YearEvidence, year: int
) -> _Read:
    read = _Read()
    ids = source_ids(request)
    for band in block.bands:
        for row in range(band.number_row + 1, band.end_row):
            for col in range(DAY_COLUMNS):
                text = clean_text(grid.value(row, col))
                if not text or grid.covered(row, col):
                    continue
                merge = grid.merge_at(row, col)
                anchor = a1(row, col)
                number_col = col // 2 * 2
                last_col = merge.end_col - 1 if merge else col
                if col // 2 == last_col // 2:
                    pairs: range | None = range(col // 2, col // 2 + 1)
                else:
                    assert merge is not None
                    pairs = day_pairs(merge.start_col, merge.end_col)
                day = band.days.get(number_col)
                if day is None:
                    read.exceptions.append(
                        GroupException(code="undated_entry", detail="entry sits under a blank day", anchor=anchor)
                    )
                    covered = range(col // 2, min(last_col // 2, DAY_COLUMNS // 2 - 1) + 1)
                    read.unread.update(a1(band.number_row, pair * 2) for pair in covered)
                    continue
                if is_availability(text):
                    read.non_tasks.append(NonTask(kind="availability", text=text, group_key=key, anchor=anchor))
                    continue
                if is_break(text):
                    read.non_tasks.append(NonTask(kind="break", text=text, group_key=key, anchor=anchor))
                    continue
                problem = "its ends are not on day boundaries" if pairs is None else None
                dates = span_dates(year, block.month, band.days, pairs) if pairs is not None else None
                if dates is not None and dates.problem:
                    problem = dates.problem
                if pairs is None or dates is None or dates.start is None or dates.due is None:
                    assert merge is not None
                    read.exceptions.append(
                        GroupException(
                            code="ambiguous_span",
                            detail=f"{text!r} merged over {merge_range(merge)}: {problem}",
                            anchor=merge_range(merge),
                        )
                    )
                    read.unread.update(a1(band.number_row, pair * 2) for pair in range(merge.start_col // 2, (merge.end_col - 1) // 2 + 1))
                    continue
                read.candidates.append(
                    Candidate(
                        candidate_key="pending",
                        group_key=key,
                        title=text,
                        start=dates.start.isoformat(),
                        due=dates.due.isoformat(),
                        tentative=tentative_marker(text),
                        source_id=ids.get((row, col)),
                        provenance=Provenance(
                            tab_id=request.source_scope.tab_id,
                            tab_title=request.source_scope.tab_title,
                            anchor=anchor,
                            range=merge_range(merge) if merge else anchor,
                            merged=merge is not None,
                            fill=grid.fills.get((row, col)),
                            year_evidence=year_evidence,
                            date_evidence=DateEvidence(
                                month=block.month,
                                day=day,
                                weekday=weekday_name(number_col),
                                month_header_anchor=a1(block.header_row, 0),
                                day_number_anchor=a1(band.number_row, number_col),
                            ),
                        ),
                    )
                )
    return read


def _month_notes(grid: Grid, block: MonthBlock, key: str) -> list[NonTask]:
    """Column O text below the NOTES label, down to the next month block. Kept as text, never read as tasks."""
    first = grid.merge_at(block.header_row, 0)
    top = first.end_row if first else block.header_row + 1
    last = block.bands[-1].end_row if block.bands else top
    notes = []
    for row in range(top, last):
        text = clean_text(grid.value(row, DAY_COLUMNS))
        if text and not grid.covered(row, DAY_COLUMNS):
            notes.append(NonTask(kind="note", text=text, group_key=key, anchor=a1(row, DAY_COLUMNS)))
    return notes
