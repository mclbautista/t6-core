"""Parse an Instagantt Standalone spreadsheet export (CSV) into timelines.

Assumed columns (matched by header, case and spacing ignored): a project
(`Project`, `Project Name`), optionally a project ID (`Project ID`), a task
(`Task`, `Task Name`, `Name`), a start (`Start`, `Start Date`) and an end
(`End`, `End Date`, `Finish`, `Due Date`). Other columns are ignored. A blank
end makes a milestone. Dates are ISO (`2026-11-08`) or US (`11/8/2026`,
`Nov 8, 2026`). The export format has not been checked against a live export
yet; adjust `COLUMNS` and `DATE_FORMATS` when it is.

Excel files are not read: save the sheet as CSV first.
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime
from pathlib import Path

from t6.instagantt.model import InstaganttError, Task, Timeline

COLUMNS: dict[str, tuple[str, ...]] = {
    "project": ("project", "projectname"),
    "project_id": ("projectid",),
    "task": ("task", "taskname", "name"),
    "start": ("start", "startdate"),
    "end": ("end", "enddate", "finish", "duedate"),
}
DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%b %d, %Y", "%B %d, %Y")


def _key(header: str) -> str:
    return "".join(ch for ch in header.lower() if ch.isalnum())


def _parse_date(text: str, line: int, column: str) -> date:
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text.strip(), fmt).date()
        except ValueError:
            continue
    raise InstaganttError(f"line {line}: cannot read {column} date {text!r}")


def parse_export(text: str) -> tuple[Timeline, ...]:
    """Timelines in order of first appearance. Tasks keep export order."""
    reader = csv.reader(io.StringIO(text.lstrip("﻿")))
    try:
        header = next(reader)
    except StopIteration:
        raise InstaganttError("the export is empty") from None
    index: dict[str, int] = {}
    keys = [_key(h) for h in header]
    for field, aliases in COLUMNS.items():
        for alias in aliases:
            if alias in keys:
                index[field] = keys.index(alias)
                break
    missing = [f for f in ("project", "task", "start") if f not in index]
    if missing:
        raise InstaganttError(f"the export has no column for: {', '.join(missing)}")

    def cell(row: list[str], field: str) -> str:
        i = index.get(field)
        return row[i].strip() if i is not None and i < len(row) else ""

    order: list[str] = []
    ids: dict[str, str] = {}
    tasks: dict[str, list[Task]] = {}
    for line, row in enumerate(reader, start=2):
        if not any(c.strip() for c in row):
            continue
        project, name = cell(row, "project"), cell(row, "task")
        if not project:
            raise InstaganttError(f"line {line}: task {name!r} has no project")
        if not name:
            raise InstaganttError(f"line {line}: a task in {project!r} has no name")
        start = _parse_date(cell(row, "start"), line, "start")
        end_text = cell(row, "end")
        end = _parse_date(end_text, line, "end") if end_text else start
        if end < start:
            raise InstaganttError(f"line {line}: task {name!r} ends before it starts")
        if project not in tasks:
            order.append(project)
            tasks[project] = []
            ids[project] = cell(row, "project_id") or project
        tasks[project].append(Task(name=name, start=start, end=end))
    return tuple(Timeline(p, ids[p], tuple(tasks[p])) for p in order)


def read_export(path: str | Path) -> tuple[Timeline, ...]:
    """Parse the CSV export file at `path`."""
    return parse_export(Path(path).read_text(encoding="utf-8"))
