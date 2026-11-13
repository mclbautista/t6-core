"""Studio rules for Projects: the status gate, date flags, missing roles, Health order, code display.

Pure functions over `t6.domain.Project`. Status and Health are matched by their
words, ignoring emoji, case and extra spaces, so a decorated Notion option such
as "🚨 In-Progress" and a plain "in-progress" are the same status. What an app
does with a flag (wording, layout) stays in the app.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from t6.domain import Project
from t6.domain.dateformats import MANILA

__all__ = [
    "DEFAULT_LEAD_WARNING_DAYS",
    "DateFlag",
    "StatusGroup",
    "code_labels",
    "date_flags",
    "health_missing",
    "health_rank",
    "is_snapshotted",
    "missing_roles",
    "status_gate",
    "status_key",
]

DEFAULT_LEAD_WARNING_DAYS = 30
"""An Incoming Project's Playdate this close (in days) is flagged."""


class StatusGroup(StrEnum):
    IN_PROGRESS = "in_progress"
    ON_HOLD = "on_hold"
    INCOMING = "incoming"


_DATED = {"in-progress", "in-progress tvc"}
_IN_PROGRESS = _DATED | {"airing"}
_ON_HOLD = {"standby", "archiving"}
_INCOMING = {"leads", "lead", "negotiations", "proposal made"}
"""Refine later with the CRM's own definition of a lead."""
_NEVER_SNAPSHOTTED = {"archived", "lost"}

ROLES = (
    "editor_ids",
    "assistant_editor_ids",
    "online_editor_ids",
    "colorist_ids",
    "sound_tech_ids",
    "post_coordinator_ids",
    "director_ids",
    "cinematographer_ids",
)
"""The Project attributes holding every required role, in report order."""

_HEALTH_ORDER = {"critical": 0, "at risk": 1, "on track": 2, "incoming": 3}


def status_key(status: object) -> str:
    """A Status or Stage for comparing: emoji and punctuation removed, lower case."""
    return " ".join(re.sub(r"[^\w\s-]", "", str(status or "")).split()).lower()


def status_gate(status: object) -> StatusGroup | None:
    """The group a Project's Status falls in, or None (the Project is dropped)."""
    key = status_key(status)
    if key in _IN_PROGRESS:
        return StatusGroup.IN_PROGRESS
    if key in _ON_HOLD:
        return StatusGroup.ON_HOLD
    if key in _INCOMING:
        return StatusGroup.INCOMING
    return None


def is_snapshotted(status: object) -> bool:
    """Archived and Lost Projects are never snapshotted."""
    return status_key(status) not in _NEVER_SNAPSHOTTED


@dataclass(frozen=True, slots=True)
class DateFlag:
    """One date problem. `field` is a Project attribute name."""

    kind: str
    """`missing`, `overdue` (`days` is how late) or `upcoming` (`days` is how far, 0 is today)."""
    field: str
    days: int | None = None


def date_flags(
    project: Project, today: dt.date, lead_warning_days: int = DEFAULT_LEAD_WARNING_DAYS
) -> tuple[DateFlag, ...]:
    """Date flags for a Project's Status, overdue ones first. `today` is the Manila day.

    In-Progress and In-Progress TVC: blank or overdue Playdate and Submission
    Deadline. Airing: blank ones, never overdue. Standby: none. Archiving: blank
    or overdue Archival Deadline. Incoming: an overdue Playdate, or one less
    than `lead_warning_days` away. Overdue means strictly before `today`.
    """
    key = status_key(project.status)
    flags: list[DateFlag] = []

    def check(name: str, *, blank: bool, late: bool) -> None:
        when = _day(getattr(project, name))
        if when is None:
            if blank:
                flags.append(DateFlag("missing", name))
        elif late and when < today:
            flags.append(DateFlag("overdue", name, (today - when).days))

    if key in _IN_PROGRESS:
        for name in ("playdate", "submission_deadline"):
            check(name, blank=True, late=key != "airing")
    elif key == "archiving":
        check("archival_deadline", blank=True, late=True)
    elif key in _INCOMING:
        check("playdate", blank=False, late=True)
        when = _day(project.playdate)
        if when is not None and 0 <= (when - today).days < lead_warning_days:
            flags.append(DateFlag("upcoming", "playdate", (when - today).days))
    return tuple(sorted(flags, key=lambda flag: flag.kind != "overdue"))


def missing_roles(project: Project) -> tuple[str, ...]:
    """The required roles (Project attribute names) with nobody assigned.

    Only In-Progress, In-Progress TVC and Airing Projects are checked.
    """
    if status_key(project.status) not in _IN_PROGRESS:
        return ()
    return tuple(role for role in ROLES if not getattr(project, role))


def health_missing(project: Project) -> bool:
    """A blank Health is flagged, whatever the Status."""
    return not status_key(project.health)


def health_rank(health: str | None) -> int:
    """Sort key: Critical, At Risk, On Track, Incoming, any other value, then blank."""
    key = status_key(health)
    if not key:
        return len(_HEALTH_ORDER) + 1
    return _HEALTH_ORDER.get(key, len(_HEALTH_ORDER))


def code_labels(projects: Iterable[Project]) -> dict[str, str]:
    """Each Project's display code, keyed by `key`.

    The code in capitals, or "CODE (Name)" when several of `projects` share it.
    A Project with no code shows its name.
    """
    listed = list(projects)
    counts: dict[str, int] = {}
    for project in listed:
        if project.project_code:
            code = project.project_code.upper()
            counts[code] = counts.get(code, 0) + 1
    labels: dict[str, str] = {}
    for project in listed:
        code = (project.project_code or "").upper()
        if not code:
            labels[project.key] = project.name
        elif counts[code] > 1:
            labels[project.key] = f"{code} ({project.name})"
        else:
            labels[project.key] = code
    return labels


def _day(value: dt.date | dt.datetime | None) -> dt.date | None:
    if isinstance(value, dt.datetime):
        return value.astimezone(MANILA).date() if value.tzinfo else value.date()
    return value
