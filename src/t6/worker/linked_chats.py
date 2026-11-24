"""Pull linked-chat facts without persisting message content or raw updates."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any
import json
import secrets
from html import escape

from t6.abang import record
from t6.chat_link import format_decision_notice
from t6.registry.sqlite import SqliteProjectRegistry
from t6.worker.gateway import GatewayClient
from t6.worker.client import WorkerJob


class LinkedChats:
    def __init__(
        self,
        registry: SqliteProjectRegistry,
        gateway: GatewayClient,
        operators: tuple[int, ...] = (),
        person_name: Callable[[str], str | None] | None = None,
    ) -> None:
        self.registry = registry
        self.gateway = gateway
        self.operators = operators
        self.person_name = person_name
        self._subscriptions: list[dict[str, int | None]] | None = None

    def tick(self) -> None:
        self._check_sends()
        self._notices()
        self.replace_subscriptions()
        with self.registry._transaction() as db:
            cursor = db.execute('SELECT seq FROM linked_chat_cursor WHERE id = 1').fetchone()[0]
        self._relay_pending()
        for item in self.gateway.updates(cursor):
            if not self._record(item):
                break
        self._relay_pending()
        self._notices()

    def replace_subscriptions(self, job: WorkerJob | None = None) -> bool:
        """Tell the gateway which chats and topics to follow; a failure raises so a queued job retries and the tick tries again."""
        with self.registry._transaction() as db:
            rows = db.execute("SELECT DISTINCT chat_id, topic_id FROM chat_links WHERE state = 'active' ORDER BY chat_id, topic_id").fetchall()
        subscriptions = [{'chat': chat, 'topic': topic} for chat, topic in rows]
        if subscriptions != self._subscriptions:
            self.gateway.replace_subscriptions(subscriptions)
            self._subscriptions = subscriptions
        return True

    def _relay_pending(self) -> None:
        with self.registry._transaction() as db:
            rows = db.execute("SELECT id, source_chat, source_topic, person_id, note, notify_ref, message_id, reply_id, forward_ref, destination_chat, destination_topic, state, edit_ref FROM abang_requests WHERE state IN ('requested', 'forwarding', 'delivered')").fetchall()
        for request_id, chat, topic, person, note, ref, message_id, reply_id, forward_ref, dest, dest_topic, state, edit_ref in rows:
            if state == 'requested' and ref is None:
                with self.registry._transaction() as db:
                    current = db.execute('SELECT state FROM abang_requests WHERE id = ?', (request_id,)).fetchone()
                    if current[0] != 'requested':
                        continue
                    telegram = db.execute('SELECT telegram_id FROM persons WHERE id = ?', (person,)).fetchone()[0]
                    text = f'<a href="tg://user?id={telegram}">File request</a>: please reply to this message with the file or a link.' + (' ' + escape(note) if note else '')
                    ref = self.gateway.notify(chat, text, [], 'abang:request:' + request_id, topic)
                    self._track(db, ref, chat, topic)
                    db.execute('UPDATE abang_requests SET notify_ref = ? WHERE id = ?', (ref, request_id))
            if state == 'requested' and ref is not None and message_id is None:
                status, message_id = self.gateway.state(ref)
                if status == 'sent' and message_id is not None:
                    with self.registry._transaction() as db:
                        db.execute('UPDATE abang_requests SET message_id = ? WHERE id = ?', (message_id, request_id))
                elif status == 'failed':
                    self._fail_request(request_id, 'the request message could not be sent')
                    continue
            if state == 'forwarding':
                if forward_ref is None:
                    forward_ref = self.gateway.forward(chat, reply_id, dest, dest_topic, 'abang:forward:' + request_id)
                    with self.registry._transaction() as db:
                        db.execute('UPDATE abang_requests SET forward_ref = ? WHERE id = ?', (forward_ref, request_id))
                status, _ = self.gateway.state(forward_ref)
                if status == 'failed':
                    self._fail_request(request_id, 'the file could not be forwarded')
                    continue
                if status == 'sent':
                    with self.registry._transaction() as db:
                        db.execute("UPDATE abang_requests SET state = 'delivered' WHERE id = ?", (request_id,))
                        record(db, self.registry, request_id, 'delivered', 'worker')
                    state = 'delivered'
            if state == 'delivered' and ref is not None and edit_ref is None:
                edit_ref = self.gateway.edit(ref, 'Delivered', [], 'abang:delivered:' + request_id)
                with self.registry._transaction() as db:
                    db.execute('UPDATE abang_requests SET edit_ref = ? WHERE id = ?', (edit_ref, request_id))
        with self.registry._transaction() as db:
            drafts = db.execute("SELECT id, source_chat, source_topic FROM abang_drafts WHERE state = 'open' AND notify_ref IS NULL").fetchall()
        for draft, chat, topic in drafts:
            with self.registry._transaction() as db:
                options = db.execute('SELECT n, label FROM abang_draft_options WHERE draft_id = ? ORDER BY n', (draft,)).fetchall()
                buttons = [[{'text': label[:60], 'payload': f'abang-dest:{draft}:{n}'}] for n, label in options]
                ref = self.gateway.notify(chat, 'Where should the file go?', buttons, 'abang:draft:' + draft, topic)
                self._track(db, ref, chat, topic)
                db.execute('UPDATE abang_drafts SET notify_ref = ? WHERE id = ?', (ref, draft))
        with self.registry._transaction() as db:
            candidates = db.execute("SELECT c.id, c.request_id, c.message_id, r.source_chat, r.source_topic FROM abang_candidates c JOIN abang_requests r ON r.id = c.request_id WHERE c.state = 'pending' AND c.notify_ref IS NULL AND r.state = 'requested'").fetchall()
        for candidate, request_id, message, chat, topic in candidates:
            buttons = [[{'text': 'Yes', 'payload': f'abang:{candidate}:yes'}, {'text': 'No', 'payload': f'abang:{candidate}:no'}]]
            with self.registry._transaction() as db:
                current = db.execute("SELECT 1 FROM abang_candidates c JOIN abang_requests r ON r.id = c.request_id WHERE c.id = ? AND c.state = 'pending' AND r.state = 'requested'", (candidate,)).fetchone()
                if current:
                    link = f'https://t.me/c/{str(chat)[4:]}/{message}' if str(chat).startswith('-100') else None
                    text = f'Is this it? <a href="{link}">View message</a>' if link else f'Is this it? Message {message}'
                    ref = self.gateway.notify(chat, text, buttons, f'abang:candidate:{request_id}:{message}', topic)
                    self._track(db, ref, chat, topic)
                    db.execute('UPDATE abang_candidates SET notify_ref = ? WHERE id = ?', (ref, candidate))
        self._notices()

    def _fail_request(self, request_id: str, reason: str) -> None:
        """End a request the gateway could not send or forward, so nothing waits on it and the operators hear of it."""
        with self.registry._transaction() as db:
            if db.execute("UPDATE abang_requests SET state = 'failed' WHERE id = ? AND state IN ('requested', 'forwarding')", (request_id,)).rowcount:
                record(db, self.registry, request_id, 'failed', 'worker')
                db.execute('INSERT OR IGNORE INTO linked_chat_notices VALUES (?, ?)', (f'abang-failed:{request_id}', f'Abang request {request_id} failed: {reason}.'))

    def _record(self, item: dict[str, Any]) -> bool:
        seq = item['seq']
        chat = item.get('chat')
        topic = item.get('topic')
        with self.registry._transaction() as db:
            cursor = db.execute('SELECT seq FROM linked_chat_cursor WHERE id = 1').fetchone()[0]
            if seq <= cursor:
                return True
            # A reply can arrive while the gateway has not exposed the sent message ID yet.
            # Keep the cursor behind it so a later poll can match it.
            if item.get('kind') == 'update' and item.get('reply_to_message_id') is not None and db.execute("SELECT 1 FROM abang_requests WHERE state = 'requested' AND source_chat = ? AND notify_ref IS NOT NULL AND message_id IS NULL", (chat,)).fetchone():
                return False
            if item.get('kind') == 'event' and item.get('event') == 'chat_migrated':
                old, new = item.get('old_chat'), item.get('new_chat')
                if type(old) is int and type(new) is int and old != new:
                    rows = db.execute("SELECT id FROM chat_links WHERE chat_id = ?", (old,)).fetchall()
                    if rows:
                        db.execute('UPDATE chat_links SET chat_id = ? WHERE chat_id = ?', (new, old))
                        db.execute('UPDATE OR REPLACE linked_chat_members SET chat_id = ? WHERE chat_id = ?', (new, old))
                        db.execute('UPDATE OR REPLACE linked_chat_topics SET chat_id = ? WHERE chat_id = ?', (new, old))
                        db.execute('INSERT OR IGNORE INTO linked_chat_notices VALUES (?, ?)', (f'migration:{old}:{new}', f'Chat moved from {old} to {new}.'))
            linked = db.execute("SELECT 1 FROM chat_links WHERE state = 'active' AND chat_id = ? AND (topic_id IS NULL OR topic_id = ?)", (chat, topic)).fetchone()
            chat_linked = db.execute("SELECT 1 FROM chat_links WHERE state = 'active' AND chat_id = ?", (chat,)).fetchone()
            name = item.get('name') if item.get('kind') == 'event' and item.get('event') in ('topic_created', 'topic_renamed') else item.get('topic_name') if item.get('kind') == 'update' else None
            if chat_linked and type(topic) is int and isinstance(name, str) and name.strip():
                db.execute('INSERT INTO linked_chat_topics VALUES (?, ?, ?) ON CONFLICT(chat_id, topic_id) DO UPDATE SET name=excluded.name', (chat, topic, name))
            if item.get('kind') == 'event':
                linked = db.execute("SELECT 1 FROM chat_links WHERE state = 'active' AND chat_id = ?", (chat,)).fetchone()
                user = item.get('user') or {}
                user_id = user.get('id')
                if linked and item.get('event') in ('member_joined', 'member_left') and type(user_id) is int:
                    person = db.execute('SELECT id FROM persons WHERE telegram_id = ? AND active = 1', (user_id,)).fetchone()
                    db.execute('INSERT INTO linked_chat_members VALUES (?, ?, ?, ?) ON CONFLICT(chat_id, telegram_id) DO UPDATE SET person_id=excluded.person_id, present=excluded.present', (chat, user_id, person[0] if person else None, int(item['event'] == 'member_joined')))
            elif linked and item.get('kind') == 'update' and type(item.get('message_id')) is int:
                sender = item.get('from') or {}
                sender_id = sender.get('id')
                sender_id = sender_id if type(sender_id) is int else None
                person = db.execute('SELECT id FROM persons WHERE telegram_id = ? AND active = 1', (sender_id,)).fetchone()
                db.execute('INSERT INTO linked_chat_messages VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(chat_id, message_id) DO UPDATE SET topic_id=excluded.topic_id, sender_id=excluded.sender_id, person_id=excluded.person_id, has_file=excluded.has_file, has_link=excluded.has_link', (chat, topic, item['message_id'], sender_id, person[0] if person else None, int(item.get('has_file') is True), int(item.get('has_link') is True)))
            if linked and item.get('kind') == 'update' and type(item.get('message_id')) is int and (item.get('has_file') is True or item.get('has_link') is True):
                reply = item.get('reply_to_message_id')
                sender = item.get('from') or {}
                row = db.execute("SELECT r.id FROM abang_requests r JOIN persons p ON p.id = r.person_id WHERE r.state = 'requested' AND r.source_chat = ? AND r.message_id = ? AND p.telegram_id = ?", (chat, reply, sender.get('id'))).fetchone()
                if row:
                    db.execute("UPDATE abang_requests SET state = 'forwarding', reply_id = ? WHERE id = ?", (item['message_id'], row[0]))
                    record(db, self.registry, row[0], 'forwarding', 'worker')
                else:
                    # Any file or link from the freelancer that is not a reply to the request is a candidate.
                    rows = db.execute("SELECT r.id FROM abang_requests r JOIN persons p ON p.id = r.person_id WHERE r.state = 'requested' AND r.source_chat = ? AND (r.source_topic IS NULL OR r.source_topic = ?) AND p.telegram_id = ?", (chat, topic, sender.get('id'))).fetchall()
                    for (request_id,) in rows:
                        db.execute("INSERT OR IGNORE INTO abang_candidates (id, request_id, message_id, state) VALUES (?, ?, ?, 'pending')", (secrets.token_hex(8), request_id, item['message_id']))
            db.execute('UPDATE linked_chat_cursor SET seq = ? WHERE id = 1', (seq,))
            return True

    def nudge(self, job: WorkerJob) -> bool:
        """After 3 days with nothing delivered, remind the coordinator who asked. A wait never silently expires."""
        request = job.payload.get('request')
        if not isinstance(request, str):
            raise ValueError('Invalid abang nudge payload')
        with self.registry._transaction() as db:
            row = db.execute("SELECT r.note, c.telegram_id FROM abang_requests r JOIN persons c ON c.id = r.actor WHERE r.id = ? AND r.state = 'requested'", (request,)).fetchone()
            if row is None or row[1] is None:
                return False
            note, coordinator = row
            text = f'Abang request {request} has had no file for 3 days.' + (f' Note: {escape(note)}' if note else '')
            self.gateway.notify(coordinator, text, [], 'abang:nudge:' + request)
            return True

    def _person_label(self, db: Any, person_id: str) -> str:
        row = db.execute(
            "SELECT page_id FROM bindings WHERE system = 'notion' AND project_id = ? AND active = 1",
            (person_id,),
        ).fetchone()
        if row and self.person_name:
            try:
                name = (self.person_name(row[0]) or '').strip()
            except Exception:
                name = ''
            if name:
                return name
        return person_id

    def chat_link_notice(self, job: WorkerJob) -> bool:
        """Post one decision line in the chat and topic being linked when a `chat link` Approval is decided."""
        payload = job.payload
        approval_id = payload.get('approval_id')
        action = payload.get('action')
        decision = payload.get('decision')
        chat = payload.get('chat')
        topic = payload.get('topic')
        label = payload.get('label')
        decided_by = payload.get('decided_by')
        reason = payload.get('reason')
        if (
            not isinstance(approval_id, str) or not approval_id.strip()
            or not isinstance(action, str) or action not in ('link-chat', 'unlink-chat')
            or not isinstance(decision, str) or decision not in ('grant', 'deny')
            or type(chat) is not int or chat == 0
            or (topic is not None and (type(topic) is not int or topic <= 0))
            or not isinstance(label, str) or not label.strip()
            or (decision == 'deny' and (not isinstance(decided_by, str) or not decided_by.strip()))
            or (reason is not None and not isinstance(reason, str))
        ):
            raise ValueError('Invalid chat link notice payload')
        denier: str | None = None
        if decision == 'deny':
            assert isinstance(decided_by, str)
            with self.registry._transaction() as db:
                denier = self._person_label(db, decided_by)
        text = format_decision_notice(action, decision, label, denier=denier, reason=reason)
        ref = self.gateway.notify(chat, text, [], f'chat-link-decision:{approval_id}', topic)
        if decision == 'grant' and action == 'link-chat' and topic is not None:
            with self.registry._transaction() as db:
                self._track(db, ref, chat, topic)
        return True

    def _requester_label(self, db: Any, requester: str) -> str:
        binding = db.execute("SELECT page_id FROM bindings WHERE system = 'notion' AND project_id = ? AND active = 1", (requester,)).fetchone()
        name = None
        if binding and self.person_name:
            try:
                name = (self.person_name(binding[0]) or '').strip()
            except Exception:  # the name is a courtesy; the reminder still names the Person id
                name = None
        return f'{name} ({requester})' if name else requester

    def remind_chat_link(self, job: WorkerJob) -> bool:
        """After 24 hours with no tap on a chat link Approval, remind the operator chat once. The Approval stays requested."""
        approval_id = job.payload.get('approval')
        if not isinstance(approval_id, str):
            raise ValueError('Invalid chat link reminder payload')
        with self.registry._transaction() as db:
            row = db.execute("SELECT target, requester FROM approvals WHERE id = ? AND gate = 'chat link' AND state = 'requested'", (approval_id,)).fetchone()
            if row is None:
                return False
            if not self.operators:
                raise RuntimeError('Chat link reminder needs T6_OPERATOR_IDS')
            target, requester = row
            who = self._requester_label(db, requester)
        chat = self.operators[0]
        text = (
            '<b>Reminder: chat link approval waiting 24h</b>\n'
            f'{escape(target)}\n'
            f'Requested by: {escape(who)}\n'
            f'Approval: {escape(approval_id)}'
        )
        self.gateway.notify(chat, text, [], f'chat-link:reminder:{approval_id}:{chat}')
        return True

    def track_send(self, ref: int, chat: int, topic: int | None) -> None:
        with self.registry._transaction() as db:
            self._track(db, ref, chat, topic)

    @staticmethod
    def _track(db: Any, ref: int, chat: int, topic: int | None) -> None:
        """Remember a send to a linked topic, so a dead topic found by it deactivates the link."""
        if topic is None:
            return
        links = db.execute("SELECT id FROM chat_links WHERE chat_id = ? AND topic_id = ? AND state = 'active'", (chat, topic)).fetchall()
        if links:
            db.execute('INSERT OR IGNORE INTO linked_chat_sends VALUES (?, ?, ?, ?)', (ref, chat, topic, json.dumps([row[0] for row in links])))

    def _check_sends(self) -> None:
        with self.registry._transaction() as db:
            sends = db.execute('SELECT ref, chat_id, topic_id, links FROM linked_chat_sends').fetchall()
        for ref, chat, topic, links in sends:
            result = self.gateway.status(ref)
            if result['state'] == 'queued':
                continue
            with self.registry._transaction() as db:
                if result['state'] == 'failed' and result.get('error') == 'thread_not_found':
                    for link in json.loads(links):
                        changed = db.execute("UPDATE chat_links SET state = 'inactive' WHERE id = ? AND chat_id = ? AND topic_id = ? AND state = 'active'", (link, chat, topic)).rowcount
                        if changed:
                            db.execute('INSERT INTO chat_link_history (link_id, state, actor, at) VALUES (?, ?, ?, ?)', (link, 'inactive', 'gateway', self.registry._clock()))
                            db.execute('INSERT OR IGNORE INTO linked_chat_notices VALUES (?, ?)', (f'dead-topic:{chat}:{topic}:{ref}', f'Chat {chat}, topic {topic} was not found; its link is inactive.'))
                db.execute('DELETE FROM linked_chat_sends WHERE ref = ?', (ref,))

    def _notices(self) -> None:
        with self.registry._transaction() as db:
            notices = db.execute('SELECT key, text FROM linked_chat_notices').fetchall()
        for key, message in notices:
            for operator in self.operators:
                self.gateway.notify(operator, message, [], f'linked-chat:{key}:{operator}')
            if self.operators:
                with self.registry._transaction() as db:
                    db.execute('DELETE FROM linked_chat_notices WHERE key = ?', (key,))
