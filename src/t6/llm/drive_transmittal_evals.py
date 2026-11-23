"""Eval cases for Jev's Drive transmittal filter.

Expected properties: keep explicit movements and captionless drive photos; ignore
questions and unrelated captions. Code grades the two-valued verdict. People
review semantic judgment on a recorded live run before release. All inputs here
are synthetic text, including photo readings; real cases and run records stay
off-repo. Privacy is enforced by contract tests, not this grader.
"""
from __future__ import annotations

from pydantic import JsonValue

from t6.llm.drive_transmittal import TRANSMITTAL
from t6.llm.evals import Case, CaseResult, SyntheticProvenance

_DRIVE: list[JsonValue] = [{'label': 'SYNTHETIC MASTER 01', 'serial': 'FAKE-SN-001', 'confidence': 0.95}]
_INPUTS: dict[str, tuple[str, bool, list[JsonValue]]] = {
    'out-photo': ('OUT Synthetic borrower', True, _DRIVE),
    'in-shelf-photo': ('IN SHELF', True, _DRIVE),
    'question': ('What time is the synthetic meeting?', False, []),
    'unrelated-caption': ('Synthetic lunch photo', True, []),
    'empty-caption-drive': ('', True, _DRIVE),
}
_EXPECTED = {
    'out-photo': 'transmittal', 'in-shelf-photo': 'transmittal',
    'question': 'not_transmittal', 'unrelated-caption': 'not_transmittal',
    'empty-caption-drive': 'transmittal',
}
CASES = [
    Case(id=key, task=TRANSMITTAL.prompt_tag,
         masked_input={'text': text, 'has_photo': has_photo, 'readings': readings},
         expected_properties=['Keep Drive movements and captionless drive photos; ignore unrelated conversation',
                              'Return only transmittal or not_transmittal'],
         grader='drive-transmittal-verdict', provenance=SyntheticProvenance(kind='synthetic'))
    for key, (text, has_photo, readings) in _INPUTS.items()
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
