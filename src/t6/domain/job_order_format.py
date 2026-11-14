"""The Job Order formatter's rules as a pure function: tidy a drafted JO into the studio layout.

`format_job_order` takes a drafted Job Order (a header line, then `Label: value`
lines, then free text) and returns it in the studio layout:

- line 1 is the canonical header `<PROJECT> - <DEPT> - JO #<N>` (any accepted
  number form is rewritten); line 2 is the date line, `July 23 / Thursday`,
  computed from the date given, never typed;
- the core fields follow in order. Labels are kept as written (`Source Drive`,
  `Notes`), because the wording matches the task; a field that is missing is
  not invented, except Watermark (defaults to "none"), and Status and Pending,
  which are always present and blank;
- a field's continuation lines stay with it; text after Assigned or cc, and
  text before any field, is the free body and goes last;
- what the formatter cannot settle is a finding, never a guess: an unparsed
  header, a missing Title, Task, Purpose or Assigned, a Title with no
  deliverable code, Mode of Transfer with no Link Settings, and an MFG shuttle
  drive outside 01 to 04 (in an MFG Job Order), and a date line that differs
  from the date given.

Deadlines are left as written; `t6.domain.deadlines` resolves them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from t6.domain.dateformats import formatter_date
from t6.domain.deadlines import Finding
from t6.domain.job_order_rules import (
    MFG_SHUTTLES,
    WATERMARK_DEFAULT,
    canonical_name,
    is_deliverable_code,
    needs_link_settings,
    parse_header,
)

__all__ = ["Formatted", "format_job_order"]

FIELDS: tuple[tuple[str, str, str], ...] = (
    ("title", "Title", r"Title"),
    ("task", "Task", r"Tasks?"),
    ("purpose", "Purpose", r"Purpose"),
    ("source", "Source", r"Source(?: Material| Drive)?|Project File"),
    ("watermark", "Watermark", r"Watermark"),
    ("subtitle", "Subtitle", r"Subtitles?"),
    ("mode", "Mode of Transfer", r"Mode of Transfer|Transfer Modes?(?:\(s\))?|Platform"),
    ("link_settings", "Link Settings", r"Link Settings"),
    ("deadline", "Deadline", r"Deadline"),
    ("status", "Status", r"Status"),
    ("pending", "Pending", r"Pending"),
    ("notes", "Notes", r"Notes?|Instructions|Note\(s\) ?/ ?Instructions"),
    ("assigned", "Assigned", r"Assigned"),
    ("cc", "cc", r"cc"),
)
SINGLE_LINE = frozenset({"status", "pending", "assigned", "cc"})
"""Fields whose value is one line; text after them is the free body."""
REQUIRED = ("title", "task", "purpose", "assigned")

_LABEL = re.compile(r"^\s*(" + "|".join(f"(?P<{key}>{pattern})" for key, _, pattern in FIELDS) + r")\s*:\s*(?P<value>.*)$", re.IGNORECASE)
_DATE_LINE = re.compile(r"^[A-Za-z]+ \d{1,2} / [A-Za-z]+$")
_SHUTTLE = re.compile(r"MFG Shuttle (\d+)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Formatted:
    text: str
    findings: tuple[Finding, ...] = ()


def format_job_order(draft: str, day: date) -> Formatted:
    """The studio layout of `draft`, dated `day`, and the findings that need a person."""
    lines = draft.replace("\xa0", " ").splitlines()
    nonblank = [i for i, line in enumerate(lines) if line.strip()]
    findings: list[Finding] = []
    if not nonblank:
        return Formatted("", (Finding("empty_draft"),))
    header_raw = lines[nonblank[0]].strip()
    rest = lines[nonblank[0] + 1 :]
    old_date = next((line.strip() for line in rest if line.strip()), "")
    while rest and (not rest[0].strip() or _DATE_LINE.match(rest[0].strip())):
        rest = rest[1:]  # the date line is rewritten from `day`
    header = parse_header(header_raw)
    if header is None:
        findings.append(Finding("header_unparsed", {"raw": header_raw}))

    labels: dict[str, str] = {}
    values: dict[str, list[str]] = {}
    body: list[str] = []
    current: str | None = None
    order = [k for k, _, _ in FIELDS]
    for line in rest:
        match = _LABEL.match(line)
        key = next((k for k in order if match and match.group(k)), None) if match else None
        # Once Notes has started, only the fields after it (Assigned, cc) start a field: a line inside the
        # notes that looks like a label, such as `Source: ...`, stays part of the notes.
        late = current is None or order.index(key or "") > order.index(current) if key else False
        if match and key and key not in values and late:
            labels[key] = line.strip().split(":", 1)[0].strip()
            values[key] = [match.group("value").strip()]
            current = None if key in SINGLE_LINE else key
        elif current is not None:
            values[current].append(line.rstrip())
        else:
            body.append(line.rstrip())
    for text in values.values():
        while len(text) > 1 and not text[-1].strip():
            text.pop()

    findings += [Finding(f"missing_{key}") for key in REQUIRED if not "".join(values.get(key, [])).strip()]
    title = " ".join(values.get("title", [""])).strip()
    if title and not is_deliverable_code(title.split()[-1]):
        findings.append(Finding("no_deliverable_code", {"title": title}))
    mode = " ".join(values.get("mode", [""]))
    link = " ".join(values.get("link_settings", [""]))
    if needs_link_settings(mode, link):
        findings.append(Finding("link_settings_missing"))
    if header is not None and header.project.upper() == "MFG":
        for number in dict.fromkeys(_SHUTTLE.findall(draft)):
            if f"MFG Shuttle {int(number):02d}" not in MFG_SHUTTLES or len(number) > 2:
                findings.append(Finding("mfg_shuttle_out_of_range", {"drive": f"MFG Shuttle {number}"}))

    values.setdefault("watermark", [WATERMARK_DEFAULT])
    if not "".join(values["watermark"]).strip():
        values["watermark"] = [WATERMARK_DEFAULT]
    date_line = formatter_date(day)
    if _DATE_LINE.match(old_date) and old_date != date_line:
        findings.append(Finding("date_line_changed", {"was": old_date, "now": date_line}))
    out = [canonical_name(header, header_raw), date_line, ""]
    for key, default_label, _ in FIELDS:
        if key == "assigned" and key in values:
            out.append("")
        if key in values or key in ("status", "pending"):
            first, *more = values.get(key, [""])
            out.append(f"{labels.get(key, default_label)}: {first}".rstrip())
            out += more
    while out and not out[-1].strip():
        out.pop()
    free_text = "\n".join(body).strip("\n")
    if free_text.strip():
        out += ["", free_text]
    return Formatted("\n".join(out) + "\n", tuple(findings))
