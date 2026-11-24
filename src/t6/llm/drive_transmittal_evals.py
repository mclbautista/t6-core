"""Eval cases for Jev's Drive transmittal filter.

Task: drive-transmittal-v2. The input is the withheld caption and a has-photo
flag; Jev runs before the photo read, so it sees no photo readings (v1 did).
Expected properties: keep explicit movements, text-only IN/OUT posts and
captionless photos; ignore questions and unrelated captions. Code grades the
two-valued verdict. People review semantic judgment on a recorded live run.
A live run of v2 is an owner step and must be recorded before any release that
contains it (docs/agents/llm-task-specs.md, evals spec #179). All inputs here
are synthetic text; real cases and run records stay off-repo. Privacy is
enforced by contract tests, not this grader.
"""
from __future__ import annotations

from pydantic import JsonValue

from t6.llm.drive_transmittal import TRANSMITTAL
from t6.llm.evals import Case, CaseResult, SyntheticProvenance

_INPUTS: dict[str, tuple[str, bool]] = {
    'out-photo': ('OUT Synthetic borrower', True),
    'in-shelf-photo': ('IN SHELF', True),
    'question': ('What time is the synthetic meeting?', False),
    'unrelated-caption': ('Synthetic lunch photo', True),
    'photo-no-caption': ('', True),
    'text-only-out': ('OUT Synthetic borrower', False),
}
_EXPECTED = {
    'out-photo': 'transmittal', 'in-shelf-photo': 'transmittal',
    'question': 'not_transmittal', 'unrelated-caption': 'not_transmittal',
    'photo-no-caption': 'transmittal', 'text-only-out': 'transmittal',
}
CASES = [
    Case(id=key, task=TRANSMITTAL.prompt_tag,
         masked_input={'text': text, 'has_photo': has_photo},
         expected_properties=['Keep Drive movements, text-only IN/OUT posts and captionless photos; ignore unrelated conversation',
                              'Return only transmittal or not_transmittal'],
         grader='drive-transmittal-verdict', provenance=SyntheticProvenance(kind='synthetic'))
    for key, (text, has_photo) in _INPUTS.items()
]
MUST_PASS = frozenset(_INPUTS)
OFFLINE_REPLIES = {
    key: {'model': 'synthetic-jev', 'answers': {'c0': {
        'type': 'choice', 'choice': 'match' if expected == 'transmittal' else 'no',
        'probabilities': {'match': 1 if expected == 'transmittal' else 0, 'maybe': 0,
                          'no': 1 if expected == 'not_transmittal' else 0},
    }}}
    for key, expected in _EXPECTED.items()
}


def grade_transmittal(case: Case, output: JsonValue) -> CaseResult:
    passed = output == _EXPECTED[case.id]
    return CaseResult(case_id=case.id, passed=passed,
                      reason='verdict matches' if passed else 'incorrect or invalid verdict')
