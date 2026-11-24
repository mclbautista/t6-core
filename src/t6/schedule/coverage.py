"""Read coverage and group status. #719 extends this with the coverage rules."""

from __future__ import annotations

from t6.schedule.contract import Coverage, GroupException, GroupStatus, ScheduleRequest
from t6.schedule.grid import Grid

HOLD_CODES = frozenset(
    {"invalid_header", "invalid_day_number", "ambiguous_year", "weekday_mismatch", "ambiguous_span"}
)


def read_coverage(request: ScheduleRequest, grid: Grid) -> Coverage:
    """What was read of the tab. Fewer rows or columns than the tab holds is incomplete."""
    short = (request.rows_total is not None and grid.row_count < request.rows_total) or (
        request.columns_total is not None and grid.column_count < request.columns_total
    )
    return Coverage(
        status="incomplete" if short else "complete",
        rows_read=grid.row_count,
        columns_read=grid.column_count,
        rows_total=request.rows_total,
        columns_total=request.columns_total,
    )


def group_status(exceptions: list[GroupException]) -> GroupStatus:
    """A group with a hold is held; with only unread parts it is incomplete; else complete."""
    if any(e.code in HOLD_CODES for e in exceptions):
        return "held"
    return "incomplete" if exceptions else "complete"
