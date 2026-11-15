"""Durable handoff relay requests, with previewed writes and audit history."""
from __future__ import annotations

import hashlib
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
                source = values.get('source')
                if source is not None:
                    try:
                        source_parts = str(source).split('/')
                        if len(source_parts) > 2:
                            raise ValueError()
                        source_chat = int(source_parts[0])
                        source_topic = int(source_parts[1]) if len(source_parts) == 2 else None
                    except ValueError:
                        return Refused(rule='source', reason='Source must be chat or chat/topic IDs')
                    chats = db.execute("SELECT DISTINCT m.chat_id, ? FROM linked_chat_members m JOIN chat_links l ON l.chat_id = m.chat_id AND l.state = 'active' WHERE m.telegram_id = ? AND m.present = 1 AND m.chat_id = ?", (source_topic, person[0], source_chat)).fetchall()
                else:
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
            elif verb == 'accept':
                row = db.execute("SELECT c.request_id, c.message_id, c.state, r.state, r.person_id FROM abang_candidates c JOIN abang_requests r ON r.id = c.request_id WHERE c.id = ?", (values['candidate'],)).fetchone()
                if not row or row[2] != 'pending' or row[3] != 'requested':
                    return Refused(rule='candidate', reason='Candidate is no longer pending')
                if actor != row[4]:
                    return Refused(rule='person', reason='Only the freelancer may decide')
                if values['decision'] not in ('yes', 'no'):
                    return Refused(rule='decision', reason='Choose yes or no')
                plan = {'request_id': row[0], 'candidate': values['candidate'], 'message_id': row[1], 'decision': values['decision'], 'idempotency_key': key}
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
                now = self.registry._clock()
                db.execute("INSERT INTO internal_jobs (queue, kind, dedupe_key, payload, run_at, state, created_at) VALUES ('main', 'abang_nudge', ?, ?, ?, 'queued', ?)", (request_id, json.dumps({'request': request_id}), now + 3 * 86400, now))
            elif verb == 'accept':
                request_id = row[0]
                decision = values['decision']
                db.execute('UPDATE abang_candidates SET state = ? WHERE id = ?', ('accepted' if decision == 'yes' else 'rejected', values['candidate']))
                if decision == 'yes':
                    db.execute("UPDATE abang_requests SET state = 'forwarding', reply_id = ? WHERE id = ?", (row[1], request_id))
                    plan['state'] = 'forwarding'
                else:
                    plan['state'] = 'candidate_rejected'
            else:
                request_id = values['request']
                db.execute("UPDATE abang_requests SET state = 'cancelled' WHERE id = ?", (request_id,))
            record(db, self.registry, request_id, plan['state'], actor)
            db.execute('INSERT INTO used_preview_tokens VALUES (?)', (nonce,))
            db.execute('INSERT INTO verb_requests VALUES (?, ?, ?)', (key, fingerprint, json.dumps(plan)))
            return Done(result=plan)


def start_draft(registry: Any, actor: str, person: str, chat: int, topic: int | None, note: str | None, key: str) -> Outcome:
    """Open the destination choice for `/abang` typed in the source chat, where the freelancer is.

    One option per linked chat or topic of that chat's Projects. A worker shows them as buttons; only
    the coordinator who typed the command may tap one, and the tap confirms the request.
    """
    draft = hashlib.sha256(key.encode()).hexdigest()[:8]
    with registry._transaction() as db:
        if db.execute('SELECT 1 FROM abang_drafts WHERE id = ?', (draft,)).fetchone():
            return Done(result={'draft': draft})
        row = db.execute("SELECT telegram_id FROM persons WHERE id = ? AND kind = 'vendor' AND active = 1", (person,)).fetchone()
        if not row or row[0] is None:
            return NeedsPerson(reason='An active vendor Person with a Telegram ID is required')
        if not db.execute('SELECT 1 FROM linked_chat_members WHERE chat_id = ? AND telegram_id = ? AND present = 1', (chat, row[0])).fetchone():
            return NeedsPerson(reason='The vendor must be present in this chat')
        projects = [r[0] for r in db.execute("SELECT DISTINCT project_id FROM chat_links WHERE state = 'active' AND chat_id = ? AND (topic_id IS ? OR topic_id IS NULL)", (chat, topic))]
        if not projects:
            return Refused(rule='chat', reason='This chat or topic is not linked to a Project')
        marks = ','.join('?' * len(projects))
        options = [(c, t, codes) for c, t, codes in db.execute(f"SELECT l.chat_id, l.topic_id, GROUP_CONCAT(DISTINCT p.code) FROM chat_links l LEFT JOIN projects p ON p.id = l.project_id WHERE l.state = 'active' AND l.project_id IN ({marks}) GROUP BY l.chat_id, l.topic_id ORDER BY MIN(l.rowid)", projects) if (c, t) != (chat, topic)]
        if not options:
            return Refused(rule='destination', reason='No other linked chat or topic to send the file to')
        db.execute("INSERT INTO abang_drafts (id, actor, person_id, source_chat, source_topic, note, state) VALUES (?, ?, ?, ?, ?, ?, 'open')", (draft, actor, person, chat, topic, note))
        for n, (c, t, codes) in enumerate(options):
            named = db.execute('SELECT name FROM linked_chat_topics WHERE chat_id = ? AND topic_id = ?', (c, t)).fetchone() if t is not None else None
            parts = [part for part in (codes.replace(',', '/') if codes else None, named[0] if named else None) if part]
            label = ' · '.join(parts) or f'Chat {c}' + (f', topic {t}' if t is not None else '')
            db.execute('INSERT INTO abang_draft_options VALUES (?, ?, ?, ?, ?)', (draft, n, c, t, label))
    return Done(result={'draft': draft})
