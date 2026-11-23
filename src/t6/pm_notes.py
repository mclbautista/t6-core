"""Read dated lines from the team's PM Notes and keep their history."""
from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timedelta
from typing import Any

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


def archive(db: Any, project_id: str, lines: list[dict[str, str]], now: float) -> None:
    """Keep one autoscan's dated lines in the PM Notes history: first and last seen, and removed at the first run a line is absent (#638)."""
    seen = {(line['as_of'], line['text']) for line in lines}
    for as_of, text in seen:
        db.execute('INSERT INTO pm_notes_history VALUES (?, ?, ?, ?, ?, NULL) ON CONFLICT (project_id, as_of, line) '
                   'DO UPDATE SET last_seen = excluded.last_seen, removed = NULL', (project_id, as_of, text, now, now))
    for as_of, text in db.execute('SELECT as_of, line FROM pm_notes_history WHERE project_id = ? AND removed IS NULL', (project_id,)).fetchall():
        if (as_of, text) not in seen:
            db.execute('UPDATE pm_notes_history SET removed = ? WHERE project_id = ? AND as_of = ? AND line = ?', (now, project_id, as_of, text))


def history(db: Any, project_id: str) -> list[dict[str, str | None]]:
    """One Project's PM Notes history, newest first; seen and removed are Manila dates."""
    from t6.domain.studio_time import MANILA

    def day(at: float | None) -> str | None:
        return None if at is None else datetime.fromtimestamp(at, MANILA).date().isoformat()
    rows = db.execute('SELECT as_of, line, first_seen, last_seen, removed FROM pm_notes_history WHERE project_id = ? '
                      'ORDER BY as_of DESC, first_seen DESC, line', (project_id,)).fetchall()
    return [{'as_of': as_of, 'text': text, 'first_seen': day(first), 'last_seen': day(last), 'removed': day(removed)} for as_of, text, first, last, removed in rows]
