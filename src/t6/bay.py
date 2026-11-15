"""Bay identity and migration from Notion."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

from t6.google.sync import enqueue
from t6.preview import PreviewTokens
from t6.verbs import Outcome
from t6.ids import EntityKind, T6Id
from t6.registry.core import normalize_page_id
from t6.registry.sqlite import SqliteProjectRegistry


class BayStore:
    def __init__(self, registry: SqliteProjectRegistry) -> None:
        self.registry = registry

    def migrate(self, rows: Iterable[Any], *, actor: str, apply: bool = False,
                digest: str | None = None) -> dict[str, Any]:
        pages = sorted({normalize_page_id(row.page_id) for row in rows})
        snapshot = json.dumps(pages)
        with self.registry._transaction() as db:
            entries = []
            for page in pages:
                binding = db.execute("SELECT project_id FROM bindings WHERE system='notion' AND page_id=?", (page,)).fetchone()
                existing = db.execute('SELECT id FROM bays WHERE id=?', (binding[0],)).fetchone() if binding else None
                entries.append({'page_id': page, 'action': 'keep' if existing else 'review' if binding else 'register', 't6_id': existing[0] if existing else None})
            fingerprint = hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest()
            if apply and digest != fingerprint:
                receipt = db.execute('SELECT snapshot, entries FROM bay_migration_receipts WHERE digest=?', (digest,)).fetchone()
                if not receipt or receipt[0] != snapshot:
                    raise ValueError('Bays changed since preview; preview again')
                prior = json.loads(receipt[1])
                if any(old['t6_id'] != new['t6_id'] for old, new in zip(prior, entries)):
                    raise ValueError('Bindings changed since preview; preview again')
                return {'digest': digest, 'entries': entries, 'review': [e for e in entries if e['action'] == 'review']}
            if apply:
                for entry in entries:
                    if entry['action'] != 'register':
                        continue
                    identifier = str(T6Id.mint(EntityKind.BAY))
                    now = self.registry._clock()
                    db.execute('INSERT INTO bays VALUES (?)', (identifier,))
                    db.execute("INSERT INTO bindings (system, page_id, project_id, bound_at, active) VALUES ('notion', ?, ?, ?, 1)", (entry['page_id'], identifier, now))
                    db.execute('INSERT INTO audit (project_id, action, actor, at, before, after) VALUES (?, ?, ?, ?, NULL, ?)', (identifier, 'bay_migrated', actor, now, json.dumps({'page_id': entry['page_id']})))
                    entry.update(action='registered', t6_id=identifier)
                db.execute('INSERT OR IGNORE INTO bay_migration_receipts VALUES (?, ?, ?)', (fingerprint, snapshot, json.dumps(entries)))
            return {'digest': fingerprint, 'entries': entries, 'review': [e for e in entries if e['action'] == 'review']}


class BookingStore:
    def __init__(self, registry: SqliteProjectRegistry, tokens: PreviewTokens) -> None:
        self.registry, self.tokens = registry, tokens

    def get(self, booking: str) -> dict[str, Any] | None:
        with self.registry._transaction() as db:
            row = db.execute('SELECT id, bay_id, person_id, project_id, start, end, forced, cancelled FROM bookings WHERE id=?', (booking,)).fetchone()
            return dict(zip(('id', 'bay_id', 'person_id', 'project_id', 'start', 'end', 'forced', 'cancelled'), (*row[:6], bool(row[6]), bool(row[7])))) if row else None

    def list(self) -> list[dict[str, Any]]:
        with self.registry._transaction() as db:
            rows = db.execute('SELECT id, bay_id, person_id, project_id, start, end, forced, cancelled FROM bookings ORDER BY start, id').fetchall()
            return [dict(zip(('id', 'bay_id', 'person_id', 'project_id', 'start', 'end', 'forced', 'cancelled'), (*row[:6], bool(row[6]), bool(row[7])))) for row in rows]

    def write(self, verb: str, values: dict[str, Any], *, actor: str, idempotency_key: str | None, confirm: str | None) -> Outcome:
        import secrets
        from datetime import date, datetime, time, timedelta
        from t6.domain.studio_time import MANILA
        from t6.verbs import Done, NeedsPerson, Refused
        if (confirm and not idempotency_key) or (idempotency_key is not None and not idempotency_key.strip()):
            return Refused(rule='idempotency_key', reason='A nonblank idempotency key is required')
        key = idempotency_key or secrets.token_hex(16)
        inputs = {**values, 'idempotency_key': key}
        name = 'bay book' if verb == 'book' else 'booking ' + verb
        fingerprint = json.dumps({'verb': name, 'actor': actor, **inputs}, sort_keys=True, separators=(',', ':'))
        nonce = self.tokens.verify(confirm, name, actor, inputs) if confirm else None
        if isinstance(nonce, Refused):
            return nonce
        with self.registry._transaction() as db:
            if nonce and db.execute('SELECT 1 FROM used_preview_tokens WHERE nonce=?', (nonce,)).fetchone():
                return Refused(rule='token_used', reason='Preview token has already been used')
            previous = db.execute('SELECT fingerprint, result FROM verb_requests WHERE idempotency_key=?', (key,)).fetchone()
            if previous and previous[0] != fingerprint:
                return Refused(rule='idempotency_key', reason='Idempotency key was used for a different request')
            if previous:
                if nonce:
                    db.execute('INSERT INTO used_preview_tokens VALUES (?)', (nonce,))
                    return Done(result=json.loads(previous[1]))
                return self.tokens.create(name, actor, inputs, json.loads(previous[1]))
            if verb == 'book':
                def resolve(table: str, value: str) -> str | None:
                    page = normalize_page_id(value)
                    row = db.execute(f"SELECT id FROM {table} WHERE id=? OR id=(SELECT project_id FROM bindings WHERE system='notion' AND page_id=?)", (value, page)).fetchone()
                    return str(row[0]) if row else None
                bay = resolve('bays', values['bay'])
                person = resolve('persons', values['person'])
                project = resolve('projects', values['project'])
                if not bay or not person or not project:
                    return Refused(rule='input', reason='Bay, Person or Project not found')
                old = None
                start, end = values.get('start'), values.get('end')
                if values.get('shift'):
                    shift = db.execute('SELECT pattern, start, end FROM person_shifts WHERE person_id=?', (person,)).fetchone()
                    if not shift or shift[0] != values['shift']:
                        return NeedsPerson(reason='Person has no matching shift pattern')
                    try:
                        day = date.fromisoformat(str(start))
                        if end is not None:
                            raise ValueError()
                        start = datetime.combine(day, time.fromisoformat(shift[1]), MANILA).isoformat()
                        end = datetime.combine(day + timedelta(days=shift[0] == 'night'), time.fromisoformat(shift[2]), MANILA).isoformat()
                    except (ValueError, TypeError):
                        return Refused(rule='shift', reason='Shift requires a start date and no end')
            else:
                old = db.execute('SELECT id, bay_id, person_id, project_id, start, end, forced, cancelled FROM bookings WHERE id=?', (values['booking'],)).fetchone()
                if not old or old[7]:
                    return Refused(rule='booking', reason='Active Booking not found')
                _, bay, person, project, start, end, _, _ = old
                if verb == 'move':
                    bay = values.get('bay') or bay
                    if not db.execute('SELECT 1 FROM bays WHERE id=?', (bay,)).fetchone():
                        return Refused(rule='bay', reason='Bay not found')
                    start, end = values['start'], values['end']
                elif verb == 'extend':
                    end = values['end']
            try:
                begin = datetime.fromisoformat(str(start))
                finish = datetime.fromisoformat(str(end))
                if begin.tzinfo is None or finish.tzinfo is None or finish <= begin:
                    raise ValueError()
                begin, finish = begin.astimezone(MANILA), finish.astimezone(MANILA)
                if verb == 'extend' and old and finish <= datetime.fromisoformat(old[5]):
                    raise ValueError()
            except (ValueError, TypeError):
                return Refused(rule='time', reason='A valid timezone-aware start before end is required')
            start, end = begin.isoformat(timespec='microseconds'), finish.isoformat(timespec='microseconds')
            overlap = bool(db.execute('SELECT 1 FROM bookings WHERE bay_id=? AND cancelled=0 AND id<>? AND start<? AND end>? LIMIT 1', (bay, old[0] if old else '', end, start)).fetchone()) if verb != 'cancel' else False
            if overlap and not values.get('force', False):
                return Refused(rule='overlap', reason='Bay already has an overlapping Booking')
            plan = {'booking_id': old[0] if old else None, 'bay_id': bay, 'person_id': person, 'project_id': project, 'start': start, 'end': end, 'forced': bool(old[6]) if verb == 'cancel' and old else overlap and bool(values.get('force')), 'cancelled': verb == 'cancel', 'idempotency_key': key}
            if not nonce:
                preview = self.tokens.create(name, actor, inputs, plan)
                preview_nonce = self.tokens.verify(preview.token, name, actor, inputs)
                db.execute('INSERT INTO booking_previews VALUES (?, ?)', (preview_nonce, json.dumps({'plan': plan, 'old': old})))
                return preview
            saved = db.execute('SELECT plan FROM booking_previews WHERE nonce=?', (nonce,)).fetchone()
            if not saved or json.loads(saved[0]) != {'plan': plan, 'old': list(old) if old else None}:
                return Refused(rule='booking_changed', reason='Booking or overlap changed since preview')
            identifier = old[0] if old else str(T6Id.mint(EntityKind.BOOKING))
            plan['booking_id'] = identifier
            db.execute('INSERT INTO bookings VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET bay_id=excluded.bay_id, start=excluded.start, end=excluded.end, forced=excluded.forced, cancelled=excluded.cancelled', (identifier, bay, person, project, start, end, int(bool(plan['forced'])), int(bool(plan['cancelled']))))
            now = self.registry._clock()
            action = 'booking_' + verb
            cursor = db.execute('INSERT INTO audit (project_id, action, actor, at, before, after) VALUES (?, ?, ?, ?, ?, ?)', (identifier, action, actor, now, json.dumps(old), json.dumps(plan)))
            db.execute('INSERT INTO activity (project_id, subject_id, at, actor, action, summary, audit_id) VALUES (?, ?, ?, ?, ?, ?, ?)', (project, identifier, now, actor, action, name, cursor.lastrowid))
            db.execute('INSERT INTO used_preview_tokens VALUES (?)', (nonce,))
            db.execute('INSERT INTO verb_requests VALUES (?, ?, ?)', (key, fingerprint, json.dumps(plan)))
            enqueue(db, identifier, old[1] if old else None, key, now)
            return Done(result=plan)
