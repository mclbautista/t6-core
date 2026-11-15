"""Media locations and their append-only history."""
from __future__ import annotations

import builtins
from contextlib import nullcontext
import hashlib
import json
import secrets
from typing import Any

from t6.ids import EntityKind, T6Id
from t6.media_links import MediaLink
from t6.preview import PreviewTokens
from t6.registry.sqlite import SqliteProjectRegistry
from t6.registry.core import normalize_page_id
from t6.verbs import Done, Outcome, Refused

KINDS = {'drive', 'nas-path', 'frameio-project', 'frameio-folder', 'resolve-project', 'timeline', 'instagantt-project'}
# Kinds whose `ref` one Project holds at a time: the rule name and what the Refused reason calls it.
ONE_PROJECT_KINDS = {'frameio-project': ('frameio_project', 'Frame.io project'), 'instagantt-project': ('instagantt_project', 'Instagantt project')}
ROLES = {'master', 'backup', 'review', 'working', 'frameio-project'}


# What the retired External Links rows called their System and Kind, and the Media location kind each became.
# An Instagantt row of either Kind is an Instagantt project (not a t6t_ Timeline). Frame.io rows are found by
# `external-link find` but the migration does not import them.
_EXTERNAL_KINDS = {('frameio', 'project'): 'frameio-project', ('resolve', 'project'): 'resolve-project',
                   ('instagantt', 'project'): 'instagantt-project', ('instagantt', 'timeline'): 'instagantt-project'}


class MediaStore:
    def __init__(self, registry: SqliteProjectRegistry, tokens: PreviewTokens) -> None:
        self.registry, self.tokens = registry, tokens

    def list(self, owner: str) -> list[dict[str, Any]]:
        with self.registry._transaction() as db:
            return [dict(zip(('id', 'owner', 'kind', 'ref', 'role'), row)) for row in db.execute(
                'SELECT id, owner_id, kind, ref, role FROM media_locations WHERE (owner_id=? OR owner_id=(SELECT project_id FROM deliverables WHERE id=?)) AND active=1 ORDER BY id', (owner, owner))]

    def write(self, verb: str, values: dict[str, Any], *, actor: str, idempotency_key: str | None = None, confirm: str | None = None, noun: str = 'media') -> Outcome:
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
        name = noun + ' ' + verb
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
                if values['kind'] in ONE_PROJECT_KINDS:
                    rule, label = ONE_PROJECT_KINDS[values['kind']]
                    holder = db.execute("SELECT project_id FROM media_locations WHERE kind=? AND ref=? AND active=1 AND project_id<>?", (values['kind'], values['ref'], project[0])).fetchone()
                    if holder:
                        return Refused(rule=rule, reason=label + ' belongs to ' + holder[0])
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

    def frameio_project_write(self, verb: str, values: dict[str, Any], *, actor: str, idempotency_key: str | None = None, confirm: str | None = None) -> Outcome:
        """`frameio project link` and `unlink`: the Media location rules for one Frame.io project of one Project."""
        project, ref = values.get('project'), values.get('frameio_project')
        if verb not in ('link', 'unlink') or not isinstance(ref, str) or not ref.strip() or not isinstance(project, str):
            return Refused(rule='input', reason='A valid verb, Project and Frame.io project are required')
        try:
            T6Id.parse(project, EntityKind.PROJECT)
        except ValueError:
            return Refused(rule='project', reason='Project must be a T6 Project ID')
        if verb == 'link':
            return self.write('link', {'owner': project, 'kind': 'frameio-project', 'ref': ref, 'role': 'frameio-project'},
                              actor=actor, idempotency_key=idempotency_key, confirm=confirm, noun='frameio project')
        with self.registry._transaction() as db:
            row = db.execute("SELECT id FROM media_locations WHERE owner_id=? AND kind='frameio-project' AND ref=? AND active=1", (project, ref)).fetchone()
        if not row:
            return Refused(rule='frameio_project', reason='Frame.io project is not linked to this Project')
        return self.write('unlink', {'media': row[0]}, actor=actor, idempotency_key=idempotency_key, confirm=confirm, noun='frameio project')

    def migrate(self, rows: builtins.list[dict[str, Any]], *, actor: str, apply: bool = False,
                digest: str | None = None, _db: Any = None) -> dict[str, Any]:
        """Preview or import only rows that can be classified by stable page bindings."""
        rows = sorted(rows, key=lambda row: normalize_page_id(row['page_id']))
        snapshot = json.dumps(rows, sort_keys=True)
        with (self.registry._transaction() if _db is None else nullcontext(_db)) as db:
            entries: builtins.list[dict[str, Any]] = []
            held_by: dict[str, str] = {}
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
                elif (media_kind := _EXTERNAL_KINDS.get((system, kind))) is None or not row.get('ref'):
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
                        holder = project[0]
                        if media_kind == 'instagantt-project':  # one Instagantt project belongs to one Project at a time
                            holder = held_by.setdefault(row['ref'], project[0])
                            other = db.execute("SELECT project_id FROM media_locations WHERE kind=? AND ref=? AND active=1 AND project_id<>?", (media_kind, row['ref'], project[0])).fetchone()
                            holder = other[0] if other else holder
                        if holder != project[0]:
                            item['reason'] = 'Instagantt project already belongs to another Project'
                        else:
                            item.update(action='register', owner=owner, project_id=project[0], kind=media_kind, ref=row['ref'], role='working')
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


def active_links(registry: SqliteProjectRegistry, kind: str, role: str | None = None) -> tuple[MediaLink, ...]:
    """The active Media locations of one kind, and one role when given, in ID order. Reads only."""
    with registry._transaction() as db:
        rows = db.execute('SELECT id, owner_id, project_id, kind, ref, role FROM media_locations '
                          'WHERE kind=? AND active=1 AND (? IS NULL OR role=?) ORDER BY id', (kind, role, role)).fetchall()
    return tuple(_link(registry, row) for row in rows)


def _link(registry: SqliteProjectRegistry, row: Any) -> MediaLink:
    project = registry.get(row[2])
    pages = tuple(b.page_id for b in sorted((b for b in project.bindings if b.system == 'notion'), key=lambda b: not b.active)) if project else ()
    return MediaLink(id=row[0], owner=row[1], project_id=row[2], kind=row[3], ref=row[4], role=row[5], pages=pages)




def find_external(registry: SqliteProjectRegistry, system: str, kind: str, external_id: str) -> MediaLink | None:
    """The active Media location that answers an old `external-link find` question, or None."""
    media_kind = _EXTERNAL_KINDS.get((system.casefold().replace('.', '').replace(' ', ''), kind.casefold()))
    if media_kind is None:
        return None
    return next((link for link in active_links(registry, media_kind) if link.ref == external_id), None)


def external_link_result(registry: SqliteProjectRegistry, system: str, kind: str, external_id: str) -> dict[str, Any] | None:
    """`external-link find`'s answer, in its old shape, from Media locations: `page_id` is the Media location ID."""
    found = find_external(registry, system, kind, external_id)
    if found is None:
        return None
    return {'page_id': found.id, 'ref': {'system': system, 'kind': kind, 'external_id': found.ref, 'url': None, 'internal_id': None},
            'active': True, 'last_activity': None, 'project_ids': [found.project_id]}


class DatabaseMediaLinks:
    """`MediaLinks` read straight from the t6 database, for a process that already opens it."""

    def __init__(self, registry: SqliteProjectRegistry) -> None:
        self._registry = registry

    def media_links(self, kind: str, role: str | None = None) -> tuple[MediaLink, ...]:
        return active_links(self._registry, kind, role)
