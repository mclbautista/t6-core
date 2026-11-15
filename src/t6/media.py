"""Media locations and their append-only history."""
from __future__ import annotations

import builtins
from contextlib import nullcontext
import hashlib
import json
import secrets
from typing import Any

from t6.ids import EntityKind, T6Id
from t6.preview import PreviewTokens
from t6.registry.sqlite import SqliteProjectRegistry
from t6.registry.core import normalize_page_id
from t6.verbs import Done, Outcome, Refused

KINDS = {'drive', 'nas-path', 'frameio-project', 'frameio-folder', 'resolve-project', 'timeline'}
ROLES = {'master', 'backup', 'review', 'working', 'frameio-project'}


class MediaStore:
    def __init__(self, registry: SqliteProjectRegistry, tokens: PreviewTokens) -> None:
        self.registry, self.tokens = registry, tokens

    def list(self, owner: str) -> list[dict[str, Any]]:
        with self.registry._transaction() as db:
            return [dict(zip(('id', 'owner', 'kind', 'ref', 'role'), row)) for row in db.execute(
                'SELECT id, owner_id, kind, ref, role FROM media_locations WHERE (owner_id=? OR owner_id=(SELECT project_id FROM deliverables WHERE id=?)) AND active=1 ORDER BY id', (owner, owner))]

    def write(self, verb: str, values: dict[str, Any], *, actor: str, idempotency_key: str | None = None, confirm: str | None = None) -> Outcome:
        if not actor.strip() or verb not in ('link', 'unlink'):
            return Refused(rule='input', reason='A valid actor and verb are required')
        if verb == 'link' and (values.get('kind') not in KINDS or values.get('role') not in ROLES or not isinstance(values.get('owner'), str) or not isinstance(values.get('ref'), str) or not values['ref'].strip()):
            return Refused(rule='input', reason='A valid owner, kind, ref and role are required')
        if verb == 'link' and values.get('kind') in ('drive', 'timeline'):
            try:
                T6Id.parse(values['ref'], EntityKind.DRIVE if values['kind'] == 'drive' else EntityKind.TIMELINE)
            except ValueError:
                return Refused(rule='ref', reason='Reference must be a T6 ID of the expected kind')
        if verb == 'unlink' and not isinstance(values.get('media'), str):
            return Refused(rule='input', reason='A media location is required')
        if (idempotency_key is not None and not idempotency_key.strip()) or (confirm and not idempotency_key):
            return Refused(rule='idempotency_key', reason='A nonblank idempotency key is required')
        key = idempotency_key or secrets.token_hex(16)
        inputs = {**values, 'idempotency_key': key}
        name = 'media ' + verb
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
            if verb == 'link':
                owner = values['owner']
                project = db.execute('SELECT id FROM projects WHERE id=? UNION ALL SELECT project_id FROM deliverables WHERE id=?', (owner, owner)).fetchone()
                if not project:
                    return Refused(rule='owner', reason='Project or Deliverable not found')
                if values['kind'] == 'frameio-folder' and (not owner.startswith('t6v_') or values['role'] != 'review'):
                    return Refused(rule='frameio_folder', reason='Frame.io folders belong to a Deliverable with role review')
                existing = db.execute('SELECT id, active FROM media_locations WHERE owner_id=? AND kind=? AND ref=? AND role=?', (owner, values['kind'], values['ref'], values['role'])).fetchone()
                if values['kind'] == 'frameio-project':
                    holder = db.execute("SELECT project_id FROM media_locations WHERE kind='frameio-project' AND ref=? AND active=1 AND project_id<>?", (values['ref'], project[0])).fetchone()
                    if holder:
                        return Refused(rule='frameio_project', reason='Frame.io project belongs to ' + holder[0])
                media_id = existing[0] if existing else str(T6Id.mint(EntityKind.MEDIA_LOCATION))
                plan = {'media_id': media_id, 'project_id': project[0], **values, 'idempotency_key': key}
            else:
                row = db.execute('SELECT project_id, active FROM media_locations WHERE id=?', (values['media'],)).fetchone()
                if not row:
                    return Refused(rule='media', reason='Media location not found')
                plan = {'media_id': values['media'], 'project_id': row[0], 'idempotency_key': key}
            if not nonce:
                preview = self.tokens.create(name, actor, inputs, plan)
                preview_nonce = self.tokens.verify(preview.token, name, actor, inputs)
                db.execute('INSERT INTO media_previews VALUES (?, ?)', (preview_nonce, json.dumps({'plan': plan, 'state': list(existing) if verb == 'link' and existing else list(row) if verb == 'unlink' else None})))
                return preview
            saved = db.execute('SELECT snapshot FROM media_previews WHERE nonce=?', (nonce,)).fetchone()
            if not saved:
                return Refused(rule='preview', reason='Preview not found')
            snapshot = json.loads(saved[0])
            current = list(existing) if verb == 'link' and existing else list(row) if verb == 'unlink' else None
            if snapshot['state'] != current:
                return Refused(rule='media_changed', reason='Media location changed since preview')
            preview_plan = snapshot['plan']
            if verb == 'link':
                media_id = preview_plan['media_id']
                plan['media_id'] = media_id
            now = self.registry._clock()
            if verb == 'link':
                db.execute('INSERT INTO media_locations VALUES (?, ?, ?, ?, ?, ?, 1, NULL) ON CONFLICT(id) DO UPDATE SET active=1', (media_id, values['owner'], project[0], values['kind'], values['ref'], values['role']))
            else:
                db.execute('UPDATE media_locations SET active=0 WHERE id=?', (plan['media_id'],))
            db.execute('INSERT INTO media_history (media_id, action, actor, at) VALUES (?, ?, ?, ?)', (plan['media_id'], verb, actor, now))
            audit = db.execute('INSERT INTO audit (project_id, action, actor, at, before, after) VALUES (?, ?, ?, ?, NULL, ?)', (plan['media_id'], name.replace(' ', '_'), actor, now, json.dumps(plan)))
            db.execute('INSERT INTO activity (project_id, subject_id, at, actor, action, summary, audit_id) VALUES (?, ?, ?, ?, ?, ?, ?)', (plan['project_id'], plan['media_id'], now, actor, name.replace(' ', '_'), name, audit.lastrowid))
            db.execute('INSERT INTO used_preview_tokens VALUES (?)', (nonce,))
            db.execute('INSERT INTO verb_requests VALUES (?, ?, ?)', (key, fingerprint, json.dumps(plan)))
            return Done(result=plan)

    def migrate(self, rows: builtins.list[dict[str, Any]], *, actor: str, apply: bool = False,
                digest: str | None = None, _db: Any = None) -> dict[str, Any]:
        """Preview or import only rows that can be classified by stable page bindings."""
        rows = sorted(rows, key=lambda row: normalize_page_id(row['page_id']))
        snapshot = json.dumps(rows, sort_keys=True)
        with (self.registry._transaction() if _db is None else nullcontext(_db)) as db:
            entries: builtins.list[dict[str, Any]] = []
            for row in rows:
                page = normalize_page_id(row['page_id'])
                previous = db.execute('SELECT id FROM media_locations WHERE source_page=?', (page,)).fetchone()
                item: dict[str, Any] = {'page_id': page, 'action': 'review', 't6_id': None}
                system = str(row.get('system') or '').casefold().replace('.', '').replace(' ', '')
                kind = str(row.get('kind') or '').casefold()
                if previous:
                    item.update(action='keep', t6_id=previous[0])
                elif row.get('active', True) is not True:
                    item['reason'] = 'Deactivated in External Links'
                elif system == 'frameio':
                    item['reason'] = 'Frame.io links are not migrated'
                elif system == 'telegram':
                    item['reason'] = 'Telegram chats are not Media locations'
                elif system != 'resolve' or kind != 'project' or not row.get('ref'):
                    item['reason'] = 'Cannot classify External Links row'
                else:
                    owners: builtins.list[str | None] = []
                    for relation, table in (('projects', 'projects'), ('deliverables', 'deliverables')):
                        for external in row.get(relation, []):
                            binding = db.execute(f"SELECT t.id FROM {table} t JOIN bindings b ON b.project_id=t.id WHERE b.system='notion' AND b.page_id=?", (normalize_page_id(external),)).fetchone()
                            if not binding:
                                owners.append(None)
                            elif binding[0] not in owners:
                                owners.append(binding[0])
                    if len(owners) == 2 and None not in owners:
                        deliverables = [value for value in owners if db.execute('SELECT 1 FROM deliverables WHERE id=?', (value,)).fetchone()]
                        if len(deliverables) == 1:
                            parent = db.execute('SELECT project_id FROM deliverables WHERE id=?', (deliverables[0],)).fetchone()[0]
                            if parent in owners:
                                owners = deliverables
                    if len(owners) != 1 or owners[0] is None:
                        item['reason'] = 'Owner is missing or ambiguous'
                    else:
                        owner = owners[0]
                        project = db.execute('SELECT id FROM projects WHERE id=? UNION ALL SELECT project_id FROM deliverables WHERE id=?', (owner, owner)).fetchone()
                        item.update(action='register', owner=owner, project_id=project[0], kind='resolve-project', ref=row['ref'], role='working')
                entries.append(item)
            fingerprint = hashlib.sha256(json.dumps({'rows': rows, 'entries': entries}, sort_keys=True).encode()).hexdigest()
            if apply and digest != fingerprint:
                receipt = db.execute('SELECT snapshot, entries FROM media_migration_receipts WHERE digest=?', (digest,)).fetchone()
                if not receipt or receipt[0] != snapshot:
                    raise ValueError('External Links changed since preview; preview again')
                prior = json.loads(receipt[1])
                if any(old.get('t6_id') != new.get('t6_id') for old, new in zip(prior, entries)):
                    raise ValueError('Media bindings changed since preview; preview again')
                return {'digest': digest, 'entries': entries, 'review': [e for e in entries if e['action'] == 'review']}
            if apply:
                for entry in entries:
                    if entry['action'] != 'register':
                        continue
                    identifier = str(T6Id.mint(EntityKind.MEDIA_LOCATION))
                    now = self.registry._clock()
                    db.execute('INSERT INTO media_locations VALUES (?, ?, ?, ?, ?, ?, 1, ?)', (identifier, entry['owner'], entry['project_id'], entry['kind'], entry['ref'], entry['role'], entry['page_id']))
                    db.execute('INSERT INTO media_history (media_id, action, actor, at) VALUES (?, ?, ?, ?)', (identifier, 'link', actor, now))
                    audit = db.execute('INSERT INTO audit (project_id, action, actor, at, before, after) VALUES (?, ?, ?, ?, NULL, ?)', (identifier, 'media_migrate', actor, now, json.dumps(entry)))
                    db.execute('INSERT INTO activity (project_id, subject_id, at, actor, action, summary, audit_id) VALUES (?, ?, ?, ?, ?, ?, ?)', (entry['project_id'], identifier, now, actor, 'media_migrate', 'Migrate media location', audit.lastrowid))
                    entry.update(action='registered', t6_id=identifier)
                db.execute('INSERT OR IGNORE INTO media_migration_receipts VALUES (?, ?, ?)', (fingerprint, snapshot, json.dumps(entries)))
            return {'digest': fingerprint, 'entries': entries, 'review': [e for e in entries if e['action'] == 'review']}

    def migrate_write(self, rows: builtins.list[dict[str, Any]], *, actor: str,
                      idempotency_key: str | None, confirm: str | None) -> Outcome:
        if not actor or (confirm and not idempotency_key) or (idempotency_key is not None and not idempotency_key.strip()):
            return Refused(rule='idempotency_key', reason='Actor and nonblank idempotency key are required')
        key = idempotency_key or secrets.token_hex(16)
        inputs = {'idempotency_key': key, 'rows': sorted(rows, key=lambda r: r['page_id'])}
        name = 'admin media migrate'
        fingerprint = json.dumps({'verb': name, 'actor': actor, **inputs}, sort_keys=True, separators=(',', ':'))
        nonce = self.tokens.verify(confirm, name, actor, inputs) if confirm else None
        if isinstance(nonce, Refused):
            return nonce
        with self.registry._transaction() as db:
            if nonce and db.execute('SELECT 1 FROM used_preview_tokens WHERE nonce=?', (nonce,)).fetchone():
                return Refused(rule='token_used', reason='Preview token has already been used')
            previous = db.execute('SELECT fingerprint, result FROM verb_requests WHERE idempotency_key=?', (key,)).fetchone()
            if previous and previous[0] != fingerprint:
                return Refused(rule='idempotency_key', reason='Idempotency key was used for another request')
            if previous:
                if nonce:
                    db.execute('INSERT INTO used_preview_tokens VALUES (?)', (nonce,))
                    return Done(result=json.loads(previous[1]))
                return self.tokens.create(name, actor, inputs, json.loads(previous[1]))
            report = self.migrate(rows, actor=actor, _db=db)
            if not nonce:
                plan = {**report, 'idempotency_key': key}
                preview = self.tokens.create(name, actor, inputs, plan)
                preview_nonce = self.tokens.verify(preview.token, name, actor, inputs)
                db.execute('INSERT INTO media_previews VALUES (?, ?)', (preview_nonce, report['digest']))
                return preview
            saved = db.execute('SELECT snapshot FROM media_previews WHERE nonce=?', (nonce,)).fetchone()
            if not saved or saved[0] != report['digest']:
                return Refused(rule='migration_changed', reason='External Links or bindings changed since preview')
            result = {**self.migrate(rows, actor=actor, apply=True, digest=saved[0], _db=db), 'idempotency_key': key}
            db.execute('INSERT INTO used_preview_tokens VALUES (?)', (nonce,))
            db.execute('INSERT INTO verb_requests VALUES (?, ?, ?)', (key, fingerprint, json.dumps(result)))
            return Done(result=result)
