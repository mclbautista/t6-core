"""Project Chat links and their Approval-gated writes."""
from __future__ import annotations

import html
import json
import re
import secrets
import sqlite3
from collections.abc import Callable
from typing import Any

from t6.ids import EntityKind, T6Id
from t6.preview import PreviewTokens
from t6.registry import normalize_code
from t6.registry.sqlite import SqliteProjectRegistry
from t6.verbs import Done, Outcome, Refused

_TARGET_SUFFIX = re.compile(r"(?:^| )(?P<project_id>\S+): chat -?\d+, topic (?:\d+|none)(?: \(.*\))?$", re.DOTALL)


def project_label_from_target(target: str) -> str:
    """Extract `Name [CODE]` (or `Name`, `[CODE]`, or fallback `t6p_...`) from `approvals.target`."""
    match = _TARGET_SUFFIX.search(target)
    if match:
        label = target[:match.start()].strip()
        return label or match.group('project_id')
    return target.partition(':')[0].strip() or target


def format_decision_notice(
    action: str,
    decision: str,
    target_label: str,
    *,
    denier: str | None = None,
    reason: str | None = None,
) -> str:
    """Format an HTML-escaped single-line decision reply for the chat and topic being linked."""
    if action not in ('link-chat', 'unlink-chat'):
        raise ValueError(f'Unknown chat link action: {action!r}')
    if decision not in ('grant', 'deny'):
        raise ValueError(f'Unknown approval decision: {decision!r}')
    if decision == 'grant':
        verb = 'Linked to' if action == 'link-chat' else 'Unlinked from'
        line = f'{verb} {target_label}'
    else:
        verb = 'Link to' if action == 'link-chat' else 'Unlink from'
        who = denier.strip() if isinstance(denier, str) and denier.strip() else 'an operator'
        line = f'{verb} {target_label} was not approved by {who}'
        note = reason.strip() if isinstance(reason, str) else ''
        if note:
            line = f'{line}: {note}'
    return html.escape(line, quote=False)


def decided(
    registry: SqliteProjectRegistry,
    db: sqlite3.Connection,
    approval_id: str,
    verb: str,
    actor: str,
    reason: str | None,
    now: float,
) -> None:
    """Queue one topic reply when a `chat link` Approval is granted or denied."""
    if verb not in ('grant', 'deny'):
        return
    row = db.execute("SELECT target FROM approvals WHERE id = ? AND gate = 'chat link'", (approval_id,)).fetchone()
    if row is None:
        return
    preview = db.execute(
        'SELECT request FROM chat_link_previews WHERE approval_id = ? ORDER BY rowid LIMIT 1',
        (approval_id,),
    ).fetchone()
    if preview is None:
        return
    request = json.loads(preview[0])
    action = request.get('verb')
    values = request.get('values') or {}
    chat, topic = values.get('chat'), values.get('topic')
    if action not in ('link-chat', 'unlink-chat') or type(chat) is not int:
        return
    from t6.worker.internal_queue import InternalJobQueue

    payload = {
        'approval_id': approval_id,
        'action': action,
        'decision': verb,
        'chat': chat,
        'topic': topic,
        'label': project_label_from_target(row[0]),
        'decided_by': actor,
        'reason': reason,
    }
    InternalJobQueue(registry, clock=lambda: now).submit(
        'main',
        'chat_link_notice',
        f'chat_link_notice:{approval_id}',
        payload,
        db=db,
    )

CHAT_LINK_REMINDER_SECONDS = 86400


class ChatLinks:
    def __init__(self, registry: SqliteProjectRegistry, tokens: PreviewTokens,
                 project_name: Callable[[str], str | None] | None = None) -> None:
        """`project_name` reads a Project's name from its Notion page id; without it the Approval names only the code."""
        self.registry, self.tokens, self.project_name = registry, tokens, project_name

    def _label(self, project: str) -> str:
        """`Name [CODE]` for the Approval's target; empty when the Project has neither."""
        with self.registry._transaction() as db:
            row = db.execute('SELECT id, code FROM projects WHERE id = ?', (project,)).fetchone()
            if not row:
                _, form = normalize_code(project)
                row = db.execute("SELECT id, code FROM projects WHERE code_form = ? AND rotation = 'in'", (form,)).fetchone()
            binding = row and db.execute("SELECT page_id FROM bindings WHERE system = 'notion' AND project_id = ? AND active = 1", (row[0],)).fetchone()
        name = None
        if binding and self.project_name:
            try:
                name = (self.project_name(binding[0]) or '').strip()
            except Exception:  # the name is a courtesy; the Approval still carries the ids
                name = None
        code = row[1] if row and row[1] else None
        return ' '.join(part for part in (name, f'[{code}]' if code else None) if part)

    def write(self, verb: str, values: dict[str, Any], *, actor: str,
              idempotency_key: str | None = None, confirm: str | None = None) -> Outcome:
        project, chat, topic = values.get('project'), values.get('chat'), values.get('topic')
        if verb not in ('link-chat', 'unlink-chat') or not isinstance(project, str) or not project.strip() or type(chat) is not int or chat == 0 or (topic is not None and (type(topic) is not int or topic <= 0)):
            return Refused(rule='input', reason='A Project, chat and optional positive topic are required')
        if (confirm and not idempotency_key) or (idempotency_key is not None and not idempotency_key.strip()):
            return Refused(rule='idempotency_key', reason='A nonblank idempotency key is required')
        key = idempotency_key or secrets.token_hex(16)
        label = '' if confirm else self._label(project)
        inputs = {'project': project, 'chat': chat, 'topic': topic, 'idempotency_key': key}
        fingerprint = json.dumps({'verb': verb, 'actor': actor, **inputs}, sort_keys=True, separators=(',', ':'))
        nonce = self.tokens.verify(confirm, 'project ' + verb, actor, inputs) if confirm else None
        if isinstance(nonce, Refused):
            return nonce
        with self.registry._transaction() as db:
            if nonce and db.execute('SELECT 1 FROM used_preview_tokens WHERE nonce = ?', (nonce,)).fetchone():
                return Refused(rule='token_used', reason='Preview token has already been used')
            previous = db.execute('SELECT fingerprint, result FROM verb_requests WHERE idempotency_key = ?', (key,)).fetchone()
            if previous and previous[0] != fingerprint:
                return Refused(rule='idempotency_key', reason='Idempotency key was used for a different request')
            if previous:
                if nonce:
                    db.execute('INSERT INTO used_preview_tokens VALUES (?)', (nonce,))
                    return Done(result=json.loads(previous[1]))
                return self.tokens.create('project ' + verb, actor, inputs, json.loads(previous[1]))
            row = db.execute('SELECT id FROM projects WHERE id = ?', (project,)).fetchone()
            if not row:
                _, form = normalize_code(project)
                row = db.execute("SELECT id FROM projects WHERE code_form = ? AND rotation = 'in'", (form,)).fetchone()
            if not row:
                return Refused(rule='project', reason='Project not found')
            project_id = row[0]
            link = db.execute('SELECT id, state FROM chat_links WHERE project_id = ? AND chat_id = ? AND topic_id IS ?', (project_id, chat, topic)).fetchone()
            desired = 'active' if verb == 'link-chat' else 'inactive'
            plan = {'project_id': project_id, 'action': verb, 'chat': chat, 'topic': topic, 'idempotency_key': key, 'state': desired, 'chat_link_id': link[0] if link else None}
            named = db.execute('SELECT name FROM linked_chat_topics WHERE chat_id = ? AND topic_id = ?', (chat, topic)).fetchone()
            if named:
                plan['topic_name'] = named[0]
            snapshot = json.dumps(link)
            if not nonce:
                pending = db.execute("SELECT approval_id, snapshot, request FROM chat_link_previews WHERE json_extract(request, '$.idempotency_key') = ?", (key,)).fetchone()
                if pending:
                    request = json.loads(pending[2])
                    if request['verb'] != verb or request['values'] != values or request['actor'] != actor:
                        return Refused(rule='idempotency_key', reason='Idempotency key was used for a different request')
                    if pending[1] != snapshot:
                        return Refused(rule='link_changed', reason='Chat link changed since preview; preview again')
                    plan['approval_id'] = pending[0]
                    from t6.verbs import Preview
                    return Preview(plan=plan, token=request['confirm'])
                if (link and link[1] == desired) or (not link and desired == 'inactive'):
                    return Done(result=plan)
                target = (f"{label} " if label else '') + f"{project_id}: chat {chat}, topic {topic if topic is not None else 'none'}" + (f" ({named[0]})" if named else '')
                # One open Approval for one request: a new preview of the same write, by the same caller, for the same link state, reuses it.
                open_one = db.execute("SELECT a.id FROM approvals a JOIN chat_link_previews p ON p.approval_id = a.id WHERE a.gate = 'chat link' AND a.state = 'requested' "
                                      "AND a.requester = ? AND a.target = ? AND p.snapshot = ? AND json_extract(p.request, '$.verb') = ? ORDER BY a.rowid LIMIT 1",
                                      (actor, target, snapshot, verb)).fetchone()
                approval_id = open_one[0] if open_one else str(T6Id.mint(EntityKind.APPROVAL))
                plan['approval_id'] = approval_id
                preview = self.tokens.create('project ' + verb, actor, inputs, plan)
                preview_nonce = self.tokens.verify(preview.token, 'project ' + verb, actor, inputs)
                assert isinstance(preview_nonce, str)
                if open_one:
                    db.execute('INSERT INTO chat_link_previews VALUES (?, ?, ?, ?)', (preview_nonce, approval_id, snapshot, json.dumps({'verb': verb, 'values': values, 'actor': actor, 'idempotency_key': key, 'confirm': preview.token})))
                    return preview
                db.execute('INSERT INTO approvals VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL)', (approval_id, 'chat link', target, actor, json.dumps('permission:operator'), 'requested'))
                now = self.registry._clock()
                from t6.worker.internal_queue import InternalJobQueue
                InternalJobQueue(self.registry, clock=lambda: now).submit('main', 'chat_link_reminder', approval_id, {'approval': approval_id}, run_at=now + CHAT_LINK_REMINDER_SECONDS, db=db)
                cursor = db.execute('INSERT INTO audit (project_id, action, actor, at, before, after) VALUES (?, ?, ?, ?, NULL, ?)', (approval_id, 'approval_request', actor, now, json.dumps({'gate': 'chat link', 'target': target})))
                db.execute('INSERT INTO activity (project_id, subject_id, at, actor, action, summary, audit_id) VALUES (?, ?, ?, ?, ?, ?, ?)', (project_id, approval_id, now, actor, 'approval_request', 'Approval requested for ' + target, cursor.lastrowid))
                db.execute('INSERT INTO chat_link_previews VALUES (?, ?, ?, ?)', (preview_nonce, approval_id, snapshot, json.dumps({'verb': verb, 'values': values, 'actor': actor, 'idempotency_key': key, 'confirm': preview.token})))
                return preview
            preview_row = db.execute('SELECT approval_id, snapshot FROM chat_link_previews WHERE nonce = ?', (nonce,)).fetchone()
            if not preview_row or preview_row[1] != snapshot:
                return Refused(rule='link_changed', reason='Chat link changed since preview; preview again')
            approval = db.execute('SELECT state FROM approvals WHERE id = ?', (preview_row[0],)).fetchone()
            if not approval or approval[0] != 'granted':
                return Refused(rule='approval', reason='The preview Approval must be granted')
            now = self.registry._clock()
            link_id = link[0] if link else str(T6Id.mint(EntityKind.CHAT_LINK))
            if link:
                db.execute('UPDATE chat_links SET state = ? WHERE id = ?', (desired, link_id))
            else:
                db.execute('INSERT INTO chat_links VALUES (?, ?, ?, ?, ?)', (link_id, project_id, chat, topic, desired))
            history = db.execute('INSERT INTO chat_link_history (link_id, state, actor, at) VALUES (?, ?, ?, ?)', (link_id, desired, actor, now))
            from t6.worker.internal_queue import InternalJobQueue
            InternalJobQueue(self.registry, clock=lambda: now).submit('main', 'chat_subscriptions', f'{link_id}:{history.lastrowid}', {}, db=db)
            plan.update(chat_link_id=link_id, approval_id=preview_row[0])
            cursor = db.execute('INSERT INTO audit (project_id, action, actor, at, before, after) VALUES (?, ?, ?, ?, ?, ?)', (project_id, 'project_' + verb.replace('-', '_'), actor, now, json.dumps(link), json.dumps(plan)))
            db.execute('INSERT INTO activity (project_id, subject_id, at, actor, action, summary, audit_id) VALUES (?, ?, ?, ?, ?, ?, ?)', (project_id, link_id, now, actor, 'project_' + verb.replace('-', '_'), f'{verb}: chat {chat}, topic {topic}', cursor.lastrowid))
            db.execute('INSERT INTO used_preview_tokens VALUES (?)', (nonce,))
            db.execute('INSERT INTO verb_requests VALUES (?, ?, ?)', (key, fingerprint, json.dumps(plan)))
            return Done(result=plan)
