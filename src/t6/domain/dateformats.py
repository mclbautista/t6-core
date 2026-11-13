"""Named message date formats, one per use, always in Manila time.

Pure functions: nothing here does I/O. The weekday is computed from the date,
never typed. Callers pass an aware datetime (any zone) or, for the long date,
epoch seconds; the value is converted to Manila before it is formatted.
"""

from __future__ import annotations

from datetime import date, datetime

from t6.domain.studio_time import MANILA

__all__ = [
    "MANILA",
    "briefing_date",
    "concern_age",
    "deadline",
    "flag_date",
    "formatter_date",
    "long_date",
    "minutes_title",
    "report_header",
]



def _manila(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        raise ValueError("a naive datetime has no zone; pass an aware one")
    return moment.astimezone(MANILA)


def deadline(moment: datetime) -> str:
    """`Nov 13 / Fri (23:59)`."""
    m = _manila(moment)
    return f"{m:%b} {m.day} / {m:%a} ({m:%H:%M})"


def report_header(moment: datetime) -> str:
    """`Fri, Nov 13, 2026`."""
    m = _manila(moment)
    return f"{m:%a}, {m:%b} {m.day}, {m.year}"


def long_date(moment: datetime | float) -> str:
    """`12 Nov 2026, 19:05 (Manila)`. A number is epoch seconds."""
    m = datetime.fromtimestamp(moment, MANILA) if isinstance(moment, (int, float)) else _manila(moment)
    return f"{m:%d %b %Y, %H:%M} (Manila)"


def briefing_date(day: date) -> str:
    """`Sat Nov 14`."""
    return f"{day:%a %b} {day.day}"


def flag_date(day: date) -> str:
    """`Dec 1, 2026`."""
    return f"{day:%b} {day.day}, {day.year}"


def concern_age(since: date, today: date) -> str:
    """`since Oct 26 (2 wks)`. Under a week the age is in days, else whole weeks."""
    days = (today - since).days
    if days < 0:
        raise ValueError("since is after today")
    if days < 7:
        age = f"{days} day" if days == 1 else f"{days} days"
    else:
        weeks = days // 7
        age = f"{weeks} wk" if weeks == 1 else f"{weeks} wks"
    return f"since {since:%b} {since.day} ({age})"


def minutes_title(day: date) -> str:
    """`PSR MOM 2026-11-16`."""
    return f"PSR MOM {day.isoformat()}"


def formatter_date(day: date) -> str:
    """`July 23 / Thursday`."""
    return f"{day:%B} {day.day} / {day:%A}"
