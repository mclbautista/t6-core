"""Preview and atomically confirm Project hold and resume writes."""
from __future__ import annotations

import json
import secrets
from typing import Any

from t6.preview import PreviewTokens
from t6.registry.sqlite import SqliteProjectRegistry, _audit, _intend_sync
from t6.verbs import Done, Outcome, Preview, Refused


class ProjectWrites:
    def __init__(self, registry: SqliteProjectRegistry, tokens: PreviewTokens) -> None:
        self.registry = registry
        self.tokens = tokens

    def run(self, verb: str, project: str, reason: str | None, *, actor: str,
            idempotency_key: str | None = None, confirm: str | None = None) -> Outcome:
        if verb not in ('hold', 'resume') or not project.strip() or not actor.strip():
            return Refused(rule='input', reason='A verb, Project and actor are required')
        if verb == 'hold' and (reason is None or not reason.strip()):
            return Refused(rule='reason', reason='A reason is required to hold a Project')
        if idempotency_key is not None and not idempotency_key.strip():
            return Refused(rule='idempotency_key', reason='Idempotency key must not be blank')
        if confirm and idempotency_key is None:
            return Refused(rule='idempotency_key', reason='Supply the key from the preview')
        key = idempotency_key or secrets.token_urlsafe(24)
        inputs = {'project': project, 'reason': reason, 'idempotency_key': key}
        fingerprint = json.dumps({'verb': verb, 'actor': actor, **inputs}, sort_keys=True, separators=(',', ':'))
        nonce: str | Refused | None = None
        if confirm:
            nonce = self.tokens.verify(confirm, f'project {verb}', actor, inputs)
            if isinstance(nonce, Refused):
                return nonce
        with self.registry._transaction() as db:
            if nonce is not None and db.execute('SELECT 1 FROM used_preview_tokens WHERE nonce = ?', (nonce,)).fetchone():
                return Refused(rule='token_used', reason='Preview token has already been used')
            previous = db.execute('SELECT fingerprint, result FROM verb_requests WHERE idempotency_key = ?', (key,)).fetchone()
            if previous is not None and previous[0] != fingerprint:
                return Refused(rule='idempotency_key', reason='Idempotency key was used for a different request')
            if previous is not None and nonce is not None:
                db.execute('INSERT INTO used_preview_tokens VALUES (?)', (nonce,))
                return Done(result=json.loads(previous[1]))
            row = db.execute('SELECT id, revision FROM projects WHERE id = ?', (project,)).fetchone()
            if row is None:
                from t6.registry import normalize_code
                _, form = normalize_code(project)
                matches = db.execute("SELECT id, revision FROM projects WHERE code_form = ? AND rotation = 'in'", (form,)).fetchall()
                row = matches[0] if len(matches) == 1 else None
            if row is None:
                return Refused(rule='project', reason='Project not found')
            project_id, revision = row
            state = db.execute('SELECT held, reason FROM project_hold_state WHERE project_id = ?', (project_id,)).fetchone()
            held, old_reason = (bool(state[0]), state[1]) if state else (False, None)
            desired = verb == 'hold'
            if previous is None and held == desired:
                return Refused(rule='project_held' if held else 'project_not_held',
                               reason='Project is already held' if held else 'Project is not held')
            plan: dict[str, Any] = {'project_id': project_id, 'action': verb, 'reason': reason,
                                    'idempotency_key': key, 'status': 'held' if desired else 'resumed'}
            if nonce is None:
                return self.tokens.create(f'project {verb}', actor, inputs, plan)
            now = self.registry._clock()
            result = dict(plan)
            db.execute('INSERT INTO project_hold_state VALUES (?, ?, ?) ON CONFLICT(project_id) DO UPDATE SET held=excluded.held, reason=excluded.reason',
                       (project_id, int(desired), reason if desired else None))
            db.execute('UPDATE projects SET revision = revision + 1 WHERE id = ?', (project_id,))
            _audit(db, project_id, f'project_{verb}', actor, now,
                   {'held': held, 'reason': old_reason}, {'held': desired, 'reason': reason if desired else None})
            _intend_sync(db, project_id, revision + 1, now)
            db.execute('INSERT INTO status_sync_intents VALUES (last_insert_rowid(), ?)', (int(desired),))
            db.execute('INSERT INTO used_preview_tokens VALUES (?)', (nonce,))
            db.execute('INSERT INTO verb_requests VALUES (?, ?, ?)', (key, fingerprint, json.dumps(result)))
            return Done(result=result)
