"""Studio rules for Projects: the status gate, date flags, missing roles, Health order, code display.

Pure functions over `t6.domain.Project`. Status and Health are matched by their
words, ignoring emoji, case and extra spaces, so a decorated Notion option such
as "🚨 In-Progress" and a plain "in-progress" are the same status. What an app
does with a flag (wording, layout) stays in the app.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from t6.domain import CrmStage, Project
from t6.domain.dateformats import MANILA

__all__ = [
    "DEFAULT_LEAD_WARNING_DAYS",
    "DateFlag",
    "PsrGroup",
    "StatusGroup",
    "code_labels",
    "date_flags",
    "date_sanity",
    "health_missing",
    "health_rank",
    "is_snapshotted",
    "missing_roles",
    "psr_group",
    "relevant_dates",
    "reviewed_every_psr",
    "status_gate",
    "status_key",
    "status_unknown",
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
_INCOMING = {"leads", "lead", "negotiations", "proposal made", "onboarding"}
"""The CRM stages before a Project is In-Progress (`t6.domain.CrmStage`), Lost excepted."""
_NEVER_SNAPSHOTTED = {"archived", "lost"}
_IN_PROGRESS_DATES = ("playdate", "submission_deadline")
_ARCHIVING_DATES = ("archival_deadline",)
_INCOMING_DATES = ("playdate",)

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

_LIVE_STATUSES = {
    "in-progress", "leads", "onboarding", "standby", "in-progress tvc", "proposal made", "negotiations",
    "operations", "archiving", "archived", "lost", "malayo pa", "airing",
}
"""The 13 live Notion Status options, as `status_key` gives them."""
_ARCHIVING_STAGES = {"archiving", "for drives turnover", "waiting for payment", "festival rounds"}
PsrGroup = Literal["archiving", "incoming", "critical", "at_risk", "on_track"]
_HEALTH_GROUP: dict[str, PsrGroup] = {"critical": "critical", "at risk": "at_risk", "on track": "on_track"}
_REVIEWED_EVERY_PSR = {"in-progress", "airing"}


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


def crm_stage(recorded: str | None, status: object) -> CrmStage | None:
    """A Project's current CRM stage: the newest recorded stage, else the one its Notion Status names.

    A recorded label that is no CRM stage (In-Progress) means the CRM part has ended, whatever the Status says.
    """
    if recorded is not None:
        return next((stage for stage in CrmStage if stage.value == recorded), None)
    return next((stage for stage in CrmStage if status_key(stage.value) == status_key(status)), None)


def is_lead(stage: CrmStage | None) -> bool:
    """A Lead is a Project in a CRM stage other than Lost."""
    return stage is not None and stage is not CrmStage.LOST


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
        for name in _IN_PROGRESS_DATES:
            check(name, blank=True, late=key != "airing")
    elif key == "archiving":
        check(_ARCHIVING_DATES[0], blank=True, late=True)
    elif key in _INCOMING:
        check("playdate", blank=False, late=True)
        when = _day(project.playdate)
        if when is not None and 0 <= (when - today).days < lead_warning_days:
            flags.append(DateFlag("upcoming", "playdate", (when - today).days))
    return tuple(sorted(flags, key=lambda flag: flag.kind != "overdue"))


def relevant_dates(status: object) -> tuple[str, ...]:
    """The date properties (Project attribute names) that matter for a Status.

    The ones `date_flags` looks at: a blank value of one could be filled in from the PM Notes.
    """
    key = status_key(status)
    if key in _IN_PROGRESS:
        return _IN_PROGRESS_DATES
    if key == "archiving":
        return _ARCHIVING_DATES
    if key in _INCOMING:
        return _INCOMING_DATES
    return ()


def date_sanity(dates: Mapping[str, dt.date | dt.datetime | None]) -> tuple[tuple[str, str, str, dt.date, dt.date], ...]:
    """The date orderings that are broken, as `(kind, first, second, first_date, second_date)`.

    `dates` maps `playdate`, `submission_deadline` and `archival_deadline` to a date (a missing
    key is blank). `submission_before_playdate`: the Submission Deadline is before the Playdate.
    `archival_before_submission`: the Archival Deadline is before the Submission Deadline. `first`
    is the date that is too early, `second` the one it is compared with.
    """
    day = {name: _day(dates.get(name)) for name in ("playdate", "submission_deadline", "archival_deadline")}
    problems: list[tuple[str, str, str, dt.date, dt.date]] = []
    playdate, submission, archival = day["playdate"], day["submission_deadline"], day["archival_deadline"]
    if playdate and submission and submission < playdate:
        problems.append(("submission_before_playdate", "submission_deadline", "playdate", submission, playdate))
    if submission and archival and archival < submission:
        problems.append(("archival_before_submission", "archival_deadline", "submission_deadline", archival, submission))
    return tuple(problems)


def missing_roles(project: Project) -> tuple[str, ...]:
    """The required roles (Project attribute names) with nobody assigned.

    Only In-Progress, In-Progress TVC and Airing Projects are checked.
    """
    if status_key(project.status) not in _IN_PROGRESS:
        return ()
    return tuple(role for role in ROLES if not getattr(project, role))


def health_missing(project: Project) -> bool:
    """A blank Project Health is flagged, whatever the Status."""
    return not status_key(project.project_health)


def psr_group(project: Project) -> PsrGroup:
    """The PSR group a Project is shown in: `archiving`, `incoming`, `critical`, `at_risk` or `on_track`.

    First match wins. Status Archiving or an archiving Stage (Archiving, For Drives Turnover, Waiting for
    Payment, Festival Rounds) is `archiving`; an incoming status group is `incoming`; otherwise Project
    Health decides, and a blank or unknown Health is `on_track` (`health_missing` still flags a blank one).
    """
    if status_key(project.status) == "archiving" or status_key(project.stage) in _ARCHIVING_STAGES:
        return "archiving"
    if status_gate(project.status) is StatusGroup.INCOMING:
        return "incoming"
    return _HEALTH_GROUP.get(status_key(project.project_health), "on_track")


def reviewed_every_psr(project: Project) -> bool:
    """In-Progress, Airing and every `archiving` Project are reviewed at every PSR; In-Progress TVC is not."""
    return status_key(project.status) in _REVIEWED_EVERY_PSR or psr_group(project) == "archiving"


def status_unknown(status: object) -> bool:
    """The Status matches none of the 13 live options (a blank Status included). Archived and Lost are known."""
    return status_key(status) not in _LIVE_STATUSES


def health_rank(health: str | None) -> int:
    """Sort key of a Project Health: Critical, At Risk, On Track, Incoming, any other value, then blank."""
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
