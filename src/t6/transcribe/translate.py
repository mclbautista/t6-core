"""Cue-by-cue translation: the task definition, SRT cues and the structure check.

The worker has no LLM credential (ADR 0003). It sends one cue's text at a time to the API's
lease-checked route ``POST /v1/transcribe/llm``, which only the transcribe worker may call, only with
the live lease of a ``transcript.translate.run`` job, and which runs the ``llm run`` task below. Timecodes,
indexes and speaker labels never go to the model; they are copied from the
source cue, so they cannot change.
"""
from __future__ import annotations

import re
import textwrap
from collections.abc import Callable
from dataclasses import dataclass

from t6.llm.api import LLMRunInput

PROMPT = ('Translate the dialogue to English. Keep post-production terms and brand names, '
          'numbers, dates, slang and profanity without softening. Preserve line breaks. '
          'Do not add dialogue. Input:\n{input}')
SCHEMA = {'type': 'object', 'properties': {'text': {'type': 'string'}},
          'required': ['text'], 'additionalProperties': False}
_TIMECODE = re.compile(r'\d{2}:\d{2}:\d{2},\d{3} --> \d{2}:\d{2}:\d{2},\d{3}')


class TranslationError(ValueError):
    """A reply broke a cue-structure rule, or the LLM run did not produce one."""


def task_input(text: str) -> LLMRunInput:
    """Registered names, Project codes and credentials are masked by the API (``names`` preset)."""
    return LLMRunInput(task_name='transcript translate', prompt_template=PROMPT, output_schema=SCHEMA,
                       input_text=text, masking_preset='names', temperature_zero=True)


@dataclass(frozen=True)
class Cue:
    index: str
    timecode: str
    speaker: str
    text: str


def _label_end(lines: list[str]) -> int | None:
    """Index of the last label line when the cue starts with a label in the exact form the renderer writes, else None."""
    for end in range(2, len(lines) - 1):
        label = lines[2:end + 1]
        if label[-1].endswith(':') and label == textwrap.wrap(' '.join(label), width=42):
            return end
    return None


def parse_srt(value: str) -> list[Cue]:
    """Parse cues; a speaker label is only what the renderer writes: the first text lines, wrapped at 42, ending in a colon."""
    cues = []
    for block in re.split(r'\n\s*\n', value.strip()):
        lines = block.splitlines()
        if len(lines) < 3 or not lines[0].isdigit() or not _TIMECODE.fullmatch(lines[1]):
            raise ValueError('Invalid SRT cue')
        end = _label_end(lines)
        speaker = '\n'.join(lines[2:end + 1]) if end is not None else ''
        text = '\n'.join(lines[(end + 1 if end is not None else 2):])
        if not text.strip():
            raise ValueError('Empty SRT cue')
        cues.append(Cue(lines[0], lines[1], speaker, text))
    return cues


def render_srt(cues: list[Cue]) -> str:
    return ''.join(f'{c.index}\n{c.timecode}\n' + (c.speaker + '\n' if c.speaker else '') + c.text + '\n\n' for c in cues)


def translate_cues(cues: list[Cue], translate: Callable[[str], str | None]) -> list[Cue]:
    """Translate each cue's text alone; copy index, timecode and speaker from the source.

    A reply that is empty, has another number of lines or holds a blank line
    would add, drop or merge cues, so it is an error.
    """
    result = []
    for cue in cues:
        value = translate(cue.text)
        if (value is None or not value.strip() or value.count('\n') != cue.text.count('\n')
                or re.search(r'\n\s*\n', value)):
            raise TranslationError('Translation does not fit the cue')
        result.append(Cue(cue.index, cue.timecode, cue.speaker, value))
    return result


def docx_words(cues: list[Cue]) -> tuple[dict[str, object], dict[tuple[str, str], str]]:
    """Transcript words and a speaker map that make ``render_docx`` write one paragraph per cue."""
    words, names = [], {}
    for number, cue in enumerate(cues):
        hours, minutes, seconds = cue.timecode.split(' --> ')[0].replace(',', '.').split(':')
        start = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        words.append({'type': 'word', 'text': cue.text, 'start': start, 'end': start, 'speaker_id': str(number)})
        names[('', str(number))] = cue.speaker.replace('\n', ' ').removesuffix(':')
    return {'words': words}, names
