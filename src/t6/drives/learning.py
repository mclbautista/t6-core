"""Custodian learning and the accuracy log of recording Item approvals."""
from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from contextlib import nullcontext
from datetime import date, datetime, time, timedelta
from typing import Any

from t6.domain.studio_time import MANILA
from t6.drives.photos import PhotoError, photo_dir, store_file
from t6.registry.sqlite import SqliteProjectRegistry
from t6.verbs import Done, Outcome, Refused


OUTCOMES = ('guess1', 'guess2', 'guess3', 'none', 'typed', 'new', 'not_a_drive')


def learn(db: sqlite3.Connection, drive: str, text: str, movement: str, capture: str, who: str, at: float) -> list[str]:
    data = json.loads(db.execute('SELECT data FROM drive_movements WHERE id=?', (movement,)).fetchone()[0])
    db.execute('INSERT INTO drive_short_names VALUES (?, ?, ?, 1, ?) ON CONFLICT(text, drive_id) DO UPDATE SET '
               'confirmations=confirmations+1, last_confirmed=excluded.last_confirmed, project=excluded.project',
               (text, drive, json.dumps(data['project']) if data['project'] else None, at))
    row = db.execute('SELECT photo, data FROM drive_captures WHERE id=?', (capture,)).fetchone()
    folder = photo_dir()
    if row[0] and folder is not None:
        media_type = json.loads(row[1])['media_type']
        path, digest = store_file(folder, f'{drive}-{movement}', row[0], media_type)
        old = db.execute('SELECT path FROM drive_reference_photos WHERE drive_id=?', (drive,)).fetchone()
        db.execute('INSERT OR REPLACE INTO drive_reference_photos VALUES (?, ?, ?, ?, ?, ?, ?)',
                   (drive, path, media_type, len(row[0]), digest, who, at))
        return [old[0]] if old and old[0] != path else []
    return []


def learned(db: sqlite3.Connection, drive: str) -> dict[str, Any]:
    names = [{'text': text, 'project': json.loads(project) if project else None, 'confirmations': count,
              'last_confirmed': datetime.fromtimestamp(at, MANILA).isoformat()}
             for text, project, count, at in db.execute('SELECT text, project, confirmations, last_confirmed FROM drive_short_names WHERE drive_id=? ORDER BY text', (drive,))]
    return {'short_names': names, 'reference_photo': drive if db.execute('SELECT 1 FROM drive_reference_photos WHERE drive_id=?', (drive,)).fetchone() else None}


def reference_photo(registry: SqliteProjectRegistry, drive: str, *, within: sqlite3.Connection | None = None) -> dict[str, Any]:
    folder = photo_dir()
    if folder is None:
        raise PhotoError('config', 'Set T6_DRIVE_PHOTO_DIR')
    with nullcontext(within) if within is not None else registry._transaction() as db:
        row = db.execute('SELECT path, media_type, size, sha256 FROM drive_reference_photos WHERE drive_id=?', (drive,)).fetchone()
    if row is None:
        raise PhotoError('photo', 'This Drive has no Reference photo')
    try:
        data = (folder / row[0]).read_bytes()
    except OSError:
        raise PhotoError('photo', 'The photo file is missing') from None
    if hashlib.sha256(data).hexdigest() != row[3]:
        raise PhotoError('photo', 'The photo file does not match its record')
    return {'drive': drive, 'media_type': row[1], 'size': row[2], 'photo': base64.b64encode(data).decode()}


def matches(registry: SqliteProjectRegistry, start: str | None, end: str | None) -> Outcome:
    try:
        first = date.fromisoformat(start) if start else None
        last = date.fromisoformat(end) if end else None
        if first and last and first > last:
            raise ValueError
    except ValueError:
        return Refused(rule='date', reason='Use ISO dates (YYYY-MM-DD), with start no later than end')
    lower = datetime.combine(first, time.min, MANILA).timestamp() if first else float('-inf')
    upper = datetime.combine(last + timedelta(days=1), time.min, MANILA).timestamp() if last else float('inf')
    with registry._transaction() as db:
        approvals = [{'capture': capture, 'item': item, 'outcome': outcome, 'rank': rank,
                      'at': datetime.fromtimestamp(at, MANILA).isoformat(), 'who': who}
                     for capture, item, outcome, rank, at, who in db.execute(
                         'SELECT capture, item, outcome, rank, at, who FROM drive_match_approvals WHERE at>=? AND at<? ORDER BY at, id', (lower, upper))]
    counts = {outcome: sum(a['outcome'] == outcome for a in approvals) for outcome in OUTCOMES}
    return Done(result={'total': len(approvals), 'counts': counts, 'guess1_rate': counts['guess1'] / len(approvals) if approvals else 0.0,
                        'approvals': approvals})
