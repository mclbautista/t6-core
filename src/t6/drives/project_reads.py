"""Effective Project Movement history and weekly Drive traffic, in Manila time."""
from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, time, timedelta
import sqlite3
from typing import Any

from t6.domain.studio_time import MANILA
from t6.drives import Ref
from t6.drives.movements import Movement, active_movements
from t6.drives.overdue import overdue_drives
from t6.drives.sqlite import SqliteDriveStore
from t6.registry.sqlite import SqliteProjectRegistry
from t6.verbs import DriveProjectWeeklyCount, DriveProjectWeeklyOutput, DriveRef


def _when(movement: Movement) -> datetime | None:
    if movement.posted_at:
        posted = datetime.fromisoformat(movement.posted_at).astimezone(MANILA)
        if movement.corrects:  # An explicit date correction takes precedence over the original post's day.
            return datetime.combine(movement.on, posted.timetz()) if movement.on else None
        return posted
    return datetime.combine(movement.on, time(), MANILA) if movement.on else None


def _movements(db: sqlite3.Connection) -> list[Movement]:
    # Clearing a Capture guess appends a drive-less correction, not another handover.
    return [m for m in active_movements(db) if not (m.capture and m.corrects and m.drive is None)]


def project_movements(db: sqlite3.Connection, project: str) -> list[dict[str, Any]]:
    movements = [m for m in _movements(db) if m.project and project in (m.project.page_id, m.project.text)]
    movements.sort(key=lambda m: _when(m) or datetime.min.replace(tzinfo=MANILA), reverse=True)
    return [{**asdict(m), 'on': when.date().isoformat() if (when := _when(m)) else None} for m in movements]


def project_weekly(registry: SqliteProjectRegistry, week: date | None, project: str | None) -> DriveProjectWeeklyOutput:
    today = datetime.fromtimestamp(registry._clock(), MANILA).date()
    day = week or today
    start = day - timedelta(days=day.weekday())
    end = start + timedelta(days=6)
    with registry._transaction() as db:
        store = SqliteDriveStore(registry, within=db)
        drives = store.list_drives()
        projects: dict[Ref, set[str]] = {}
        for drive in drives:
            for assignment in store.list_assignments(drive.id):
                if assignment.is_open and assignment.project:
                    projects.setdefault(assignment.project, set()).add(drive.id)
        movements = _movements(db)
        for movement in movements:
            if movement.project:
                projects.setdefault(movement.project, set())
        overdue = overdue_drives(store, drives, today)
        rows = []
        for ref, ids in projects.items():
            if project is not None and project not in (ref.page_id, ref.text):
                continue
            weekly = [m for m in movements if m.project == ref and (when := _when(m)) and start <= when.date() <= end]
            rows.append(DriveProjectWeeklyCount(project=DriveRef(page_id=ref.page_id, text=ref.text),
                        in_count=sum(m.direction == 'IN' for m in weekly), out_count=sum(m.direction == 'OUT' for m in weekly),
                        overdue=[row for row in overdue if row.drive_id in ids]))
    return DriveProjectWeeklyOutput(week_start=start.isoformat(), week_end=end.isoformat(), as_of=today.isoformat(), projects=rows)
