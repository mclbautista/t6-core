"""Month names, weekday alignment and the year. #718 extends this with ambiguous-year holds."""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date

from t6.schedule.contract import CellValue

MONTHS = {name.upper(): number for number, name in enumerate(calendar.month_name) if name}
WEEKDAYS = ("SUNDAY", "MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY")

# Years tried when the caller supplies none. A sheet outside this window needs a supplied year.
INFERENCE_WINDOW = range(2020, 2036)


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


def fitting_start_years(months: list[int], cells_by_block: list[list[DayCell]]) -> list[int]:
    """Start years in the window where every block's day numbers match their weekdays."""
    fits = []
    for start in INFERENCE_WINDOW:
        years = block_years(start, months)
        if all(aligns(y, cells) for y, cells in zip(years, cells_by_block, strict=True)):
            fits.append(start)
    return fits
