"""Synthetic cases for the PM Notes closed-choice task; real cases stay off-repo."""
from t6.llm.evals import Case, SyntheticProvenance


def _case(name: str, line: str, choice: str, quote: str, *, concern_id: str | None = None,
          waiting_on: str | None = None, as_of: str = '2026-11-08', expected: str | None = None) -> Case:
    return Case(id=name, task='concern-pm-notes', masked_input={'line': line, 'as_of': as_of,
        'answer': {'choice': choice, 'quote': quote, 'concern_id': concern_id, 'waiting_on': waiting_on},
        'expected': expected or choice}, expected_properties=[expected or choice], grader='concern-outcome',
        provenance=SyntheticProvenance(kind='synthetic'))


CASES = [
    _case('continues', 'Still waiting on client', 'continues', 'Still waiting on client', concern_id='offered'),
    _case('reworded', 'KDM pending with client', 'continues', 'KDM pending with client', concern_id='offered'),
    _case('settles', 'Client sent KDM', 'settles', 'Client sent KDM', concern_id='offered'),
    _case('new', 'Waiting on vendor for sound', 'new', 'Waiting on vendor for sound', waiting_on='vendor'),
    _case('nothing', 'Color looks good', 'nothing', 'Color looks good'),
    _case('taglish', 'KDM natanggap na kay client', 'settles', 'KDM natanggap na kay client', concern_id='offered'),
    _case('bad-quote', 'Client sent KDM', 'settles', 'invented', concern_id='offered', expected='ignored'),
    _case('bad-id', 'Client sent KDM', 'settles', 'Client sent KDM', concern_id='invented', expected='ignored'),
    _case('old', 'Client sent KDM', 'settles', 'Client sent KDM', concern_id='offered', as_of='2026-10-01', expected='ignored'),
    _case('not-waiting', 'Color looks good', 'nothing', 'Color looks good'),
]
