"""Studio rules for Job Orders: header, names, keys, deliverable codes, formatter defaults, status groups.

Pure functions. Ported from the JO watchdog (`header.py`, `duplicates.py`,
the Telegram key) and t6-notion-reports (JO ID, status groups, health flags),
and from the Job Order formatter skill's rules. Each rule is listed in
`CONTEXT.md`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from t6.domain import JobOrderStatus

__all__ = [
    "DEPARTMENT_ALIASES",
    "HEALTH_FLAGS",
    "MFG_SHUTTLES",
    "WATERMARK_DEFAULT",
    "Header",
    "JobOrderStatusGroup",
    "canonical_name",
    "health_flags",
    "is_active",
    "is_deliverable_code",
    "is_mfg_shuttle",
    "jo_id",
    "jo_key",
    "needs_link_settings",
    "normalize_department",
    "parse_header",
    "parse_number",
    "parse_telegram_key",
    "status_group",
    "telegram_key",
]

DEPARTMENT_ALIASES: Mapping[str, str] = {"TO": "TOC"}
"""Short forms of a Department that people type, mapped to the Department name."""

# `JO #74`, `JO No. 15`, `#0155`, `JOB ORDER 27`, `JO01`. A dash between the
# label and the digits (`JOB ORDER - 033`) is accepted because the header split
# keeps everything after the department token as the number part.
_NUMBER = re.compile(r"(?:JOB\s+ORDER|JO)?\s*-?\s*(?:No\.?|#)?\s*(\d+)", re.IGNORECASE)
_SEPARATOR = re.compile(r"\s*-\s*")
# A `JO CODE:` label is not part of the project token (`JO CODE: INA-TOC-30`).
_LABEL = re.compile(r"^\s*JO\s+CODE\s*:\s*", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Header:
    """A fully parsed header: all three tokens are present."""

    project: str
    department: str
    number: str
    """The digits as posted, leading zeros kept."""

    @property
    def jo_number(self) -> str:
        return f"#{self.number}"

    @property
    def jo_number_int(self) -> int:
        return int(self.number)


def parse_number(text: str | None) -> str | None:
    """The digits of a JO number part as posted, or None."""
    if not text:
        return None
    match = _NUMBER.fullmatch(text.strip())
    return match.group(1) if match else None


def normalize_department(token: str, aliases: Mapping[str, str] = DEPARTMENT_ALIASES) -> str:
    """Look the token up in the alias map (ignoring case), then upper-case it."""
    token = token.strip()
    folded = {key.strip().upper(): value for key, value in aliases.items()}
    return folded.get(token.upper(), token).strip().upper()


def parse_header(header_raw: str | None, aliases: Mapping[str, str] = DEPARTMENT_ALIASES) -> Header | None:
    """Parse `<PROJECT> - <DEPT> - JO #<N>` into a Header, or None.

    A stray leading backslash (a Telegram markdown-escape artifact) is dropped
    from the project token; everything else is kept as posted.
    """
    if not header_raw:
        return None
    parts = _SEPARATOR.split(_LABEL.sub("", header_raw.strip()), maxsplit=2)
    if len(parts) != 3:
        return None
    project, department, number_part = (part.strip() for part in parts)
    project = project.lstrip("\\").strip()
    digits = parse_number(number_part)
    if not project or not department or digits is None:
        return None
    return Header(project=project, department=normalize_department(department, aliases), number=digits)


def canonical_name(header: Header | None, fallback: str, department: str | None = None) -> str:
    """`<project> - <DEPT> - JO #<digits>`, or `fallback` when it can't be built.

    `department` is the validated live option, used verbatim. None means it was
    not validated, so the header's own token is accepted.
    """
    if header is None:
        return fallback
    return f"{header.project} - {department or header.department} - JO #{header.number}"


def jo_key(project_page_id: str | None, number: int | None) -> str | None:
    """`<Project page ID>|<number>`, only when both are known.

    There is one JO counter per Project, shared by every Department, so the
    Department is not part of the key.
    """
    if not project_page_id or number is None:
        return None
    return f"{project_page_id}|{number}"


def jo_id(project_codes: Sequence[str], number: str | None) -> str:
    """`<code>#<number>`, with one "#" even when the number has its own.

    Several Projects are joined with "/"; none gives "no project"; no number
    gives "#?".
    """
    digits = (number or "").strip().lstrip("#").strip()
    return f"{'/'.join(project_codes) or 'no project'}#{digits or '?'}"


def telegram_key(chat_id: int | str, message_id: int | str) -> str:
    """`<chat_id>:<message_id>`: the Telegram message a Job Order came from."""
    return f"{chat_id}:{message_id}"


def parse_telegram_key(key: str | None) -> tuple[int, int] | None:
    """The `(chat_id, message_id)` of a Telegram Key, or None when it is not one."""
    chat, separator, message = (key or "").rpartition(":")
    if not separator:
        return None
    try:
        return int(chat), int(message)
    except ValueError:
        return None


_DELIVERABLE = re.compile(r"(?:FTR|TLR|TSR|SHR|Ep\d{2}(?:-\d{2})?)", re.IGNORECASE)


def is_deliverable_code(text: str) -> bool:
    """FTR (feature), TLR (trailer), TSR (teaser), SHR (short), EpXX or EpXX-XX (episodes)."""
    return _DELIVERABLE.fullmatch(text.strip()) is not None


WATERMARK_DEFAULT = "none"
"""What the formatter writes for Watermark when none is given."""

MFG_SHUTTLES = tuple(f"MFG Shuttle {number:02d}" for number in range(1, 5))
"""My Father's Girlfriend shuttle drives: 01 to 04, rotating. There is no 05."""


def is_mfg_shuttle(name: str) -> bool:
    return name.strip() in MFG_SHUTTLES


def needs_link_settings(mode_of_transfer: str | None, link_settings: str | None) -> bool:
    """Link Settings travels with Mode of Transfer: true when one is set and the other is blank."""
    return bool((mode_of_transfer or "").strip()) and not (link_settings or "").strip()


class JobOrderStatusGroup(StrEnum):
    NOT_STARTED = "not_started"
    STARTED = "started"
    CANCELLED = "cancelled"


_GROUPS = {
    JobOrderStatus.NEEDS_INTAKE_REVIEW: JobOrderStatusGroup.NOT_STARTED,
    JobOrderStatus.PENDING: JobOrderStatusGroup.NOT_STARTED,
    JobOrderStatus.IN_PROGRESS: JobOrderStatusGroup.STARTED,
    JobOrderStatus.NEEDS_COMPLETION_REVIEW: JobOrderStatusGroup.STARTED,
    JobOrderStatus.DONE: JobOrderStatusGroup.STARTED,
    JobOrderStatus.CANCELLED: JobOrderStatusGroup.CANCELLED,
}


def status_group(status: str | None) -> JobOrderStatusGroup | None:
    """Not started (Needs Intake Review, Pending), started (In progress, Needs
    Completion Review, Done) or cancelled. None for a blank or unknown status."""
    try:
        return _GROUPS[JobOrderStatus(status or "")]
    except ValueError:
        return None


def is_active(status: str | None) -> bool:
    """A Job Order that is neither Done nor Cancelled."""
    return status not in (JobOrderStatus.DONE, JobOrderStatus.CANCELLED)


HEALTH_FLAGS = ("Blocked", "Delayed", "Urgent")
"""The Health values that show on a report, worst first."""


def health_flags(health: str | None) -> tuple[str, ...]:
    """The flags (Blocked, Delayed, Urgent) in a Job Order's Health text, ignoring emoji."""
    text = (health or "").lower()
    return tuple(flag for flag in HEALTH_FLAGS if flag.lower() in text)
