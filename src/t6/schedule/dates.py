"""Month names, weekday alignment, the year and the end date of a multi-day span."""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date

from t6.schedule.contract import CellValue

MONTHS = {name.upper(): number for number, name in enumerate(calendar.month_name) if name}
WEEKDAYS = ("SUNDAY", "MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY")

# Start years tried when the caller supplies none: the reference year and the one on either side.
INFERENCE_SPAN = 1


def day_number(value: CellValue) -> int | None:
    """The day of month in a day-number cell: 1, 1.0 or "1". Anything else is None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value) if float(value).is_integer() else None
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def column_weekday(col: int) -> int:
    """Weekday index (Monday 0) of a day pair: columns A:B are Sunday."""
    return (col // 2 - 1) % 7


def weekday_name(col: int) -> str:
    return WEEKDAYS[col // 2]


def make_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class DayCell:
    """A day number under a month name: which month, which day, and the column it sits in."""

    month: int
    day: int
    col: int


def aligns(year: int, cells: list[DayCell]) -> bool:
    """True when every numbered cell falls on its column's weekday in `year`."""
    for cell in cells:
        d = make_date(year, cell.month, cell.day)
        if d is None or d.weekday() != column_weekday(cell.col):
            return False
    return True


def block_years(start_year: int, months: list[int]) -> list[int]:
    """The year of each month block in order; the year rolls over when the month number drops."""
    years: list[int] = []
    year = start_year
    for index, month in enumerate(months):
        if index and month < months[index - 1]:
            year += 1
        years.append(year)
    return years


def best_start_years(months: list[int], cells_by_block: list[list[DayCell] | None], reference_year: int) -> list[int]:
    """The start years near `reference_year` that align the most blocks (None cells: a block not counted).

    Empty when no start aligns any block; more than one when the best is tied.
    """
    scores: dict[int, int] = {}
    for start in range(reference_year - INFERENCE_SPAN, reference_year + INFERENCE_SPAN + 1):
        years = block_years(start, months)
        scores[start] = sum(
            1 for y, cells in zip(years, cells_by_block, strict=True) if cells is not None and aligns(y, cells)
        )
    best = max(scores.values())
    return [start for start, score in scores.items() if best and score == best]


@dataclass(frozen=True, slots=True)
class SpanDates:
    """The inclusive ends of a span, or why they cannot be placed. Exactly one side is set."""

    start: date | None = None
    due: date | None = None
    problem: str | None = None


def span_dates(year: int, month: int, band_days: dict[int, int], pairs: range) -> SpanDates:
    """Inclusive dates of the day pairs `pairs` (pair 0 is Sunday) in one week band.

    `band_days` maps a pair's left column to its day number. Both ends need a day number, the
    numbers between must run one day at a time, and the end must fall on its column's weekday
    (this also checks that the end does not run past the month end).
    """
    first, last = pairs[0], pairs[-1]
    start_day, end_day = band_days.get(first * 2), band_days.get(last * 2)
    if start_day is None or end_day is None:
        return SpanDates(problem=f"the span's {'start' if start_day is None else 'end'} day has no day number")
    if end_day - start_day != last - first:
        return SpanDates(problem=f"day numbers {start_day} to {end_day} do not match {len(pairs)} day columns")
    start, due = make_date(year, month, start_day), make_date(year, month, end_day)
    if start is None or due is None:
        return SpanDates(problem=f"day {end_day if due is None else start_day} does not exist in month {month} of {year}")
    if due.weekday() != column_weekday(last * 2):
        return SpanDates(problem=f"{due.isoformat()} does not fall on {WEEKDAYS[last]}")
    return SpanDates(start=start, due=due)
