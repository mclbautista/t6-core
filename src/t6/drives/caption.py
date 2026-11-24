"""Read a recording-channel caption as the staff write it: IN/OUT, Items, Count lines, `R:` recipient and @tags."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

DIRECTION = re.compile(r'(in|out)(?=[\s:\-]|$)[\s:\-]*', re.IGNORECASE)
_RECIPIENT = re.compile(r'r\s*:\s*(.+)', re.IGNORECASE)
_COUNT = re.compile(r'(\d+)\s+([^\W\d_][^\W\d_ ]*(?: [^\W\d_][^\W\d_ ]*)*)')
_TAG = re.compile(r'@\w+')


@dataclass(frozen=True)
class Caption:
    direction: Literal['IN', 'OUT'] | None
    items: list[str]
    counts: list[dict[str, int | str]]
    recipient: str | None
    tags: list[str]

    @property
    def grade(self) -> Literal['complete', 'thin', 'problem']:
        if self.direction is None or self.direction == 'OUT' and not self.recipient:
            return 'problem'
        return 'complete' if self.items or self.counts else 'thin'

    @property
    def problem(self) -> Literal['missing_direction', 'missing_recipient'] | None:
        if self.direction is None:
            return 'missing_direction'
        return 'missing_recipient' if self.direction == 'OUT' and not self.recipient else None


def parse_caption(text: str) -> Caption:
    lines = [line.strip() for line in text.strip().splitlines()]
    direction: Literal['IN', 'OUT'] | None = None
    if lines and (start := DIRECTION.match(lines[0])):
        direction = 'IN' if start[1].upper() == 'IN' else 'OUT'
        lines[0] = lines[0][start.end():]
    items, counts, recipient, tags = list[str](), list[dict[str, int | str]](), None, list[str]()
    for line in lines:
        tags += [tag[1:] for tag in _TAG.findall(line)]
        line = _TAG.sub('', line).strip()
        if (named := _RECIPIENT.fullmatch(line)):
            recipient = named[1].strip()
        elif (count := _COUNT.fullmatch(line)):
            counts.append({'count': int(count[1]), 'kind': count[2]})
        elif line:
            items.append(line)
    return Caption(direction, items, counts, recipient, tags)
