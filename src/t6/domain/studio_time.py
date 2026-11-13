"""The studio's clock: one Asia/Manila zone, the end-of-day time, and the shift calendar."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time, timedelta, timezone

__all__ = ["END_OF_DAY", "MANILA", "SHIFT_REPORT_SLOTS", "ShiftReportSlot"]

MANILA = timezone(timedelta(hours=8), "Asia/Manila")
"""Asia/Manila has no daylight saving time, so a fixed offset is exact."""

END_OF_DAY = time(23, 59)
"""A date with no time, and the fallback when no assignee has a fixed shift."""


@dataclass(frozen=True, slots=True)
class ShiftReportSlot:
    """One shift report: when it goes out (Manila) and when its window opens."""

    name: str
    at: time
    opens_at: time


SHIFT_REPORT_SLOTS = (
    ShiftReportSlot("0500", time(5, 0), time(19, 0)),
    ShiftReportSlot("1130", time(11, 30), time(5, 0)),
    ShiftReportSlot("1900", time(19, 0), time(11, 30)),
)
"""Three shift reports a day. Each covers the time since the previous one went out."""
