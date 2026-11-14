"""Person identity, migration and previewed writes."""
from __future__ import annotations

import hashlib
import json
import secrets
import re
from datetime import time
from typing import Any, Iterable, Mapping

from t6.ids import EntityKind, T6Id
from t6.preview import PreviewTokens
from t6.registry.core import normalize_page_id
from t6.registry.sqlite import SqliteProjectRegistry, _audit
from t6.verbs import Done, Outcome, Refused


SERVICES = frozenset({'gateway', 'worker', 'reports', 'watchdog', 'munki', 'hermes'})

PERMISSIONS = frozenset({"operator", "coordinator", "bookings", "drives", "admin"})


class People:
    def __init__(self, registry: SqliteProjectRegistry, tokens: PreviewTokens,
                 service_permissions: Mapping[str, frozenset[str]] | None = None) -> None:
        self.registry, self.tokens = registry, tokens
        self.service_permissions = dict(service_permissions or {})
        if any(name not in SERVICES or not grants <= PERMISSIONS
               for name, grants in self.service_permissions.items()):
            raise ValueError('Invalid service or permission in service grants')

    def get(self, person_id: str) -> dict[str, Any] | None:
        with self.registry._transaction() as db:
            row = db.execute('SELECT id, kind, departments, roles, telegram_id, master_brief, active FROM persons WHERE id = ?', (person_id,)).fetchone()
            if not row:
                return None
            bindings = db.execute("SELECT page_id, active FROM bindings WHERE system = 'notion' AND project_id = ? ORDER BY bound_at", (person_id,)).fetchall()
            shift = db.execute('SELECT pattern, start, end FROM person_shifts WHERE person_id = ?', (person_id,)).fetchone()
            permissions = [r[0] for r in db.execute("SELECT permission FROM person_permissions WHERE person_id = ? ORDER BY permission", (person_id,))]
        return dict(shift=dict(pattern=shift[0], start=shift[1], end=shift[2]) if shift else None, permissions=permissions, id=row[0], kind=row[1], departments=json.loads(row[2]), roles=json.loads(row[3]), telegram_id=row[4], master_brief=bool(row[5]), active=bool(row[6]), bindings=[dict(page_id=b[0], active=bool(b[1])) for b in bindings])

    def authorize(self, actor: str, role: str) -> Refused | None:
        with self.registry._transaction() as db:
            return self._authorize(db, actor, role)

    def _authorize(self, db: Any, actor: str, role: str) -> Refused | None:
        if actor in SERVICES:
            service_grants = self.service_permissions.get(actor, frozenset())
            if role == 'reader' or role in service_grants or (role == 'operator' and 'admin' in service_grants):
                return None
            return Refused(rule='permission', reason=f'needs {role}')
        person = db.execute("SELECT kind, active FROM persons WHERE id = ?", (actor,)).fetchone()
        if not person or not person[1]:
            return Refused(rule='permission', reason='needs active Person')
        if role == 'reader':
            return None if person[0] == 'staff' else Refused(rule='permission', reason='needs active staff Person')
        permissions = {row[0] for row in db.execute('SELECT permission FROM person_permissions WHERE person_id = ?', (actor,))}
        if role not in permissions and not (role == 'operator' and 'admin' in permissions):
            return Refused(rule='permission', reason=f'needs {role}')
        return None

    def allows_telegram(self, telegram_id: int | None, rule: Any) -> bool:
        """Resolve an active Telegram user and evaluate an action's allowed-tapper rule."""
        with self.registry._transaction() as db:
            person = db.execute('SELECT id FROM persons WHERE active = 1 AND telegram_id = ?', (telegram_id,)).fetchone()
            if person is None:
                return False
            def allows(candidate: Any) -> bool:
                if isinstance(candidate, str):
                    if candidate.startswith('person:'):
                        return bool(person[0] == candidate[7:])
                    if candidate.startswith('permission:'):
                        return self._authorize(db, person[0], candidate[11:]) is None
                if isinstance(candidate, dict) and isinstance(candidate.get('any_of'), list):
                    return any(allows(child) for child in candidate['any_of'])
                return False
            return allows(rule)

    def seed_operators(self, telegram_ids: Iterable[int]) -> dict[str, Any]:
        """Seed once after Persons have been imported; retain the first report."""
        with self.registry._transaction() as db:
            previous = db.execute('SELECT unmatched FROM permission_seed WHERE id = 1').fetchone()
            if previous:
                return {'unmatched': json.loads(previous[0]), 'seeded': False}
            unmatched = []
            for telegram_id in dict.fromkeys(telegram_ids):
                people = db.execute('SELECT id FROM persons WHERE telegram_id = ?', (telegram_id,)).fetchall()
                if not people:
                    unmatched.append(telegram_id)
                for (person_id,) in people:
                    db.execute("INSERT OR IGNORE INTO person_permissions VALUES (?, 'operator')", (person_id,))
                    _audit(db, person_id, 'person_grant', 'operator seed', self.registry._clock(), None, {'permission': 'operator'})
            db.execute('INSERT INTO permission_seed VALUES (1, ?)', (json.dumps(unmatched),))
            return {'unmatched': unmatched, 'seeded': True}

    def migrate(self, contacts: Iterable[Any], *, actor: str, apply: bool = False, digest: str | None = None) -> dict[str, Any]:
        contacts = tuple(contacts)
        snapshot = json.dumps(sorted((normalize_page_id(c.page_id), c.name, c.telegram_user_id) for c in contacts), ensure_ascii=False)
        with self.registry._transaction() as db:
            entries = []
            seen = set()
            telegram_pages: dict[int, str] = {}
            for contact in contacts:
                page = normalize_page_id(contact.page_id)
                if page in seen:
                    continue
                seen.add(page)
                binding = db.execute("SELECT project_id FROM bindings WHERE system = 'notion' AND page_id = ?", (page,)).fetchone()
                holder = db.execute('SELECT id FROM persons WHERE active = 1 AND telegram_id = ?', (contact.telegram_user_id,)).fetchone() if contact.telegram_user_id is not None else None
                duplicate = contact.telegram_user_id is not None and contact.telegram_user_id in telegram_pages
                if contact.telegram_user_id is not None:
                    telegram_pages[contact.telegram_user_id] = page
                action = 'keep' if binding and db.execute('SELECT 1 FROM persons WHERE id = ?', (binding[0],)).fetchone() else 'review' if binding or holder or duplicate else 'register'
                kind = None
                if action == 'keep':
                    kind = db.execute('SELECT kind FROM persons WHERE id = ?', (binding[0],)).fetchone()[0]
                problem = 'Page or Telegram ID already belongs to another entity' if action == 'review' else 'Kind is unknown' if kind is None else None
                entries.append(dict(page_id=page, name=contact.name, telegram_id=contact.telegram_user_id, kind=kind, action=action, t6_id=binding[0] if binding else None, problem=problem))
            # Include input values as well as state, so confirmation cannot silently change the plan.
            fingerprint = hashlib.sha256(json.dumps(entries, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if apply and digest != fingerprint:
                receipt = db.execute('SELECT contacts, entries FROM person_migration_receipts WHERE digest = ?', (digest,)).fetchone()
                if receipt is None or receipt[0] != snapshot:
                    raise ValueError('Contacts changed since preview; preview again')
                prior = json.loads(receipt[1])
                for entry in prior:
                    bound = db.execute("SELECT project_id FROM bindings WHERE system = 'notion' AND page_id = ?", (entry['page_id'],)).fetchone()
                    if (bound[0] if bound else None) != entry['t6_id']:
                        raise ValueError('Bindings changed since preview; preview again')
                return {'digest': digest, 'entries': entries, 'review': [e for e in entries if e['problem'] is not None]}
            if apply:
                for entry in entries:
                    if entry['action'] != 'register':
                        continue
                    # A preceding row in this same migration may have claimed the ID.
                    holder = db.execute('SELECT id FROM persons WHERE active = 1 AND telegram_id = ?', (entry['telegram_id'],)).fetchone() if entry['telegram_id'] is not None else None
                    if holder:
                        entry.update(action='review', problem=f'Telegram ID held by {holder[0]}')
                        continue
                    person_id = str(T6Id.mint(EntityKind.PERSON))
                    now = self.registry._clock()
                    db.execute("INSERT INTO persons VALUES (?, NULL, '[]', '[]', ?, 0, 1)", (person_id, entry['telegram_id']))
                    db.execute("INSERT INTO bindings (system, page_id, project_id, bound_at, active) VALUES ('notion', ?, ?, ?, 1)", (entry['page_id'], person_id, now))
                    _audit(db, person_id, 'person_migrated', actor, now, None, {'page_id': entry['page_id'], 'telegram_id': entry['telegram_id']})
                    entry.update(action='registered', t6_id=person_id)
                db.execute('INSERT OR REPLACE INTO person_migration_receipts VALUES (?, ?, ?)', (fingerprint, snapshot, json.dumps(entries)))
        return {'digest': fingerprint, 'entries': entries, 'review': [e for e in entries if e['problem'] is not None]}

    def write(self, verb: str, values: dict[str, Any], *, actor: str, idempotency_key: str | None = None, confirm: str | None = None) -> Outcome:
        if not actor.strip() or (idempotency_key is not None and not idempotency_key.strip()):
            return Refused(rule='input', reason='Actor and a nonblank idempotency key are required')
        if confirm and not idempotency_key:
            return Refused(rule='idempotency_key', reason='Supply the key from the preview')
        key = idempotency_key or secrets.token_hex(16)  # hex: a leading "-" would read as a CLI option
        inputs = {**values, 'idempotency_key': key}
        fingerprint = json.dumps({'verb': verb, 'actor': actor, **inputs}, sort_keys=True, separators=(',', ':'))
        nonce = self.tokens.verify(confirm, f'person {verb}', actor, inputs) if confirm else None
        if isinstance(nonce, Refused):
            return nonce
        with self.registry._transaction() as db:
            if verb in ('grant', 'revoke'):
                refusal = self._authorize(db, actor, 'operator')
                if refusal:
                    return refusal
            if nonce and db.execute('SELECT 1 FROM used_preview_tokens WHERE nonce = ?', (nonce,)).fetchone():
                return Refused(rule='token_used', reason='Preview token has already been used')
            previous = db.execute('SELECT fingerprint, result FROM verb_requests WHERE idempotency_key = ?', (key,)).fetchone()
            if previous and previous[0] != fingerprint:
                return Refused(rule='idempotency_key', reason='Idempotency key was used for a different request')
            if previous and nonce:
                db.execute('INSERT INTO used_preview_tokens VALUES (?)', (nonce,))
                return Done(result=json.loads(previous[1]))
            if verb == 'add':
                page = normalize_page_id(values.get('page_id', ''))
                if not page or values.get('kind') not in ('staff', 'client', 'vendor'):
                    return Refused(rule='input', reason='A page ID and valid kind are required')
                if db.execute("SELECT 1 FROM bindings WHERE system = 'notion' AND page_id = ?", (page,)).fetchone():
                    return Refused(rule='binding', reason='Page is already bound')
                person_id = None
            elif verb in ('set-telegram', 'rebind', 'shift', 'grant', 'revoke'):
                person_id = values.get('person_id')
                target = db.execute('SELECT active FROM persons WHERE id = ?', (person_id,)).fetchone()
                if not target:
                    return Refused(rule='person', reason='Person not found')
            else:
                return Refused(rule='input', reason='Unknown verb')
            if verb == 'shift':
                start, end = values.get('start', ''), values.get('end', '')
                try:
                    if not all(isinstance(v, str) and re.fullmatch(r'\d{2}:\d{2}', v) for v in (start, end)):
                        raise ValueError
                    time.fromisoformat(start)
                    time.fromisoformat(end)
                    if values.get('pattern') not in ('day', 'night'):
                        raise ValueError
                except ValueError:
                    return Refused(rule='shift', reason='Use day or night with valid HH:MM times')
            if verb in ('grant', 'revoke') and values.get('permission') not in PERMISSIONS:
                return Refused(rule='permission', reason='Unknown permission')
            if verb == 'rebind':
                page = normalize_page_id(values.get('page_id', ''))
                if not page:
                    return Refused(rule='input', reason='A page ID is required')
                bound = db.execute("SELECT project_id FROM bindings WHERE system = 'notion' AND page_id = ?", (page,)).fetchone()
                if bound and bound[0] != person_id:
                    return Refused(rule='binding', reason=f'Page is already bound to {bound[0]}')
            telegram = values.get('telegram_id')
            if telegram is not None and (verb == 'add' and values.get('active', True) or verb == 'set-telegram' and target[0]):
                holder = db.execute('SELECT id FROM persons WHERE active = 1 AND telegram_id = ? AND id != ?', (telegram, person_id or '')).fetchone()
                if holder:
                    return Refused(rule='telegram_id', reason=f'Telegram ID is held by {holder[0]}')
            plan = {'action': verb, **values, 'idempotency_key': key}
            if nonce is None:
                return self.tokens.create(f'person {verb}', actor, inputs, plan)
            now = self.registry._clock()
            if verb == 'add':
                person_id = str(T6Id.mint(EntityKind.PERSON))
                db.execute('INSERT INTO persons VALUES (?, ?, ?, ?, ?, ?, ?)', (person_id, values['kind'], json.dumps(values.get('departments', [])), json.dumps(values.get('roles', [])), telegram, int(values.get('master_brief', False)), int(values.get('active', True))))
                db.execute("INSERT INTO bindings (system, page_id, project_id, bound_at, active) VALUES ('notion', ?, ?, ?, 1)", (page, person_id, now))
                before = None
            elif verb == 'rebind':
                current = db.execute("SELECT page_id FROM bindings WHERE system = 'notion' AND project_id = ? AND active = 1", (person_id,)).fetchone()
                before = {'page_id': current[0] if current else None}
                db.execute("UPDATE bindings SET active = 0 WHERE system = 'notion' AND project_id = ?", (person_id,))
                db.execute("INSERT INTO bindings (system, page_id, project_id, bound_at, active) VALUES ('notion', ?, ?, ?, 1) ON CONFLICT(system, page_id) DO UPDATE SET active = 1", (page, person_id, now))
            elif verb == 'shift':
                current = db.execute('SELECT pattern, start, end FROM person_shifts WHERE person_id = ?', (person_id,)).fetchone()
                before = dict(zip(('pattern', 'start', 'end'), current)) if current else None
                db.execute('INSERT INTO person_shifts VALUES (?, ?, ?, ?) ON CONFLICT(person_id) DO UPDATE SET pattern=excluded.pattern, start=excluded.start, end=excluded.end', (person_id, values['pattern'], values['start'], values['end']))
            elif verb in ('grant', 'revoke'):
                permission = values['permission']
                held = db.execute('SELECT 1 FROM person_permissions WHERE person_id = ? AND permission = ?', (person_id, permission)).fetchone() is not None
                before = {'permission': permission, 'held': held}
                if verb == 'grant':
                    db.execute('INSERT OR IGNORE INTO person_permissions VALUES (?, ?)', (person_id, permission))
                else:
                    db.execute('DELETE FROM person_permissions WHERE person_id = ? AND permission = ?', (person_id, permission))
            else:
                before = {'telegram_id': db.execute('SELECT telegram_id FROM persons WHERE id = ?', (person_id,)).fetchone()[0]}
                db.execute('UPDATE persons SET telegram_id = ? WHERE id = ?', (telegram, person_id))
            result = {**plan, 'person_id': person_id}
            assert isinstance(person_id, str)
            _audit(db, person_id, 'person_' + verb.replace('-', '_'), actor, now, before, values)
            db.execute('INSERT INTO used_preview_tokens VALUES (?)', (nonce,))
            db.execute('INSERT INTO verb_requests VALUES (?, ?, ?)', (key, fingerprint, json.dumps(result)))
            return Done(result=result)
