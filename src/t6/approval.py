"""Approval records and previewed decisions."""
from __future__ import annotations

import json
import secrets
from typing import Any

from t6.ids import EntityKind, T6Id
from t6.operator.store import _validate_rule
from t6.person import People
from t6.preview import PreviewTokens
from t6.registry.sqlite import SqliteProjectRegistry
from t6.registry.core import normalize_page_id
from t6.verbs import Done, Outcome, Refused

GATES = ('vfx committee', 'client cut', 'grade', 'delivery sign-off', 'drive wipe', 'fleet promotion', 'project start')


class Approvals:
    def __init__(self, registry: SqliteProjectRegistry, tokens: PreviewTokens) -> None:
        self.registry, self.tokens = registry, tokens

    def get(self, approval_id: str) -> dict[str, Any] | None:
        with self.registry._transaction() as db:
            row = db.execute('SELECT id, gate, target, requester, tapper_rule, state, decided_by, decided_at, reason FROM approvals WHERE id = ?', (approval_id,)).fetchone()
        if row is None:
            return None
        return dict(zip(('id', 'gate', 'target', 'requester', 'tapper_rule', 'state', 'decided_by', 'decided_at', 'reason'), (*row[:4], json.loads(row[4]), *row[5:])))

    def requested_under(self, key: str, gate: str, target: str) -> bool:
        """Whether the Approval with this gate for this target was requested under this idempotency key.

        The key's own record names the Approval it made (its stored plan carries the Approval ID), so a key that only
        looks alike, or was used for another gate or target, does not count.
        """
        with self.registry._transaction() as db:
            row = db.execute('SELECT json_extract(r.result, \'$.approval_id\') FROM verb_requests r WHERE r.idempotency_key = ?', (key,)).fetchone()
            if row is None or row[0] is None:
                return False
            return db.execute('SELECT 1 FROM approvals WHERE id = ? AND gate = ? AND target = ?', (row[0], gate, target)).fetchone() is not None

    def write(self, verb: str, values: dict[str, Any], *, actor: str, idempotency_key: str | None = None, confirm: str | None = None) -> Outcome:
        if verb not in ('request', 'grant', 'deny'):
            return Refused(rule='verb', reason='Unknown Approval verb')
        if not idempotency_key and confirm or idempotency_key is not None and not idempotency_key.strip():
            return Refused(rule='idempotency_key', reason='A nonblank idempotency key is required')
        key = idempotency_key or secrets.token_hex(16)
        inputs = {**values, 'idempotency_key': key}
        fingerprint = json.dumps({'verb': verb, 'actor': actor, **inputs}, sort_keys=True, separators=(',', ':'))
        nonce = self.tokens.verify(confirm, 'approval ' + verb, actor, inputs) if confirm else None
        if isinstance(nonce, Refused):
            return nonce
        with self.registry._transaction() as db:
            if nonce and db.execute('SELECT 1 FROM used_preview_tokens WHERE nonce = ?', (nonce,)).fetchone():
                return Refused(rule='token_used', reason='Preview token has already been used')
            previous = db.execute('SELECT fingerprint, result FROM verb_requests WHERE idempotency_key = ?', (key,)).fetchone()
            if previous and previous[0] != fingerprint:
                return Refused(rule='idempotency_key', reason='Idempotency key was used for a different request')
            if previous:
                result = json.loads(previous[1])
                if nonce:
                    db.execute('INSERT INTO used_preview_tokens VALUES (?)', (nonce,))
                    return Done(result=result)
                return self.tokens.create('approval ' + verb, actor, inputs, result)
            person = db.execute('SELECT active FROM persons WHERE id = ?', (actor,)).fetchone()
            if not person or not person[0]:
                return Refused(rule='person', reason='An active Person is required')
            if verb == 'request':
                gate = values.get('gate', '').lower().replace('_', ' ').replace('-', ' ')
                gates = {g.replace('-', ' '): g for g in GATES}
                if gate not in gates:
                    return Refused(rule='gate', reason='Unknown Approval gate')
                target = values.get('target')
                if not isinstance(target, str) or not target.strip():
                    return Refused(rule='target', reason='A target is required')
                if gates[gate] == 'client cut':
                    deliverable = db.execute("SELECT id FROM deliverables WHERE id=? OR id=(SELECT project_id FROM bindings WHERE system='notion' AND page_id=?)", (target, normalize_page_id(target))).fetchone()
                    if not deliverable:
                        return Refused(rule='deliverable', reason='Deliverable not found')
                    version = values.get('version')
                    if version is not None:
                        row = db.execute('SELECT id FROM versions WHERE id=? AND deliverable_id=?', (version, deliverable[0])).fetchone()
                    else:
                        row = db.execute('SELECT id FROM versions WHERE deliverable_id=? ORDER BY rowid DESC LIMIT 1', (deliverable[0],)).fetchone()
                    if not row:
                        return Refused(rule='version', reason='Version not found for Deliverable')
                    target = row[0]
                rule = values.get('tapper_rule')
                if rule is None:
                    rule = 'permission:operator'
                try:
                    _validate_rule(rule)
                except ValueError as error:
                    return Refused(rule='tapper_rule', reason=str(error))
                plan = {'action': verb, 'idempotency_key': key, 'gate': gates[gate], 'target': target, 'tapper_rule': rule}
            else:
                approval_id = values.get('approval', '')
                row = db.execute('SELECT target, tapper_rule, state FROM approvals WHERE id = ?', (approval_id,)).fetchone()
                if not row:
                    return Refused(rule='approval', reason='Approval not found')
                if row[2] != 'requested':
                    return Refused(rule='state', reason='Approval has already been decided')
                if not People(self.registry, self.tokens)._allows(db, actor, json.loads(row[1])):
                    db.execute('INSERT INTO audit (project_id, action, actor, at, before, after) VALUES (?, ?, ?, ?, NULL, ?)',
                               (approval_id, 'approval_refused', actor, self.registry._clock(), json.dumps({'verb': verb, 'rule': json.loads(row[1])})))
                    return Refused(rule='allowed_tapper', reason='Person is not allowed to decide this Approval')
                reason = values.get('reason')
                if verb == 'deny' and (not isinstance(reason, str) or not reason.strip()):
                    return Refused(rule='reason', reason='A reason is required to deny')
                target = row[0]
                plan = {'approval_id': approval_id, 'action': verb, 'idempotency_key': key, 'reason': reason}
            if not nonce:
                preview = self.tokens.create('approval ' + verb, actor, inputs, plan)
                if verb == 'request' and plan['gate'] == 'client cut':
                    preview_nonce = self.tokens.verify(preview.token, 'approval ' + verb, actor, inputs)
                    db.execute('INSERT INTO jo_state_previews VALUES (?, ?)', (preview_nonce, json.dumps(plan)))
                return preview
            if verb == 'request' and plan['gate'] == 'client cut':
                saved = db.execute('SELECT state FROM jo_state_previews WHERE nonce=?', (nonce,)).fetchone()
                if not saved:
                    return Refused(rule='preview', reason='Preview not found')
                plan = json.loads(saved[0])
                target = plan['target']
            now = self.registry._clock()
            if verb == 'request':
                approval_id = str(T6Id.mint(EntityKind.APPROVAL))
                plan['approval_id'] = approval_id
                db.execute('INSERT INTO approvals VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL)', (approval_id, plan['gate'], target, actor, json.dumps(rule), 'requested'))
            else:
                db.execute('UPDATE approvals SET state = ?, decided_by = ?, decided_at = ?, reason = ? WHERE id = ?', ('granted' if verb == 'grant' else 'denied', actor, now, plan['reason'], approval_id))
                if verb == 'grant':
                    from t6.concern import settle_linked
                    settle_linked(db, approval_id, now)
            cursor = db.execute('INSERT INTO audit (project_id, action, actor, at, before, after) VALUES (?, ?, ?, ?, NULL, ?)', (approval_id, 'approval_' + verb, actor, now, json.dumps(plan)))
            project = db.execute('SELECT id FROM projects WHERE id = ?', (target,)).fetchone()
            if not project:
                project = db.execute('SELECT project_id FROM job_orders WHERE id = ?', (target,)).fetchone()
            version = db.execute('SELECT d.project_id FROM versions v JOIN deliverables d ON d.id=v.deliverable_id WHERE v.id=?', (target,)).fetchone()
            if version:
                project = version
            summary = 'Approval ' + verb + (' for Version ' + target if version else '')
            db.execute('INSERT INTO activity (project_id, subject_id, at, actor, action, summary, audit_id) VALUES (?, ?, ?, ?, ?, ?, ?)', (project[0] if project and project[0] else approval_id, approval_id, now, actor, 'approval_' + verb, summary, cursor.lastrowid))
            if verb != 'request':
                from t6.project_start import decided
                decided(self.registry, db, approval_id, verb, actor, plan['reason'], now, cursor.lastrowid)
            db.execute('INSERT INTO used_preview_tokens VALUES (?)', (nonce,))
            db.execute('INSERT INTO verb_requests VALUES (?, ?, ?)', (key, fingerprint, json.dumps(plan)))
            return Done(result=plan)
