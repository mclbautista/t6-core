"""Read dated lines from the team's PM Notes."""
from __future__ import annotations

import calendar
import re
from datetime import date, timedelta

_HEADER = re.compile(r'^\s*as\s+of\s+(.+)$', re.I)
_DATE = re.compile(r'^(?:(?:mon|tue|wed|thu|fri|sat|sun)\w*\s+)?(?:(\d{4})-(\d{1,2})-(\d{1,2})|([a-z]{3,})\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:[ ,]+(\d{4}))?)(?:[\s.,:;\-]+(.*))?$', re.I)
_BULLET = re.compile(r'^\s*[•*·–-]+\s*')


def dated_lines(text: str | None, today: date) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    current: date | None = None
    for raw in (text or '').splitlines():
        line = raw.strip()
        header = _HEADER.match(line)
        if header:
            current = None
            match = _DATE.match(header.group(1))
            if not match:
                continue
            year, month, day, name, day_name, year_name, tail = match.groups()
            if name:
                months = [m.lower() for m in calendar.month_name]
                candidates = [i for i, m in enumerate(months) if len(name) >= 3 and m.startswith(name.lower())]
                if len(candidates) != 1:
                    continue
                month, day = str(candidates[0]), day_name
                year = year_name
            try:
                if year:
                    current = date(int(year), int(month or ''), int(day or ''))
                else:
                    limit = today + timedelta(days=7)
                    possibilities = []
                    for y in (today.year - 1, today.year, today.year + 1):
                        try:
                            candidate = date(y, int(month or ''), int(day or ''))
                            if candidate <= limit:
                                possibilities.append(candidate)
                        except ValueError:
                            pass
                    current = max(possibilities) if possibilities else None
            except ValueError:
                continue
            line = tail or ''
        if current and line:
            line = _BULLET.sub('', line).strip()
            if line:
                result.append({'as_of': current.isoformat(), 'text': line})
    return result
