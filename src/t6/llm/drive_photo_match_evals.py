"""Generated prop boxes and scripted replies; no studio photos or names.

Code grades schema, shortlist letters, rank one and visible label text. A person
reviews visual grounding and reasons in the release-blocking recorded live run.
Real cases and live run records stay off-repo.
"""
from __future__ import annotations

import io
import json

from PIL import Image, ImageDraw
from pydantic import JsonValue, ValidationError

from t6.llm.drive_photo_match import PHOTO_MATCH, PhotoMatches
from t6.llm.evals import Case, CaseResult, SyntheticProvenance

_DESCRIPTIONS = {
    'matching-box': 'Foreground red prop box labelled SYN MASTER, matching Reference A.',
    'background-drive': 'Foreground blue SYN BACKUP box (B); unrelated red SYN MASTER box (A) in the background.',
    'unrelated': 'Empty grey tabletop, with no Drive or visible label.',
}
_EXPECTED = {'matching-box': ('A', 'SYN MASTER'), 'background-drive': ('B', 'SYN BACKUP'), 'unrelated': (None, None)}
CASES = [Case(
    id=key, task=PHOTO_MATCH.prompt_tag,
    masked_input={'synthetic_photo_description': description, 'shortlist': ['A', 'B']},
    expected_properties=[
        'At most three distinct shortlisted letters with finite confidence in 0 to 1 and a one-line reason',
        'Guess one identifies the named foreground box, never an unrelated background Drive',
        'An unrelated photo returns no guesses; label text is read only when visible',
    ], grader='drive-photo-match-ground-truth', provenance=SyntheticProvenance(kind='synthetic'),
) for key, description in _DESCRIPTIONS.items()]
MUST_PASS = frozenset(case.id for case in CASES)
OFFLINE_REPLIES = {key: json.dumps({
    'guesses': [{'letter': letter, 'confidence': 0.85, 'reason': 'Same synthetic box and visible tape label'}] if letter else [],
    'label_text': label,
}) for key, (letter, label) in _EXPECTED.items()}


def synthetic_photo(case_id: str) -> bytes:
    """Build reproducible synthetic evidence at runtime, including a background case."""
    image = Image.new('RGB', (640, 400), 'grey')
    draw = ImageDraw.Draw(image)
    if case_id == 'background-drive':
        draw.rectangle((450, 20, 600, 110), fill='red')
        draw.text((460, 50), 'SYN MASTER', fill='white')
    if case_id != 'unrelated':
        label = _EXPECTED[case_id][1]
        assert label is not None
        draw.rectangle((40, 130, 420, 360), fill='blue' if case_id == 'background-drive' else 'red')
        draw.rectangle((65, 200, 350, 250), fill='white')
        draw.text((80, 215), label, fill='black')
    output = io.BytesIO()
    image.save(output, format='PNG')
    return output.getvalue()


def grade_match(case: Case, output: JsonValue) -> CaseResult:
    try:
        value = PhotoMatches.model_validate(output)
    except ValidationError:
        return CaseResult(case_id=case.id, passed=False, reason='invalid match schema')
    first, label = _EXPECTED[case.id]
    passed = (all(g.letter in ('A', 'B') for g in value.guesses)
              and (value.guesses[0].letter if value.guesses else None) == first
              and value.label_text == label)
    return CaseResult(case_id=case.id, passed=passed,
                      reason='rank one and visible label match' if passed else 'rank one or visible label differs')
