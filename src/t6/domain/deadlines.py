"""Deterministic deadline resolution in Asia/Manila time.

A hand-rolled phrase parser that fails closed: it never fills a missing
component implicitly. Rules:

- a stated time is used as is; a date with no time means 23:59;
- "end of shift", "within shift" and "asap" use the latest assignee end of
  shift at or after the posting time; a morning end of shift rolls to the next
  day when a date is stated; no fixed shift means 23:59 with a fallback finding;
- "asap" also sets the `asap` finding;
- a missing year takes the closest year; an explicit year is honoured;
- hedges (`if keri`, `kung ...`, parentheses) and fillers are stripped;
- a deadline before the posting time is kept and flagged `deadline_past`;
- anything else, including `tomorrow`, is `deadline_unparsed`, never a guess.

Ported from the JO watchdog's `deadline.py`, with its tests as contract tests.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Literal

from t6.domain import Contact
from t6.domain.studio_time import END_OF_DAY, MANILA

__all__ = ["ASAP", "DEADLINE_PAST", "DEADLINE_UNPARSED", "SHIFT_FALLBACK", "DeadlineResult", "Finding", "deadline", "strip_hedges"]

ASAP = "asap"
DEADLINE_PAST = "deadline_past"
DEADLINE_UNPARSED = "deadline_unparsed"
SHIFT_FALLBACK = "shift_fallback"

DeadlineSource = Literal["stated", "shift", "fallback", "blank"]

_PLACEHOLDERS = {"-", "n/a", "na", "none"}

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}  # fmt: skip
_DATE = re.compile(
    r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
    r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?\s+(\d{1,2})\b"
    # An optional posted year (`September 16, 2020`); `1130 PM` is a time, not a year.
    r"(?:,?\s+(20\d{2})\b(?!\s*[ap]m\b))?",
    re.IGNORECASE,
)
# `5AM`, `5:00 AM`, `7pm`, `530PM` (= 17:30), `12:00 AM` (= 00:00).
_TIME = re.compile(r"\b(\d{1,2})(?::?(\d{2}))?\s*(am|pm)\b", re.IGNORECASE)
_WEEKDAY = re.compile(
    r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday"
    r"|mon|tues?|wed|thu(?:rs?)?|fri|sat|sun)\b",
    re.IGNORECASE,
)
_SHIFT = re.compile(r"\b(end of shift|within shift|asap)\b", re.IGNORECASE)
_DAY_END = re.compile(r"\b(tonight|today|within the day)\b", re.IGNORECASE)
_BARE_PM = re.compile(r"\bpm\b", re.IGNORECASE)

_PARENTHETICAL = re.compile(r"\([^)]*\)?")
_LEADING_HEDGE = re.compile(r"^\s*(?:if|kung)\s+\S+", re.IGNORECASE)
_HEDGE = re.compile(r"\b(?:if|kung)\b|\sor\s", re.IGNORECASE)
_FILLER = re.compile(r"\b(?:rin|po|na)\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Finding:
    """Something a person should know about a deadline. `params` are plain JSON values."""

    code: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DeadlineResult:
    deadline: datetime | None = None
    """An aware datetime in Manila, or None when the phrase is blank or unreadable."""
    source: DeadlineSource = "blank"
    """`stated` (the phrase said it), `shift` (an end of shift), `fallback` (23:59) or `blank`."""
    findings: list[Finding] = field(default_factory=list)


def strip_hedges(raw: str) -> str:
    """Remove hedges and filler words; the caller keeps the original for Notes.

    A hedge is a parenthetical, or text starting at ``if``, ``kung`` or `` or ``.
    A leading hedge (``If keri rin po today``) drops only its marker and the
    word after it, so the phrase it qualifies survives.
    """
    text = _PARENTHETICAL.sub(" ", raw)
    text = _LEADING_HEDGE.sub(" ", text, count=1)
    cut = _HEDGE.search(text)
    if cut:
        text = text[: cut.start()]
    text = _FILLER.sub(" ", text)
    return " ".join(text.split())


def _closest_year(month: int, day: int, posted: date) -> date | None:
    candidates = []
    for year in (posted.year - 1, posted.year, posted.year + 1):
        try:
            candidates.append(date(year, month, day))
        except ValueError:
            continue
    if not candidates:
        return None
    return min(candidates, key=lambda d: abs((d - posted).days))


def _stated_date(month: int, day: int, year: str | None, posted_at: datetime) -> date | None:
    """An explicit year is honored; the closest year applies only when it is absent."""
    if year is None:
        return _closest_year(month, day, posted_at.date())
    try:
        return date(int(year), month, day)
    except ValueError:
        return None


def _parse_time(match: re.Match[str]) -> time | None:
    hour, minute = int(match.group(1)), int(match.group(2) or 0)
    if not 1 <= hour <= 12 or minute > 59:
        return None
    if match.group(3).lower() == "am":
        hour = 0 if hour == 12 else hour
    else:
        hour = 12 if hour == 12 else hour + 12
    return time(hour, minute)


def _at(day: date, at: time) -> datetime:
    return datetime.combine(day, at, tzinfo=MANILA)


def _resolve_shift(
    posted_at: datetime,
    stated: date | None,
    assignees: Sequence[Contact],
) -> tuple[datetime, DeadlineSource, list[Finding]]:
    """The latest end of shift across assignees, else the 23:59 fallback."""
    candidates: list[datetime] = []
    defaulted: list[str] = []
    for contact in assignees:
        hour = contact.end_of_shift
        if hour is None:
            defaulted.append(contact.name)
            continue
        if stated is None:
            occurrence = _at(posted_at.date(), hour)
            if occurrence < posted_at:
                occurrence += timedelta(days=1)
        else:
            # A shift ending in the morning ends the night that starts on `stated`.
            day = stated + timedelta(days=1) if hour < time(12) else stated
            occurrence = _at(day, hour)
        candidates.append(occurrence)

    if candidates:
        return max(candidates), "shift", []

    params: dict[str, Any] = {"defaulted": defaulted} if defaulted else {"reason": "no assignee resolved"}
    fallback = _at(stated or posted_at.date(), END_OF_DAY)
    return fallback, "fallback", [Finding(code=SHIFT_FALLBACK, params=params)]


def deadline(
    phrase: str | None,
    posted_at: datetime,
    assignees: Sequence[Contact] = (),
    *,
    person: dict[str, Any] | None = None,
) -> DeadlineResult:
    """Resolve a posted deadline phrase to an Asia/Manila datetime.

    `posted_at` is the intake posting (or forwarding) time. `assignees` are the
    resolved assignee Contacts, whose `end_of_shift` decides "end of shift";
    an empty sequence means no assignee resolved.
    """
    posted_at = posted_at.astimezone(MANILA)
    original = " ".join((phrase or "").replace("\xa0", " ").split())
    if not original or original.lower() in _PLACEHOLDERS:
        return DeadlineResult()

    text = strip_hedges(original)

    stated: date | None = None
    date_match = _DATE.search(text)
    if date_match:
        month = _MONTHS[date_match.group(1).lower()[:3]]
        stated = _stated_date(month, int(date_match.group(2)), date_match.group(3), posted_at)
        if stated is None:
            return _unparsed(original)
        text = text[: date_match.start()] + " " + text[date_match.end() :]
    text = _WEEKDAY.sub(" ", text)

    at: time | None = None
    time_match = _TIME.search(text)
    if time_match:
        at = _parse_time(time_match)
        if at is None:
            return _unparsed(original)
        text = text[: time_match.start()] + " " + text[time_match.end() :]

    shift_match = _SHIFT.search(text)
    day_end = _DAY_END.search(text) or (at is None and _BARE_PM.search(text))

    findings: list[Finding] = []
    if shift_match and shift_match.group(1).lower() == "asap":
        findings.append(Finding(code=ASAP))

    source: DeadlineSource
    if at is not None:
        when, source = _at(stated or posted_at.date(), at), "stated"
    elif shift_match:
        shift = person.get("shift") if person else None
        if shift:
            end = time.fromisoformat(shift["end"])
            if stated is not None:
                day = stated + timedelta(days=1) if shift["pattern"] == "night" else stated
                when = _at(day, end)
            else:
                when = _at(posted_at.date(), end)
                if when < posted_at or (shift["pattern"] == "night" and posted_at.time() >= time.fromisoformat(shift["start"])):
                    when += timedelta(days=1)
            source = "shift"
            shift_findings: list[Finding] = []
        else:
            when, source, shift_findings = _resolve_shift(posted_at, stated, assignees)
        findings.extend(shift_findings)
    elif day_end or stated is not None:
        # A date followed by a no-clock phrase is still 23:59.
        when, source = _at(stated or posted_at.date(), END_OF_DAY), "stated"
    else:
        return _unparsed(original)

    if stated is None and re.sub(r"[\s/,.\-]+", "", _leftover(text)):
        # Without a date, unrecognised words could change the day (`tomorrow`).
        return _unparsed(original)

    if when < posted_at:
        findings.append(Finding(code=DEADLINE_PAST, params={"deadline": when.isoformat()}))
    return DeadlineResult(deadline=when, source=source, findings=findings)


def _leftover(text: str) -> str:
    for pattern in (_SHIFT, _DAY_END, _BARE_PM):
        text = pattern.sub(" ", text)
    return text


def _unparsed(original: str) -> DeadlineResult:
    return DeadlineResult(findings=[Finding(code=DEADLINE_UNPARSED, params={"raw": original})])
