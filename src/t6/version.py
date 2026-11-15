"""Versions and their append-only picture-lock history."""
from __future__ import annotations

import json
import secrets
from datetime import datetime
from typing import Any

from t6.domain.studio_time import MANILA
from t6.deliverable import write_milestone
from t6.ids import EntityKind, T6Id
from t6.person import FRAMEIO_ACTOR
from t6.registry.core import normalize_page_id
from t6.registry.sqlite import SqliteProjectRegistry
from t6.preview import PreviewTokens
from t6.verbs import Done, Outcome, Refused


class VersionStore:
    def __init__(self, registry: SqliteProjectRegistry, tokens: PreviewTokens, reader: Any = None) -> None:
        self.registry, self.tokens, self.reader = registry, tokens, reader

    def for_file(self, file_id: str) -> dict[str, Any] | None:
        with self.registry._transaction() as db:
            row = db.execute('SELECT v.id, v.deliverable_id, v.frameio_file_id, v.frameio_stack_id, v.source_timeline_id, v.note, EXISTS(SELECT 1 FROM version_locks l WHERE l.version_id=v.id) FROM versions v WHERE v.frameio_file_id=?', (file_id,)).fetchone()
            return dict(zip(('id', 'deliverable_id', 'frameio_file_id', 'frameio_stack_id', 'source_timeline_id', 'note', 'locked'), (*row[:6], bool(row[6])))) if row else None

    def get(self, version_id: str) -> dict[str, Any] | None:
        with self.registry._transaction() as db:
            row = db.execute('SELECT id, deliverable_id, frameio_file_id, frameio_stack_id, source_timeline_id, note, EXISTS(SELECT 1 FROM version_locks WHERE version_id=versions.id) FROM versions WHERE id=?', (version_id,)).fetchone()
            if not row:
                return None
            result = dict(zip(('id', 'deliverable_id', 'frameio_file_id', 'frameio_stack_id', 'source_timeline_id', 'note', 'locked'), (*row[:6], bool(row[6]))))
            result['approvals'] = [dict(zip(('id', 'gate', 'state', 'decided_by', 'decided_at', 'reason'), approval)) for approval in db.execute("SELECT id, gate, state, decided_by, decided_at, reason FROM approvals WHERE target=? AND gate='client cut'", (version_id,))]
            return result

    def event_seen(self, event_id: str) -> bool:
        with self.registry._transaction() as db:
            return db.execute('SELECT 1 FROM verb_requests WHERE idempotency_key=?', ('frameio-event:' + event_id,)).fetchone() is not None

    def record_from_frameio(self, event_id: str, folder_id: str, file_id: str, stack_id: str | None) -> str:
        """Record the Version a file landing in a Deliverable's Frame.io folder makes, at most once per event.

        The folder is a `frameio-folder` Media location with role review, owned by a Deliverable. Returns
        `recorded`, `known` (the event or the file is already recorded, nothing changes) or `not a T6 folder`.
        """
        key = 'frameio-event:' + event_id
        with self.registry._transaction() as db:
            if db.execute('SELECT 1 FROM verb_requests WHERE idempotency_key=?', (key,)).fetchone():
                return 'known'
            owners = db.execute("SELECT DISTINCT owner_id, project_id FROM media_locations WHERE kind='frameio-folder' AND role='review' AND active=1 AND ref=?", (folder_id,)).fetchall()
            if len(owners) != 1:
                return 'not a T6 folder'
            deliverable, project = owners[0]
            if db.execute('SELECT 1 FROM versions WHERE frameio_file_id=?', (file_id,)).fetchone():
                result, outcome = {'deliverable_id': deliverable, 'frameio_file_id': file_id, 'recorded': False}, 'known'
            else:
                now, version_id = self.registry._clock(), str(T6Id.mint(EntityKind.VERSION))
                db.execute('INSERT INTO versions VALUES (?, ?, ?, ?, NULL, NULL)', (version_id, deliverable, file_id, stack_id))
                result, outcome = {'version_id': version_id, 'deliverable_id': deliverable, 'project_id': project, 'frameio_file_id': file_id, 'frameio_stack_id': stack_id, 'recorded': True}, 'recorded'
                audit = db.execute('INSERT INTO audit (project_id, action, actor, at, before, after) VALUES (?, ?, ?, ?, NULL, ?)', (version_id, 'version_add', FRAMEIO_ACTOR, now, json.dumps(result)))
                db.execute('INSERT INTO activity (project_id, subject_id, at, actor, action, summary, audit_id) VALUES (?, ?, ?, ?, ?, ?, ?)', (project, version_id, now, FRAMEIO_ACTOR, 'version_add', 'version add', audit.lastrowid))
            db.execute('INSERT INTO verb_requests VALUES (?, ?, ?)', (key, event_id, json.dumps(result)))
            return outcome

    def write(self, verb: str, values: dict[str, Any], *, actor: str, idempotency_key: str | None = None, confirm: str | None = None) -> Outcome:
        if not actor.strip() or any(not isinstance(v, str) or not v.strip() for v in ([values.get('deliverable'), values.get('frameio_file')] if verb == 'add' else [values.get('version')])):
            return Refused(rule='input', reason='Required values must be nonblank')
        if (idempotency_key is not None and not idempotency_key.strip()) or (confirm and not idempotency_key):
            return Refused(rule='idempotency_key', reason='A nonblank idempotency key is required')
        key = idempotency_key or secrets.token_hex(16)
        inputs = {**values, 'idempotency_key': key}
        name = 'version ' + verb
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
            if verb == 'add':
                ref = values['deliverable']
                row = db.execute("SELECT id, project_id FROM deliverables WHERE id=? OR id=(SELECT project_id FROM bindings WHERE system='notion' AND page_id=?)", (ref, normalize_page_id(ref))).fetchone()
                if not row:
                    return Refused(rule='deliverable', reason='Deliverable not found')
                existing = db.execute('SELECT id, deliverable_id FROM versions WHERE frameio_file_id=?', (values['frameio_file'],)).fetchone()
                if existing and existing[1] != row[0]:
                    return Refused(rule='frameio_file', reason='File belongs to another Deliverable')
                timeline = values.get('timeline')
                if timeline is not None and not db.execute('SELECT 1 FROM timelines WHERE id=? AND project_id=?', (timeline, row[1])).fetchone():
                    return Refused(rule='timeline', reason='Timeline is not linked to the Deliverable\'s Project')
                if nonce is None:
                    if self.reader is None:
                        return Refused(rule='frameio', reason='Frame.io reader is not configured')
                    from t6.frameio.requests import Failed
                    versions = self.reader.versions(values['frameio_file'])
                    if isinstance(versions, Failed):
                        return Refused(rule='frameio', reason=versions.reason)
                    match = next((v for v in versions if v.file_id == values['frameio_file']), None)
                    if match is None:
                        return Refused(rule='frameio_file', reason='Frame.io file not found')
                    plan = {'deliverable_id': row[0], 'project_id': row[1], 'frameio_file_id': values['frameio_file'], 'frameio_stack_id': match.stack_id, 'note': values.get('note'), 'source_timeline_id': timeline, 'idempotency_key': key}
                else:
                    plan = None
            else:
                row = db.execute('SELECT v.id, v.deliverable_id, d.project_id FROM versions v JOIN deliverables d ON d.id=v.deliverable_id WHERE v.id=?', (values['version'],)).fetchone()
                if not row:
                    return Refused(rule='version', reason='Version not found')
                old = db.execute('SELECT version_id FROM version_locks WHERE deliverable_id=?', (row[1],)).fetchone()
                old_id = old[0] if old else None
                plan = {'version_id': row[0], 'deliverable_id': row[1], 'project_id': row[2], 'previous_version_id': old_id, 'idempotency_key': key}
                if old_id and old_id != row[0]:
                    plan['message'] = f'this moves the lock from {old_id} to {row[0]}'
            if nonce is None:
                preview = self.tokens.create(name, actor, inputs, plan)
                preview_nonce = self.tokens.verify(preview.token, name, actor, inputs)
                db.execute('INSERT INTO jo_state_previews VALUES (?, ?)', (preview_nonce, json.dumps(plan)))
                return preview
            observed = db.execute('SELECT state FROM jo_state_previews WHERE nonce=?', (nonce,)).fetchone()
            if not observed:
                return Refused(rule='preview', reason='Preview not found')
            saved = json.loads(observed[0])
            if verb == 'lock' and saved != plan:
                return Refused(rule='lock_changed', reason='Lock changed since preview; preview again')
            now = self.registry._clock()
            if verb == 'add':
                if saved['deliverable_id'] != row[0]:
                    return Refused(rule='deliverable_changed', reason='Deliverable changed since preview')
                existing = db.execute('SELECT id, deliverable_id FROM versions WHERE frameio_file_id=?', (values['frameio_file'],)).fetchone()
                if existing and existing[1] != row[0]:
                    return Refused(rule='frameio_file', reason='File belongs to another Deliverable')
                identifier = existing[0] if existing else str(T6Id.mint(EntityKind.VERSION))
                if not existing:
                    db.execute('INSERT INTO versions VALUES (?, ?, ?, ?, ?, ?)', (identifier, row[0], values['frameio_file'], saved['frameio_stack_id'], saved.get('source_timeline_id'), values.get('note')))
                result = {**saved, 'version_id': identifier}
                changed = not existing
            else:
                changed = saved['previous_version_id'] != row[0]
                result = saved
                if changed:
                    from t6.deliverable import deliverable_status, intend_status_change
                    before_status = deliverable_status(db, row[1])
                    milestone = write_milestone(db, row[1], 'piclock', datetime.fromtimestamp(now, MANILA).date().isoformat(), actor, now)
                    intend_status_change(db, row[1], before_status, now)
                    db.execute('INSERT INTO version_locks VALUES (?, ?) ON CONFLICT(deliverable_id) DO UPDATE SET version_id=excluded.version_id', (row[1], row[0]))
                    result = {**saved, 'milestone_id': milestone}
            if changed:
                cursor = db.execute('INSERT INTO audit (project_id, action, actor, at, before, after) VALUES (?, ?, ?, ?, ?, ?)', (result['version_id'], 'version_' + verb, actor, now, json.dumps(saved.get('previous_version_id')), json.dumps(result)))
                db.execute('INSERT INTO activity (project_id, subject_id, at, actor, action, summary, audit_id) VALUES (?, ?, ?, ?, ?, ?, ?)', (result['project_id'], result['version_id'], now, actor, 'version_' + verb, 'version ' + verb, cursor.lastrowid))
            db.execute('INSERT INTO used_preview_tokens VALUES (?)', (nonce,))
            db.execute('INSERT INTO verb_requests VALUES (?, ?, ?)', (key, fingerprint, json.dumps(result)))
            return Done(result=result)
