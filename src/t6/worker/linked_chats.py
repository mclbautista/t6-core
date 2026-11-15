"""Pull linked-chat facts without persisting message content or raw updates."""
from __future__ import annotations

from typing import Any
import json

from t6.registry.sqlite import SqliteProjectRegistry
from t6.worker.gateway import GatewayClient


class LinkedChats:
    def __init__(self, registry: SqliteProjectRegistry, gateway: GatewayClient, operators: tuple[int, ...] = ()) -> None:
        self.registry = registry
        self.gateway = gateway
        self.operators = operators
        self._subscriptions: list[dict[str, int | None]] | None = None

    def tick(self) -> None:
        self._check_sends()
        self._notices()
        with self.registry._transaction() as db:
            rows = db.execute("SELECT DISTINCT chat_id, topic_id FROM chat_links WHERE state = 'active' ORDER BY chat_id, topic_id").fetchall()
            cursor = db.execute('SELECT seq FROM linked_chat_cursor WHERE id = 1').fetchone()[0]
        subscriptions = [{'chat': chat, 'topic': topic} for chat, topic in rows]
        if subscriptions != self._subscriptions:
            self.gateway.replace_subscriptions(subscriptions)
            self._subscriptions = subscriptions
        for item in self.gateway.updates(cursor):
            self._record(item)
        self._notices()

    def _record(self, item: dict[str, Any]) -> None:
        seq = item['seq']
        chat = item.get('chat')
        topic = item.get('topic')
        with self.registry._transaction() as db:
            cursor = db.execute('SELECT seq FROM linked_chat_cursor WHERE id = 1').fetchone()[0]
            if seq <= cursor:
                return
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
            db.execute('UPDATE linked_chat_cursor SET seq = ? WHERE id = 1', (seq,))

    def track_send(self, ref: int, chat: int, topic: int | None) -> None:
        if topic is None:
            return
        with self.registry._transaction() as db:
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
