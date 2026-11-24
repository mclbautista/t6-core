"""Read coverage, group status and what a snapshot may say about absence (#719).

Three things stay apart. A *held* group has a problem confined to it (a bad header, an ambiguous year). An
*incomplete* source is whole-source uncertainty: a short read, an unreported tab size or no merge list, and it
makes every group incomplete. A day is `read` only when its entries were read; a `blank`, `unread` or `invalid`
day is never "no task here". `standing` is the one place a planner asks whether missing means absent.
"""

from __future__ import annotations

from typing import Literal

from t6.schedule.contract import (
    Coverage,
    CoverageReason,
    DayState,
    GroupException,
    GroupStatus,
    ScheduleRequest,
    ScheduleSnapshot,
)
from t6.schedule.grid import Grid, a1_range

HOLD_CODES = frozenset(
    {"invalid_header", "invalid_day_number", "ambiguous_year", "weekday_mismatch", "ambiguous_span"}
)

Standing = Literal["read", "held", "incomplete", "not_in_snapshot"]


def read_coverage(request: ScheduleRequest, grid: Grid) -> Coverage:
    """What was read of the tab. A short read, an unreported size or no merge list is incomplete."""
    rows, columns = request.rows_total, request.columns_total
    reasons: list[CoverageReason] = []
    if rows is None or columns is None:
        reasons.append("extent_unknown")
    if rows is not None and grid.row_count < rows:
        reasons.append("rows_short")
    if columns is not None and grid.column_count < columns:
        reasons.append("columns_short")
    if request.merges is None:
        reasons.append("merges_missing")
    return Coverage(
        status="incomplete" if reasons else "complete",
        rows_read=grid.row_count,
        columns_read=grid.column_count,
        rows_total=rows,
        columns_total=columns,
        read_range=_extent(grid.row_count, grid.column_count),
        total_range=_extent(rows, columns),
        reasons=reasons,
    )


def _extent(rows: int | None, columns: int | None) -> str | None:
    if not rows or not columns:
        return None
    return a1_range(0, rows - 1, 0, columns - 1)


def group_status(exceptions: list[GroupException]) -> GroupStatus:
    """A group with a hold is held; with only unread parts it is incomplete; else complete."""
    if any(e.code in HOLD_CODES for e in exceptions):
        return "held"
    return "incomplete" if exceptions else "complete"


def with_source_uncertainty(exceptions: list[GroupException], coverage: Coverage) -> list[GroupException]:
    """Whole-source uncertainty makes a group incomplete. A held group stays held: its hold is its own problem."""
    if coverage.status == "complete" or group_status(exceptions) == "held":
        return exceptions
    detail = (
        f"source not fully read ({', '.join(coverage.reasons)}): "
        f"read {coverage.read_range} of {coverage.total_range}"
    )
    return [*exceptions, GroupException(code="unread_region", detail=detail)]


def unread_days(days: list[DayState], coverage: Coverage) -> list[DayState]:
    """Days read from an incomplete source were not fully read; blank and invalid days stay as they are."""
    if coverage.status == "complete":
        return days
    return [d.model_copy(update={"state": "unread"}) if d.state == "read" else d for d in days]


def standing(snapshot: ScheduleSnapshot, group_key: str, day: str | None = None) -> Standing:
    """May a missing candidate in this group (or on this ISO day) be read as "no task"?

    `read` says yes. `held`, `incomplete` and `not_in_snapshot` say no: the candidate is unknown here, so no
    deletion, match or addition may follow from it being absent.
    """
    group = next((g for g in snapshot.groups if g.group_key == group_key), None)
    if group is None:
        return "incomplete" if snapshot.coverage.status == "incomplete" else "not_in_snapshot"
    if group.status == "held":
        return "held"
    if day is not None:
        state = next((d for d in group.days if d.date == day), None)
        if state is None:
            return "not_in_snapshot"
        complete = group.status == "complete" and snapshot.coverage.status == "complete"
        return "read" if state.state == "read" and complete else "incomplete"
    return "read" if group.status == "complete" and snapshot.coverage.status == "complete" else "incomplete"
