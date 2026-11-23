"""Described synthetic photo fixtures and offline replies; no real images or labels.

Code grades schema, counts, and exact readable text. People review visual grounding
and confidence calibration on a recorded live run. Descriptions are fixture recipes,
not substitutes for images in a live run. Real cases and run records stay off-repo.
"""

from __future__ import annotations

import json
from collections import Counter

from pydantic import JsonValue, ValidationError

from t6.llm.drive_photo import PHOTO_READ, PhotoReadings
from t6.llm.evals import Case, CaseResult, SyntheticProvenance

# All text below is invented, including maker serials.
_EXPECTED: dict[str, list[tuple[str | None, str | None]]] = {
    "one-drive": [("SYNTHETIC MASTER 01", "FAKE-SN-001")],
    "several-drives": [("SYNTHETIC MASTER 01", "FAKE-SN-001"), ("Synthetic backup 02", None)],
    "unreadable": [],
}
_DESCRIPTIONS = {
    "one-drive": "One prop drive: label SYNTHETIC MASTER 01; maker sticker S/N FAKE-SN-001.",
    "several-drives": (
        "Two prop drives: label SYNTHETIC MASTER 01 with maker sticker S/N FAKE-SN-001; "
        "label Synthetic backup 02 with the maker sticker hidden."
    ),
    "unreadable": "Blurred prop drives with all labels and maker stickers fully obscured.",
}
CASES = [
    Case(
        id=key, task=PHOTO_READ.prompt_tag, masked_input={"synthetic_photo_description": description},
        expected_properties=[
            "One entry per drive with readable text; no invented entries",
            "Label copied exactly, maker serial only when visible, otherwise null",
            "Confidence is a finite number from 0 to 1",
            "Empty drives when nothing is readable",
        ],
        grader="drive-photo-exact-text", provenance=SyntheticProvenance(kind="synthetic"),
    )
    for key, description in _DESCRIPTIONS.items()
]
MUST_PASS = frozenset(case.id for case in CASES)
OFFLINE_REPLIES = {
    key: json.dumps({"drives": [{"label": label, "serial": serial, "confidence": 0.95}
                               for label, serial in expected]})
    for key, expected in _EXPECTED.items()
}


def grade_photo(case: Case, output: JsonValue) -> CaseResult:
    """Grade transcription and count independently of drive ordering."""
    try:
        readings = PhotoReadings.model_validate(output)
    except ValidationError:
        return CaseResult(case_id=case.id, passed=False, reason="invalid photo reading schema")
    found = Counter((drive.label, drive.serial) for drive in readings.drives)
    passed = found == Counter(_EXPECTED[case.id])
    return CaseResult(case_id=case.id, passed=passed,
                      reason="readable text and count match" if passed else "readable text or count differs")
