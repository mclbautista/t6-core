"""`jo draft`: a code-only Job Order draft from one line (spec #791, ticket #794).

The server gathers the Project, its Job Orders, the assignee and the standing cc,
then `build_draft` lays the draft out with the Job Order formatter. No model runs
here and nothing is written. Unknown fields are "(?)".
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from t6.domain.deadlines import deadline as resolve_deadline
from t6.domain.job_order_format import format_job_order
from t6.domain.job_order_rules import is_deliverable_code, parse_number
from t6.privacy import withhold

__all__ = ["Line", "Steps", "build_draft", "candidate_text", "classify_answer", "fixed_source", "haiku_input", "line_deadline", "merge_fields", "other_names", "next_number", "parse_line", "pick_candidates", "pick_match", "questions"]

UNKNOWN = "(?)"
MAX_CANDIDATES = 12
_WORD = re.compile(r"[^\W_]{3,}")
_STOP = frozenset({"the", "and", "for", "with", "from", "that", "this", "all", "new", "per", "into", "our"})
_BY = re.compile(r"\b(?:by|before|until)\s+(?P<when>\S.*?)\s*$", re.IGNORECASE)
_NONE = frozenset({"none", "no deadline", "-", "n/a", "na"})
_FIXED_PLACE = re.compile(r"\b(?:drive|pix|share)\b", re.IGNORECASE)
_FOR_TAIL = re.compile(r"[\s,;]+for\s+(?P<name>[^\W\d_][\w'’.\-]*(?:\s+[^\W\d_][\w'’.\-]*)?)\s*[.!]?\s*$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Line:
    code: str
    """The first word, the Project Code."""
    task: str
    """The rest of the line, without the `for X` tail."""
    assignee: str | None
    deliverable: str | None
    """The deliverable code as it goes in Title (`FTR`, `Ep07`), or None when the line has none."""


def parse_line(line: str) -> Line | None:
    """Split `<CODE> <task words> [, for <Name>]`. None when the line is blank."""
    words = line.strip().split(None, 1)
    if not words:
        return None
    code, rest = words[0], (words[1] if len(words) > 1 else "")
    tail = _FOR_TAIL.search(" " + rest)
    assignee = tail.group("name").strip() if tail else None
    task = (" " + rest)[: tail.start()] if tail else rest
    task = task.strip(" ,;")
    deliverable = next(
        (_deliverable(token) for token in re.findall(r"[\w\-]+", task) if is_deliverable_code(token)),
        None,
    )
    return Line(code=code, task=task, assignee=assignee, deliverable=deliverable)


def _deliverable(token: str) -> str:
    return "Ep" + token[2:] if token.lower().startswith("ep") else token.upper()


def _words(text: str | None) -> set[str]:
    return {w for w in _WORD.findall((text or "").casefold()) if w not in _STOP}


def candidate_text(order: Mapping[str, Any]) -> str:
    """The three fields Jev sees for a past Job Order: Task, Purpose, Mode of Transfer."""
    return "\n".join(
        f"{label}: {order.get(key) or ''}"
        for label, key in (("Task", "task"), ("Purpose", "purpose"), ("Mode of Transfer", "mode_of_transfer"))
    )


def pick_candidates(task: str, orders: Sequence[Mapping[str, Any]], limit: int = MAX_CANDIDATES) -> list[Mapping[str, Any]]:
    """Up to `limit` past Job Orders sharing words with the line's task, most shared words first.

    Ties keep the order given. Orders sharing no word are left out.
    """
    wanted = _words(task)
    scored = [(len(wanted & _words(candidate_text(o))), i, o) for i, o in enumerate(orders)]
    scored = [row for row in scored if row[0] > 0]
    scored.sort(key=lambda row: (-row[0], row[1]))
    return [o for _, _, o in scored[:limit]]


def pick_match(answers: Sequence[Mapping[str, Any]]) -> int | None:
    """The index of the answer that says `match` with the highest probability, or None."""
    best: tuple[float, int] | None = None
    for answer in answers:
        if answer.get("choice") != "match":
            continue
        score = float((answer.get("probabilities") or {}).get("match", 0.0))
        if best is None or score > best[0]:
            best = (score, int(answer["index"]))
    return best[1] if best else None


def fixed_source(text: str | None) -> str | None:
    """Source as a fixed place (a drive, PIX or a share), never a link."""
    text = (text or "").strip()
    return text if text and "://" not in text and _FIXED_PLACE.search(text) else None


def _states_deadline(phrase: str, posted_at: datetime) -> bool:
    return resolve_deadline(phrase, posted_at).deadline is not None


def line_deadline(task: str, posted_at: datetime) -> str | None:
    """The deadline the line states as `by|before|until <when>`, when the deadline rules can read it."""
    match = _BY.search(task)
    return match.group("when") if match and _states_deadline(match.group("when"), posted_at) else None


def classify_answer(answer: str | None, posted_at: datetime) -> tuple[str | None, str | None]:
    """`(deadline, purpose)` from a reply to the question line: "none" or a readable deadline is the
    Deadline as written, anything else is the Purpose."""
    text = " ".join((answer or "").split())
    if not text:
        return None, None
    if text.casefold() in _NONE or _states_deadline(text, posted_at):
        return text, None
    return None, text


def questions(*, deadline: str | None, purpose: str | None) -> list[str]:
    """The question line's words: Deadline when none is known, Purpose when it is unknown."""
    return [f"{name}?" for name, value in (("Deadline", deadline), ("Purpose", purpose)) if not value]


_NAME = re.compile(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?\b")


def other_names(task: str, assignee: str | None) -> list[str]:
    """Capitalised names in the task other than the assignee's, in the order they appear."""
    skip = {w.casefold() for w in (assignee or "").split()}
    found: list[str] = []
    for match in _NAME.finditer(task):
        name = match.group(0)
        if not any(w.casefold() in skip for w in name.split()) and name not in found:
            found.append(name)
    return found


_PAST_FIELDS = (("Department", "department"), ("Purpose", "purpose"), ("Source", "source"), ("Watermark", "watermark"),
                ("Mode of Transfer", "mode_of_transfer"), ("Link Settings", "link_settings"), ("Notes", "notes"))
_HAIKU_KEYS = ("purpose", "source", "watermark", "mode_of_transfer", "link_settings", "notes")


def _past_text(order: Mapping[str, Any], heading: str) -> list[str]:
    return [heading, *(f"{label}: {order.get(key) or ''}" for label, key in _PAST_FIELDS)]


def haiku_input(
    *, line: Line, project_title: str, number: int, copied: Mapping[str, Any] | None, assigned_name: str | None,
    extra_person: str | None = None, extra_past: Mapping[str, Any] | None = None,
) -> str:
    """The one message the formatter skill gets: the request, what is settled, the closest past Job Order and extra lookups.

    Plain text, masked and credential-free later by the runner. Nothing here is a link the skill may copy.
    """
    parts = [f"Request: {line.task}", f"Project title: {project_title}", f"JO number: {number}"]
    if assigned_name:
        parts.append(f"Assigned: {assigned_name}")
    parts += _past_text(copied, "Closest past Job Order:") if copied else ["Closest past Job Order: none"]
    if extra_person:
        parts.append(f"Another person named in the request: {extra_person}")
    if extra_past:
        parts += _past_text(extra_past, "One more past Job Order:")
    return "\n".join(parts)


def merge_fields(copied: Mapping[str, Any] | None, fields: Mapping[str, Any]) -> dict[str, Any]:
    """The code's copy with the skill's fields over it: a field the skill leaves empty or "(?)" keeps the copy's value."""
    merged = dict(copied or {})
    for key in _HAIKU_KEYS:
        value = " ".join(str(fields.get(key) or "").split())
        if value and value != UNKNOWN:
            merged[key] = value
    return merged


def next_number(numbers: Iterable[tuple[str | None, str | None]], taken: Callable[[int], bool]) -> tuple[int, str | None]:
    """The highest JO number plus one, stepped up while `taken`, and a note on how it was picked.

    `numbers` are `(number text, department)` of the Project's Job Orders. Gaps stay gaps.
    """
    best: tuple[int, str | None] | None = None
    for text, department in numbers:
        digits = parse_number(text)
        if digits is not None and (best is None or int(digits) > best[0]):
            best = (int(digits), department)
    number = best[0] + 1 if best else 1
    while taken(number):
        number += 1
    if best is None:
        return number, None
    last = f"#{best[0]} {best[1]}".strip() if best[1] else f"#{best[0]}"
    return number, f"#{number} (last: {last})"


@dataclass(slots=True)
class Steps:
    """Wall time of each step, in milliseconds, in the order they ran."""

    rows: list[tuple[str, int]] = field(default_factory=list)

    def run(self, name: str, work: Callable[[], Any]) -> Any:
        started = time.monotonic()
        try:
            return work()
        finally:
            self.rows.append((name, round((time.monotonic() - started) * 1000)))

    def report(self) -> dict[str, Any]:
        """Ids, numbers and step names only: no JO text and no names. The total is the sum of the steps."""
        return {
            "total_ms": sum(ms for _, ms in self.rows),
            "steps": [{"step": name, "ms": ms} for name, ms in self.rows],
        }


def build_draft(
    *,
    line: Line,
    project_code: str,
    project_title: str,
    number: int,
    assigned_handle: str | None,
    cc_handle: str | None,
    today: date,
    copied: Mapping[str, Any] | None = None,
    deadline: str | None = None,
    purpose: str | None = None,
    task: str | None = None,
    subtitle: str | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """The formatted draft text and the formatter's findings.

    `copied` is the best-matching past Job Order. It fills Department, Purpose, Watermark,
    Mode of Transfer, Link Settings and Notes, Source when it is a fixed place, and the
    deliverable code when the line has none. Without it those stay "(?)".
    """
    copied = copied or {}

    def got(key: str) -> str:
        # Credentials and links in a copied field never reach the draft.
        return " ".join(withhold(str(copied.get(key) or "")).text.split()) or UNKNOWN

    deliverable = line.deliverable
    if not deliverable:
        deliverable = next(
            (_deliverable(t) for t in re.findall(r"[\w\-]+", str(copied.get("title") or "")) if is_deliverable_code(t)), None
        )
    title = f"{project_title} {deliverable}" if deliverable else project_title
    draft = "\n".join(
        [
            f"{project_code} - {got('department')} - JO #{number}",
            "",
            f"Title: {title}",
            f"Task: {task or line.task}",
            f"Purpose: {purpose or got('purpose')}",
            f"Source: {fixed_source(copied.get('source')) or UNKNOWN}",
            *([f"Subtitle: {subtitle}"] if subtitle else []),
            f"Mode of Transfer: {got('mode_of_transfer')}",
            f"Link Settings: {got('link_settings')}",
            f"Deadline: {deadline or UNKNOWN}",
            f"Notes: {got('notes')}",
            *([f"Watermark: {got('watermark')}"] if copied.get("watermark") else []),
            f"Assigned: {_handle(assigned_handle)}",
            f"cc: {_handle(cc_handle)}",
        ]
    )
    result = format_job_order(draft, today)
    return result.text, [{"code": f.code, "params": dict(f.params)} for f in result.findings]


def _handle(handle: str | None) -> str:
    if not handle or not handle.strip():
        return UNKNOWN
    handle = handle.strip()
    return handle if handle.startswith("@") else "@" + handle


def project_numbers(orders: Sequence[dict[str, Any]]) -> list[tuple[str | None, str | None]]:
    return [(o.get("number"), o.get("department")) for o in orders]
