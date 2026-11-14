"""Pull linked-chat facts without persisting message content or raw updates."""
from __future__ import annotations

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
        for item in self.gateway.updates(cursor):
            self._record(item)

    def _record(self, item: dict) -> None:
        seq = item['seq']
        chat = item.get('chat')
        topic = item.get('topic')
        with self.registry._transaction() as db:
            cursor = db.execute('SELECT seq FROM linked_chat_cursor WHERE id = 1').fetchone()[0]
            if seq <= cursor:
                return
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
            db.execute('UPDATE linked_chat_cursor SET seq = ? WHERE id = 1', (seq,))
