"""The extractor: cell values plus merge geometry in, ScheduleSnapshot/v1 out. Pure; no I/O."""

from __future__ import annotations

from typing import Literal

from t6.schedule.contract import (
    Candidate,
    DateEvidence,
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
from t6.schedule.coverage import group_status, read_coverage
from t6.schedule.dates import DayCell, aligns, block_years, fitting_start_years, weekday_name
from t6.schedule.grid import Grid, a1, merge_range
from t6.schedule.layout import DAY_COLUMNS, MonthBlock, find_month_blocks
from t6.schedule.text import clean_text, is_availability


Method = Literal["supplied", "inferred_weekday_alignment"]


def extract_schedule(request: ScheduleRequest) -> ScheduleSnapshot | UnsupportedLayout:
    grid = Grid.from_request(request)
    scope = request.source_scope
    if grid.row_count == 0:
        return _unsupported(request, "empty_tab", "The tab has no cells.")
    blocks = find_month_blocks(grid)
    if not blocks:
        return _unsupported(
            request,
            "no_month_header",
            "No month header found. Expected a month name in column A, merged across A:N, "
            "above a SUNDAY to SATURDAY header row.",
        )

    years, year_exceptions, method = _resolve_years(request.year, blocks, grid)
    groups: list[Group] = []
    candidates: list[Candidate] = []
    non_tasks: list[NonTask] = []
    for block in blocks:
        key = f"m{block.index + 1:02d}-{block.name[:3].lower()}"
        year = years.get(block.index)
        exceptions = [block.problem] if block.problem else list(year_exceptions.get(block.index, []))
        evidence: list[Evidence] = []
        if year is not None and not exceptions:
            evidence.append(Evidence(kind="year", detail=f"{year} {method}", anchor=block.header_range))
            read = _read_entries(grid, request, block, key, YearEvidence(year=year, method=method), year)
            candidates.extend(read.candidates)
            non_tasks.extend(read.non_tasks)
            exceptions.extend(read.exceptions)
        groups.append(
            Group(
                group_key=key,
                label=block.name,
                status=group_status(exceptions),
                anchor=block.header_range,
                year=year,
                evidence=evidence,
                exceptions=exceptions,
            )
        )
    numbered = [c.model_copy(update={"candidate_key": f"c{n}"}) for n, c in enumerate(candidates, 1)]
    return ScheduleSnapshot(
        source_scope=scope,
        source_revision=request.source_revision,
        coverage=read_coverage(request, grid),
        groups=groups,
        candidates=numbered,
        non_tasks=non_tasks,
    )


def _unsupported(request: ScheduleRequest, code: str, message: str) -> UnsupportedLayout:
    return UnsupportedLayout.model_validate(
        {
            "source_scope": request.source_scope,
            "source_revision": request.source_revision,
            "diagnostic": {"code": code, "message": message},
        }
    )


def _day_cells(block: MonthBlock) -> list[DayCell]:
    return [DayCell(block.month, day, col) for band in block.bands for col, day in band.days.items()]


def _resolve_years(
    supplied: int | None, blocks: list[MonthBlock], grid: Grid
) -> tuple[dict[int, int], dict[int, list[GroupException]], Method]:
    """Year per readable block, or the hold that stops it. Returns (years, holds, method)."""
    months = [b.month for b in blocks]
    readable = [b for b in blocks if b.problem is None]
    if supplied is not None:
        method: Method = "supplied"
        by_block = dict(enumerate(block_years(supplied, months)))
        years = {}
        holds: dict[int, list[GroupException]] = {}
        for block in readable:
            if aligns(by_block[block.index], _day_cells(block)):
                years[block.index] = by_block[block.index]
            else:
                holds[block.index] = [
                    GroupException(
                        code="weekday_mismatch",
                        detail=f"day numbers do not fall on their weekdays in {by_block[block.index]}",
                        anchor=block.header_range,
                    )
                ]
        return years, holds, method

    inferred: Method = "inferred_weekday_alignment"
    cells = [_day_cells(b) if b.problem is None else [] for b in blocks]
    fits = fitting_start_years(months, cells)
    if len(fits) == 1:
        return dict(enumerate(block_years(fits[0], months))), {}, inferred
    if fits:
        hold = GroupException(
            code="ambiguous_year",
            detail=f"more than one year fits the weekday alignment, starting {', '.join(map(str, fits))}",
        )
    else:
        hold = GroupException(code="weekday_mismatch", detail="no year in the inference window fits the weekday alignment")
    return {}, {b.index: [hold] for b in readable}, inferred


class _Read:
    def __init__(self) -> None:
        self.candidates: list[Candidate] = []
        self.non_tasks: list[NonTask] = []
        self.exceptions: list[GroupException] = []


def _read_entries(
    grid: Grid, request: ScheduleRequest, block: MonthBlock, key: str, year_evidence: YearEvidence, year: int
) -> _Read:
    read = _Read()
    for band in block.bands:
        for row in range(band.number_row + 1, band.end_row):
            for col in range(DAY_COLUMNS):
                text = clean_text(grid.value(row, col))
                if not text or grid.covered(row, col):
                    continue
                merge = grid.merge_at(row, col)
                last_col = merge.end_col - 1 if merge else col
                anchor = a1(row, col)
                if col // 2 != last_col // 2:
                    read.exceptions.append(
                        GroupException(code="span_not_read", detail="entry spans several days", anchor=anchor)
                    )
                    continue
                day = band.days.get(col // 2 * 2)
                if day is None:
                    read.exceptions.append(
                        GroupException(code="undated_entry", detail="entry sits under a blank day", anchor=anchor)
                    )
                    continue
                if is_availability(text):
                    read.non_tasks.append(NonTask(kind="availability", text=text, group_key=key, anchor=anchor))
                    continue
                iso = f"{year:04d}-{block.month:02d}-{day:02d}"
                number_col = col // 2 * 2
                read.candidates.append(
                    Candidate(
                        candidate_key="pending",
                        group_key=key,
                        title=text,
                        start=iso,
                        due=iso,
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
