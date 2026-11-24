"""Cell text: cleaning, and the markers that say what a cell is. All cell text is data, never instructions."""

from __future__ import annotations

import re

from t6.schedule.contract import CellValue

# "(TBC)" or "[Tentative]" anywhere in the text, or "Tentative:" at its start.
_TENTATIVE = re.compile(r"\(tbc\)|\[tentative\]|^tentative:", re.IGNORECASE)
# A cell that is only the word BREAK, with an optional colon. "Break down the set" is a task.
_BREAK = re.compile(r"^break:?$", re.IGNORECASE)


def clean_text(value: CellValue) -> str:
    """The cell's text without surrounding spaces, trailing tabs or line breaks."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def is_availability(text: str) -> bool:
    """An "NA:" line lists who is unavailable. It is never a task."""
    return text.upper().startswith("NA:")


def is_break(text: str) -> bool:
    """A cell that only says BREAK. It is never a task."""
    return bool(_BREAK.match(text))


def tentative_marker(text: str) -> str | None:
    """The tentative marker as the client wrote it, or None. The title keeps the marker."""
    found = _TENTATIVE.search(text)
    return found.group(0) if found else None
