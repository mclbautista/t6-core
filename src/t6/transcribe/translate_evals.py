"""Synthetic eval cases and code graders for the ``transcript translate`` task.

Every line here is invented. Real cases stay off-repo (see docs/agents/llm-task-specs.md).
Code graders check structure only; meaning, Taglish fluency and whether slang and
profanity were kept (not softened) are judged by people on the recorded live run.
``OFFLINE_REPLIES`` are scripted fake-LLM replies for the offline harness, one per cue.
"""
from __future__ import annotations

import re

from pydantic import JsonValue

from t6.llm.evals import Case, CaseResult, SyntheticProvenance
from t6.transcribe.translate import Cue, parse_srt

TASK = 'transcript translate'
GRADER = 'cue-structure'
KEPT_TERMS = ('color grade', 'timeline', 'export', 'audio mix', 'lock the cut', 'ProRes', 'LUT', 'DCP')
"""Invented post-production terms and brand names that a source cue may hold and the output must keep."""


def _srt(*cues: tuple[str, str]) -> str:
    return ''.join(f'{i}\n{timecode}\n{text}\n\n' for i, (timecode, text) in enumerate(cues, 1))


def _case(case_id: str, properties: list[str], srt: str) -> Case:
    return Case(id=case_id, task=TASK, masked_input=srt, expected_properties=properties, grader=GRADER,
                provenance=SyntheticProvenance(kind='synthetic'))


_A, _B, _C, _D = ('00:00:01,000 --> 00:00:03,500', '00:00:04,000 --> 00:00:06,250',
                  '00:00:07,000 --> 00:00:09,000', '00:00:10,500 --> 00:00:12,000')

_SOURCES: dict[str, tuple[list[str], str]] = {
    'taglish-terms': (['Tagalog sentences keep English post-production terms'],
                      _srt((_A, 'Host:\nIlagay mo na yung color grade sa timeline bago mag export.'),
                           (_B, 'Guest:\nSige, gagamitin ko yung LUT na ProRes ang export.'))),
    'code-switching': (['Mid-sentence English words stay as spoken'],
                       _srt((_A, 'Host:\nSige, lock the cut mamaya, pero check muna natin yung audio mix.'))),
    'speaker-labels': (['Speaker labels are unchanged'],
                       _srt((_A, 'Host:\nKumusta ang shoot kahapon?'),
                            (_B, 'Guest:\nMaayos naman, medyo mahaba lang.'),
                            (_C, 'Host:\nSalamat sa pagpunta.'))),
    'timecodes-line-breaks': (['Timecodes and line breaks are unchanged'],
                              _srt((_A, 'Host:\nMagsisimula tayo ngayon\nat tatapusin natin bukas.'),
                                   (_B, 'Guest:\nOo,\nsige\nna.'))),
    'numbers-dates': (['Numbers and dates are unchanged'],
                      _srt((_A, 'Host:\nSa 12 Marso, 3 shots lang ang naka 4K at 24 fps lahat.'),
                           (_B, 'Guest:\nAng DCP ay 2048 by 858, due sa 30 Abril.'))),
    'profanity-slang': (['Slang and profanity are translated, not softened or dropped'],
                        _srt((_A, 'Host:\nPutang ina, ang pangit ng audio, tangina talaga.'),
                             (_B, 'Guest:\nPak na pak yung shot, solid!'))),
    'no-merge-no-invention': (['No invented lines, no merged speakers'],
                              _srt((_A, 'Host:\nOo.'), (_B, 'Guest:\nOo nga.'), (_C, 'Host:\nSige.'),
                                   (_D, 'Guest:\nO, tapos na.'))),
    'character-name': (['A character name in dialogue passes through unchanged'],
                       _srt((_A, 'Guest:\nSabi ni Kapitan Bagwis, huwag kang aalis.'))),
    'raw-speaker-id': (['With a speaker map applied, a speaker with no entry keeps its raw id label and only its dialogue is translated'],
                       _srt((_A, 'Host:\nKumusta ang shoot?'), (_B, 'speaker_2:\nMaayos naman, medyo mahaba lang.'))),
    'unlabelled-colon': (['A cue with no speaker label whose text has a line ending in a colon is translated in full'],
                         _srt((_A, 'Sabi niya sa akin\nang sagot ay ito:\nOo na, sige.'))),
    'unmapped-label': (['With no speaker map there are no labels: a first line like "Sabi niya:" is dialogue and is translated'],
                       _srt((_A, 'Sabi niya:\nOo na, sige.'))),
}

SPEAKER_NAMES = frozenset({'Host', 'Guest'})
NO_SPEAKER_MAP = frozenset({'unmapped-label', 'unlabelled-colon'})
"""Cases whose Transcript has no applied speaker map; every other case has one that names Host and Guest."""

OFFLINE_REPLIES: dict[str, list[str]] = {
    'taglish-terms': ['Put the color grade on the timeline already before you export.',
                      "Okay, I'll use the LUT with the ProRes export."],
    'code-switching': ["Okay, we'll lock the cut later, but let's check the audio mix first."],
    'speaker-labels': ['How was the shoot yesterday?', 'It went fine, just a bit long.', 'Thanks for coming.'],
    'timecodes-line-breaks': ["We'll start now\nand we'll finish tomorrow.", 'Yes,\nokay\nthen.'],
    'numbers-dates': ['On March 12, only 3 shots are in 4K and everything is 24 fps.',
                      'The DCP is 2048 by 858, due April 30.'],
    'profanity-slang': ["Fucking hell, the audio is ugly, goddamn it.", 'That shot is stunning, solid!'],
    'no-merge-no-invention': ['Yes.', 'Yes indeed.', 'Okay.', 'Right, that is all.'],
    'character-name': ['Captain Bagwis said, do not leave.'],
    'raw-speaker-id': ['How was the shoot?', 'It went fine, just a bit long.'],
    'unlabelled-colon': ["He told me\nthe answer is this:\nYes, okay, fine."],
    'unmapped-label': ['He said:\nYes, okay, fine.'],
}

CASES = [_case(case_id, [*properties, 'Same cue count, timecodes, speaker labels and line breaks'], srt)
         for case_id, (properties, srt) in _SOURCES.items()]
MUST_PASS = frozenset(case.id for case in CASES)


def names_for(case: Case) -> frozenset[str] | None:
    """The speaker names the case's Transcript knows (its applied speaker map), None when no map is applied."""
    return None if case.id in NO_SPEAKER_MAP else SPEAKER_NAMES


def _terms(text: str) -> set[str]:
    return {term for term in KEPT_TERMS if term.lower() in text.lower()}


def _numbers(text: str) -> list[str]:
    return sorted(re.findall(r'\d+', text))


def grade_translation(case: Case, output: JsonValue) -> CaseResult:
    """Check a translated SRT against the case's source SRT; code only, no model."""

    def result(passed: bool, reason: str) -> CaseResult:
        return CaseResult(case_id=case.id, passed=passed, reason=reason)

    if not isinstance(case.masked_input, str) or not isinstance(output, str):
        return result(False, 'source and output must be SRT text')
    try:
        source, translated = parse_srt(case.masked_input, names_for(case)), parse_srt(output, names_for(case))
    except ValueError:
        return result(False, 'output is not valid SRT')
    if len(source) != len(translated):
        return result(False, 'cue count changed')
    for a, b in zip(source, translated):
        failure = _compare(a, b)
        if failure:
            return result(False, f'cue {a.index}: {failure}')
    return result(True, 'cue structure kept')


def _compare(a: Cue, b: Cue) -> str:
    if a.index != b.index:
        return 'index changed'
    if a.timecode != b.timecode:
        return 'timecode changed'
    if a.speaker != b.speaker:
        return 'speaker label changed'
    if a.text.count('\n') != b.text.count('\n'):
        return 'line breaks changed'
    if _numbers(a.text) != _numbers(b.text):
        return 'numbers changed'
    if not _terms(a.text) <= _terms(b.text):
        return 'kept term lost'
    return ''
