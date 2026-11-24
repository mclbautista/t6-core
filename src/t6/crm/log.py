"""Read the CRM Sheet's Log tab: rows by Project, last contact, links by kind, reminders due."""
from __future__ import annotations

import re
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from t6.drives.sheets import SheetSource

LOG_TAB = 'Log'
LINK_KINDS = ('quote', 'billing', 'gmail_thread')  # xero_invoice joins later
REMINDER_KINDS = ('quote', 'billing')
_PROJECT_ID = re.compile(r't6p_[0-9a-f]{24}')
_SERIAL_EPOCH = date(1899, 12, 30)

StageChange = Callable[[str], date | None]
"""A Project's last stage change as a Manila day. The caller wires it to the stage history."""


@dataclass(frozen=True)
class LogRow:
    project_id: str
    day: date
    kind: str
    note: str | None
    link: str | None
    by: str | None
    remind_on: date | None
    done: bool


@dataclass(frozen=True)
class Unmapped:
    row: int  # the 1-based Sheet row, so a person can find it
    reason: str  # 'no project', 'unknown project' or 'no date'


@dataclass
class CrmLog:
    by_project: dict[str, list[LogRow]] = field(default_factory=dict)
    unmapped: list[Unmapped] = field(default_factory=list)

    def last_contact(self, project_id: str, stage_change: StageChange) -> date | None:
        days = [row.day for row in self.by_project.get(project_id, ())]
        changed = stage_change(project_id)
        return max(days + ([changed] if changed else []), default=None)


def _day(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _SERIAL_EPOCH + timedelta(days=int(value))
    return None


def _text(value: Any) -> str | None:
    return str(value).strip() or None if value is not None else None


def read_log(source: SheetSource, known_projects: Collection[str]) -> CrmLog:
    """Parse the Log tab by header name. A row maps to a Project only by its `t6p_` ID."""
    rows = source.tabs().get(LOG_TAB)
    if not rows:
        raise ValueError(f'The CRM Sheet has no {LOG_TAB} tab')
    names = [str(cell).strip().lower() if cell is not None else '' for cell in rows[0]]
    for needed in ('date', 'project', 'kind'):
        if needed not in names:
            raise ValueError(f'The CRM Sheet {LOG_TAB} tab has no {needed.title()} column')

    def cell(values: tuple[Any, ...], name: str) -> Any:
        index = names.index(name) if name in names else len(values)
        return values[index] if index < len(values) else None

    log = CrmLog()
    for number, values in enumerate(rows[1:], start=2):
        if all(value is None for value in values):
            continue
        found = _PROJECT_ID.search(str(cell(values, 'project') or ''))
        if not found:
            log.unmapped.append(Unmapped(number, 'no project'))
            continue
        if found.group() not in known_projects:
            log.unmapped.append(Unmapped(number, 'unknown project'))
            continue
        day = _day(cell(values, 'date'))
        if day is None:
            log.unmapped.append(Unmapped(number, 'no date'))
            continue
        log.by_project.setdefault(found.group(), []).append(LogRow(
            found.group(), day, (_text(cell(values, 'kind')) or '').lower(), _text(cell(values, 'note')),
            _text(cell(values, 'link')), _text(cell(values, 'by')), _day(cell(values, 'remind on')),
            cell(values, 'done') is True))
    return log


def links_by_kind(log: CrmLog, project_id: str) -> dict[str, list[str]]:
    """A Project's Log links grouped by kind, in Sheet order; every known kind is present."""
    links: dict[str, list[str]] = {kind: [] for kind in LINK_KINDS}
    for row in log.by_project.get(project_id, ()):
        if row.link and row.kind in links:
            links[row.kind].append(row.link)
    return links


def due_reminders(log: CrmLog, today: date) -> list[LogRow]:
    """Quote and billing rows whose Remind on is today or earlier and not Done."""
    return [row for rows in log.by_project.values() for row in rows
            if row.kind in REMINDER_KINDS and row.remind_on and row.remind_on <= today and not row.done]
