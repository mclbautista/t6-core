"""Durable handoff relay requests, with previewed writes and audit history."""
from __future__ import annotations

import json
import secrets
from typing import Any

from t6.ids import EntityKind, T6Id
from t6.preview import PreviewTokens
from t6.verbs import Ambiguous, Done, NeedsPerson, Outcome, Refused


def record(db: Any, registry: Any, request_id: str, state: str, actor: str) -> None:
    now = registry._clock()
    db.execute('INSERT INTO abang_history (request_id, state, actor, at) VALUES (?, ?, ?, ?)', (request_id, state, actor, now))
    cur = db.execute('INSERT INTO audit (project_id, action, actor, at, before, after) VALUES (?, ?, ?, ?, NULL, ?)',
                     (request_id, 'abang_' + state, actor, now, json.dumps({'state': state})))
    db.execute('INSERT INTO activity (project_id, subject_id, at, actor, action, summary, audit_id) VALUES (?, ?, ?, ?, ?, ?, ?)',
               (request_id, request_id, now, actor, 'abang_' + state, 'Abang ' + state, cur.lastrowid))


class Abang:
    def __init__(self, registry: Any, tokens: PreviewTokens) -> None:
        self.registry, self.tokens = registry, tokens

    def write(self, verb: str, values: dict[str, Any], *, actor: str, idempotency_key: str | None, confirm: str | None) -> Outcome:
        if (confirm and not idempotency_key) or (idempotency_key is not None and not idempotency_key.strip()):
            return Refused(rule='idempotency_key', reason='A nonblank idempotency key is required')
        key = idempotency_key or secrets.token_hex(16)
        inputs = {**values, 'idempotency_key': key}
        fingerprint = json.dumps({'verb': 'abang ' + verb, 'actor': actor, **inputs}, sort_keys=True)
        nonce = self.tokens.verify(confirm, 'abang ' + verb, actor, inputs) if confirm else None
        if isinstance(nonce, Refused):
            return nonce
        with self.registry._transaction() as db:
            if nonce and db.execute('SELECT 1 FROM used_preview_tokens WHERE nonce = ?', (nonce,)).fetchone():
                return Refused(rule='token_used', reason='Preview token has already been used')
            previous = db.execute('SELECT fingerprint, result FROM verb_requests WHERE idempotency_key = ?', (key,)).fetchone()
            if previous:
                if previous[0] != fingerprint:
                    return Refused(rule='idempotency_key', reason='Key was used for another request')
                if nonce:
                    db.execute('INSERT INTO used_preview_tokens VALUES (?)', (nonce,))
                    return Done(result=json.loads(previous[1]))
                return self.tokens.create('abang ' + verb, actor, inputs, json.loads(previous[1]))
            if verb == 'request':
                person = db.execute("SELECT telegram_id FROM persons WHERE id = ? AND kind = 'vendor' AND active = 1", (values['person'],)).fetchone()
                if not person or person[0] is None:
                    return NeedsPerson(reason='An active vendor Person with a Telegram ID is required')
                chats = db.execute("SELECT DISTINCT m.chat_id, l.topic_id FROM linked_chat_members m JOIN chat_links l ON l.chat_id = m.chat_id AND l.state = 'active' WHERE m.telegram_id = ? AND m.present = 1", (person[0],)).fetchall()
                if not chats:
                    return NeedsPerson(reason='The vendor must be present in a linked chat')
                if len(chats) != 1:
                    return Ambiguous(candidates=[{'chat': c, 'topic': t} for c, t in chats])
                try:
                    parts = values['destination'].split('/')
                    if len(parts) > 2:
                        raise ValueError()
                    dest = int(parts[0]); topic = int(parts[1]) if len(parts) == 2 else None
                    if not dest or (topic is not None and topic <= 0):
                        raise ValueError()
                except (ValueError, AttributeError):
                    return Refused(rule='destination', reason='Destination must be chat or chat/topic IDs')
                plan = {'person': values['person'], 'source_chat': chats[0][0], 'source_topic': chats[0][1], 'destination_chat': dest, 'destination_topic': topic, 'note': values.get('note'), 'idempotency_key': key}
            else:
                row = db.execute('SELECT state FROM abang_requests WHERE id = ?', (values['request'],)).fetchone()
                if not row or row[0] != 'requested':
                    return Refused(rule='request', reason='Request is not pending')
                plan = {'request_id': values['request'], 'state': 'cancelled', 'idempotency_key': key}
            if not nonce:
                preview = self.tokens.create('abang ' + verb, actor, inputs, plan)
                preview_nonce = self.tokens.verify(preview.token, 'abang ' + verb, actor, inputs)
                db.execute('INSERT INTO abang_previews VALUES (?, ?)', (preview_nonce, json.dumps(plan, sort_keys=True)))
                return preview
            snapshot = db.execute('SELECT plan FROM abang_previews WHERE nonce = ?', (nonce,)).fetchone()
            if not snapshot or snapshot[0] != json.dumps(plan, sort_keys=True):
                return Refused(rule='request_changed', reason='Request changed since preview; preview again')
            if verb == 'request':
                request_id = str(T6Id.mint(EntityKind.ABANG))
                db.execute('INSERT INTO abang_requests (id, person_id, source_chat, source_topic, destination_chat, destination_topic, note, actor, state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)', (request_id, values['person'], chats[0][0], chats[0][1], dest, topic, values.get('note'), actor, 'requested'))
                plan.update(request_id=request_id, state='requested')
            else:
                request_id = values['request']
                db.execute("UPDATE abang_requests SET state = 'cancelled' WHERE id = ?", (request_id,))
            record(db, self.registry, request_id, plan['state'], actor)
            db.execute('INSERT INTO used_preview_tokens VALUES (?)', (nonce,))
            db.execute('INSERT INTO verb_requests VALUES (?, ?, ?)', (key, fingerprint, json.dumps(plan)))
            return Done(result=plan)
