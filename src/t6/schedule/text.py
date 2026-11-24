"""Cell text. #720 extends this with tentative, NA, break and note classification."""

from __future__ import annotations

from t6.schedule.contract import CellValue


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
