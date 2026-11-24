"""Recognise the monthly-calendar layout: month blocks and their week bands.

See docs/schedule-monthly-calendar.md. A block that starts like a month but has a
bad header or bad day numbers is kept with a `problem`, so its group is held and
the others still read. A tab with no month header at all is not this layout.
"""

from __future__ import annotations

from dataclasses import dataclass

from t6.schedule.contract import GroupException
from t6.schedule.dates import MONTHS, WEEKDAYS, day_number
from t6.schedule.grid import Grid, a1, a1_range
from t6.schedule.text import clean_text

DAY_COLUMNS = 14  # A:N; column O (index 14) holds month notes
LEFT_COLUMNS = range(0, DAY_COLUMNS, 2)


@dataclass(frozen=True, slots=True)
class WeekBand:
    """Rows from one day-number row up to the next. `days` maps a pair's left column to its day."""

    number_row: int
    end_row: int
    days: dict[int, int]


@dataclass(frozen=True, slots=True)
class MonthBlock:
    index: int
    month: int
    name: str
    header_row: int
    header_range: str
    bands: tuple[WeekBand, ...]
    problem: GroupException | None


def find_month_blocks(grid: Grid) -> list[MonthBlock]:
    """Every month header in the tab, with its week bands, in order down the tab."""
    headers = [
        (row, MONTHS[clean_text(grid.value(row, 0)).upper()])
        for row in range(grid.row_count)
        if clean_text(grid.value(row, 0)).upper() in MONTHS and _is_month_merge(grid, row)
    ]
    blocks = []
    for index, (row, month) in enumerate(headers):
        end = headers[index + 1][0] if index + 1 < len(headers) else grid.row_count
        blocks.append(_read_block(grid, index, row, month, end))
    return blocks


def _is_month_merge(grid: Grid, row: int) -> bool:
    merge = grid.merge_at(row, 0)
    return merge is not None and merge.end_col == DAY_COLUMNS


def _read_block(grid: Grid, index: int, row: int, month: int, end: int) -> MonthBlock:
    merge = grid.merge_at(row, 0)
    assert merge is not None
    name = clean_text(grid.value(row, 0)).upper()
    header_range = a1_range(row, merge.end_row - 1, 0, DAY_COLUMNS - 1)

    def block(bands: tuple[WeekBand, ...], problem: GroupException | None) -> MonthBlock:
        return MonthBlock(index, month, name, row, header_range, bands, problem)

    weekday_row = merge.end_row
    names = [clean_text(grid.value(weekday_row, col)).upper() for col in LEFT_COLUMNS]
    if names != list(WEEKDAYS):
        problem = GroupException(
            code="invalid_header",
            detail="expected SUNDAY to SATURDAY across A:N under the month name",
            anchor=a1_range(weekday_row, weekday_row, 0, DAY_COLUMNS - 1),
        )
        return block((), problem)

    number_rows = [
        r for r in range(weekday_row + 1, end) if any(_is_numeric(grid.value(r, c)) for c in LEFT_COLUMNS)
    ]
    if not number_rows:
        return block((), GroupException(code="invalid_header", detail="no week bands under the weekday header", anchor=a1(weekday_row, 0)))
    bands = []
    for k, number_row in enumerate(number_rows):
        band_end = number_rows[k + 1] if k + 1 < len(number_rows) else end
        days: dict[int, int] = {}
        for col in LEFT_COLUMNS:
            value = grid.value(number_row, col)
            if not _is_numeric(value):
                continue
            day = day_number(value)
            if day is None or not 1 <= day <= 31:
                problem = GroupException(
                    code="invalid_day_number", detail=f"not a day of the month: {value!r}", anchor=a1(number_row, col)
                )
                return block((), problem)
            days[col] = day
        bands.append(WeekBand(number_row, band_end, days))
    return block(tuple(bands), None)


def _is_numeric(value: object) -> bool:
    """A day-number cell: a number, or text that is only digits. Titles like "2ND pass" are not."""
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    return isinstance(value, str) and value.strip().isdigit()


def day_pairs(first_col: int, end_col: int) -> range | None:
    """The day pairs (0 is Sunday) covered by columns [first_col, end_col), or None when an end
    does not sit on a day boundary or the span leaves A:N. Only column geometry is used."""
    if first_col % 2 or end_col % 2 or not first_col < end_col <= DAY_COLUMNS:
        return None
    return range(first_col // 2, end_col // 2)
