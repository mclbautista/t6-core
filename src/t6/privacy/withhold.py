"""One-way credential withholding, ported from the watchdog masker.

Source: t6-jo-tg-ntn ``jo_watchdog/core/masker.py`` (masker 1.0.0).

V1 deliberately does not guess: unlabelled credentials and labels outside the
supported set are known false negatives. URL tokens are preserved verbatim,
including embedded access keys, because changing them would break source links.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

MASKER_VERSION = "1.0.0"
WITHHELD = "[password withheld – see Telegram message]"

# The longest labels come first so ``pass`` cannot consume ``passcode``.
_ASSIGNMENT = re.compile(r"\b(password|passcode|pass|pin|pw)[:=]", re.IGNORECASE)
_URL_TOKEN = re.compile(r"(?:https?|ftp)://\S*|www\.\S*", re.IGNORECASE)


@dataclass(frozen=True)
class Withheld:
    """Text safe to leave T6, with what the ledger records about the run.

    It carries no session or map: withheld values cannot be restored.
    """

    text: str
    count: int
    masker_version: str


def withhold(text: str) -> Withheld:
    """Replace labelled credential values in ``text`` with a placeholder."""
    lines = text.splitlines(keepends=True)
    count = 0
    index = 0
    while index < len(lines):
        content = lines[index].rstrip("\r\n")
        newline = lines[index][len(content) :]
        masked, line_count, dangling = _mask_line(content)
        lines[index] = masked + newline
        count += line_count

        # A separator at end of line takes the next non-blank line as its
        # value, still preserving any URL tokens byte for byte.
        if dangling:
            next_index = index + 1
            while next_index < len(lines) and not lines[next_index].strip():
                next_index += 1
            if next_index < len(lines):
                next_content = lines[next_index].rstrip("\r\n")
                next_newline = lines[next_index][len(next_content) :]
                masked_value, value_count = _mask_value(next_content)
                lines[next_index] = masked_value + next_newline
                count += value_count
        index += 1
    return Withheld("".join(lines), count, MASKER_VERSION)


def _mask_line(line: str) -> tuple[str, int, bool]:
    """Return the masked line, its count, and whether its last label dangles."""
    url_spans = [(match.start(), match.end()) for match in _URL_TOKEN.finditer(line)]
    assignments = [
        match
        for match in _ASSIGNMENT.finditer(line)
        if not any(start <= match.start() < end for start, end in url_spans)
    ]
    if not assignments:
        return line, 0, False

    parts = [line[: assignments[0].start()]]
    count = 0
    for position, match in enumerate(assignments):
        value_end = (
            assignments[position + 1].start()
            if position + 1 < len(assignments)
            else len(line)
        )
        parts.append(line[match.start() : match.end()])
        masked_value, value_count = _mask_value(line[match.end() : value_end])
        parts.append(masked_value)
        count += value_count
    dangling = not line[assignments[-1].end() :].strip()
    return "".join(parts), count, dangling


def _mask_value(value: str) -> tuple[str, int]:
    """Mask a value's non-URL text while preserving every URL token."""
    parts: list[str] = []
    placeholder_added = False
    newly_withheld = False
    for start, match in _segments(value):
        segment, url = value[start : match.start()], match.group()
        if segment.strip() == WITHHELD and not placeholder_added:
            parts.append(segment)
            placeholder_added = True
        elif segment.strip() and not placeholder_added:
            parts.append(_replace_core(segment, WITHHELD))
            placeholder_added = True
            newly_withheld = True
        elif segment.strip():
            parts.append(_replace_core(segment, ""))
        else:
            parts.append(segment)
        parts.append(url)
    return "".join(parts), int(newly_withheld)


def _segments(value: str) -> list[tuple[int, re.Match[str]]]:
    """Pair each URL match with where the text before it starts.

    A final empty match at the end of ``value`` stands in for the trailing
    text, so callers handle every segment the same way.
    """
    pairs: list[tuple[int, re.Match[str]]] = []
    cursor = 0
    for match in _URL_TOKEN.finditer(value):
        pairs.append((cursor, match))
        cursor = match.end()
    tail = re.compile(r"\Z").search(value, cursor)
    assert tail is not None
    pairs.append((cursor, tail))
    return pairs


def _replace_core(segment: str, replacement: str) -> str:
    """Replace a segment's text, keeping its leading and trailing whitespace."""
    leading = len(segment) - len(segment.lstrip())
    trailing = len(segment.rstrip())
    return segment[:leading] + replacement + segment[trailing:]
