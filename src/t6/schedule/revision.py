"""Revision identity: which read of a tab this is, and the durable IDs a client supplied. Pure.

`source_revision` is the Google revision when the request gives one. Otherwise it is a
fingerprint of the normalized values plus merge geometry, so the same input always gives
the same fingerprint and any change to a value or a merge gives a different one. Fills,
supplied IDs and the tab's size hints are not part of it.
"""

from __future__ import annotations

import hashlib
import json

from t6.schedule.contract import CellValue, ScheduleRequest


def _normal(value: CellValue) -> str | int | float | None:
    """A value as the fingerprint sees it: blank is None, text is stripped, a whole float is an int."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def source_revision(request: ScheduleRequest) -> str:
    if request.source_revision is not None:
        return request.source_revision
    values = [
        [row, col, normal]
        for row, cells in enumerate(request.cells)
        for col, value in enumerate(cells)
        if (normal := _normal(value)) is not None
    ]
    merges = sorted((m.start_row, m.end_row, m.start_col, m.end_col) for m in request.merges)
    payload = json.dumps({"values": values, "merges": merges}, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


def source_ids(request: ScheduleRequest) -> dict[tuple[int, int], str]:
    """Durable IDs the client supplied, by cell. The cell is only where the ID was read this time."""
    return {(s.row, s.col): s.id for s in request.source_ids}
