"""Pull linked-chat facts without persisting message content or raw updates."""
from __future__ import annotations

from typing import Any
from html import escape

from t6.abang import record

from t6.registry.sqlite import SqliteProjectRegistry
from t6.worker.gateway import GatewayClient


class LinkedChats:
    def __init__(self, registry: SqliteProjectRegistry, gateway: GatewayClient) -> None:
        self.registry = registry
        self.gateway = gateway
        self._subscriptions: list[dict[str, int | None]] | None = None

    def tick(self) -> None:
        with self.registry._transaction() as db:
            rows = db.execute("SELECT DISTINCT chat_id, topic_id FROM chat_links WHERE state = 'active' ORDER BY chat_id, topic_id").fetchall()
            cursor = db.execute('SELECT seq FROM linked_chat_cursor WHERE id = 1').fetchone()[0]
        subscriptions = [{'chat': chat, 'topic': topic} for chat, topic in rows]
        if subscriptions != self._subscriptions:
            self.gateway.replace_subscriptions(subscriptions)
            self._subscriptions = subscriptions
        self._relay_pending()
        for item in self.gateway.updates(cursor):
            if not self._record(item):
                break
        self._relay_pending()

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
                    text = f'<a href="tg://user?id={telegram}">File request</a>' + (': ' + escape(note) if note else '')
                    ref = self.gateway.notify(chat, text, [], 'abang:request:' + request_id, topic)
                    db.execute('UPDATE abang_requests SET notify_ref = ? WHERE id = ?', (ref, request_id))
            if state == 'requested' and ref is not None and message_id is None:
                status, message_id = self.gateway.state(ref)
                if status == 'sent' and message_id is not None:
                    with self.registry._transaction() as db:
                        db.execute('UPDATE abang_requests SET message_id = ? WHERE id = ?', (message_id, request_id))
            if state == 'forwarding':
                if forward_ref is None:
                    forward_ref = self.gateway.forward(chat, reply_id, dest, dest_topic, 'abang:forward:' + request_id)
                    with self.registry._transaction() as db:
                        db.execute('UPDATE abang_requests SET forward_ref = ? WHERE id = ?', (forward_ref, request_id))
                status, _ = self.gateway.state(forward_ref)
                if status == 'sent':
                    with self.registry._transaction() as db:
                        db.execute("UPDATE abang_requests SET state = 'delivered' WHERE id = ?", (request_id,))
                        record(db, self.registry, request_id, 'delivered', 'worker')
                    state = 'delivered'
            if state == 'delivered' and ref is not None and edit_ref is None:
                edit_ref = self.gateway.edit(ref, 'Delivered', [], 'abang:delivered:' + request_id)
                with self.registry._transaction() as db:
                    db.execute('UPDATE abang_requests SET edit_ref = ? WHERE id = ?', (edit_ref, request_id))

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
            linked = db.execute("SELECT 1 FROM chat_links WHERE state = 'active' AND chat_id = ? AND (topic_id IS NULL OR topic_id = ?)", (chat, topic)).fetchone()
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
            db.execute('UPDATE linked_chat_cursor SET seq = ? WHERE id = 1', (seq,))
            return True
