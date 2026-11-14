import json
import sqlite3
import tempfile
import unittest
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path
from typing import Any
from unittest.mock import patch

from t6.registry import Registered
from t6.registry.sqlite import SqliteProjectRegistry
from t6.worker.gateway import GatewayClient, GatewayRejected
from t6.worker.linked_chats import LinkedChats


class FakeGateway:
    def __init__(self) -> None:
        self.subscriptions: list[dict[str, int | None]] = []
        self.items: list[dict[str, Any]] = []
        self.calls: list[tuple[str, str, bytes]] = []
        self.refuse_put = False

    def send(self, method: str, path: str, headers: Mapping[str, str], body: bytes) -> tuple[int, dict[str, Any]]:
        self.calls.append((method, path, body))
        if method == 'PUT':
            if self.refuse_put:
                return 503, {}
            self.subscriptions = json.loads(body)
            return 200, {}
        after = int(path.split('=')[1])
        return 200, {'items': [item for item in self.items if item['seq'] > after]}


class LinkedChatTests(unittest.TestCase):
    def test_subscriptions_and_private_id_only_facts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'db'
            registry = SqliteProjectRegistry(path)
            project = registry.register(actor='test', idempotency_key='project')
            assert isinstance(project, Registered)
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("INSERT INTO persons VALUES ('person', 'staff', '[]', '[]', 42, 0, 1)")
                db.execute("INSERT INTO chat_links VALUES ('link', ?, -100, 7, 'active')", (project.project.id,))
            fake = FakeGateway()
            secret = 'private message never store'
            fake.items = [
                {'seq': 1, 'kind': 'update', 'chat': -100, 'topic': 7, 'message_id': 1, 'from': {'id': 42}, 'has_file': True, 'has_link': False, 'text': secret, 'raw': {'text': secret}},
                {'seq': 2, 'kind': 'update', 'chat': -100, 'topic': 7, 'message_id': 2, 'from': {'id': 99}, 'has_file': False, 'has_link': True, 'text': secret},
                {'seq': 3, 'kind': 'event', 'chat': -100, 'event': 'member_joined', 'user': {'id': 42, 'first_name': secret}},
                {'seq': 4, 'kind': 'event', 'chat': -100, 'event': 'member_left', 'user': {'id': 99}},
            ]
            worker = LinkedChats(registry, GatewayClient(fake, 'secret'))
            with patch('t6.server.llm.LLMService.run') as llm, patch('t6.llm.task.run') as task_run:
                worker.tick()
                llm.assert_not_called()
                task_run.assert_not_called()
            self.assertEqual(fake.subscriptions, [{'chat': -100, 'topic': 7}])
            with closing(sqlite3.connect(path)) as db, db:
                self.assertEqual(db.execute('SELECT message_id, sender_id, person_id, has_file, has_link FROM linked_chat_messages ORDER BY message_id').fetchall(), [(1, 42, 'person', 1, 0), (2, 99, None, 0, 1)])
                self.assertEqual(db.execute('SELECT telegram_id, person_id, present FROM linked_chat_members ORDER BY telegram_id').fetchall(), [(42, 'person', 1), (99, None, 0)])
                self.assertNotIn(secret, '\n'.join(db.iterdump()))
                db.execute("UPDATE chat_links SET state = 'inactive'")
            worker.tick()
            self.assertEqual(fake.subscriptions, [])
            self.assertEqual(fake.calls[-1][1], '/updates?after=4')

    def test_a_refused_subscription_change_is_sent_again_on_the_next_tick(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'db'
            registry = SqliteProjectRegistry(path)
            project = registry.register(actor='test', idempotency_key='project')
            assert isinstance(project, Registered)
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("INSERT INTO chat_links VALUES ('link', ?, -100, NULL, 'active')", (project.project.id,))
            fake = FakeGateway()
            fake.refuse_put = True
            worker = LinkedChats(registry, GatewayClient(fake, 'secret'))
            with self.assertRaises(GatewayRejected):
                worker.tick()
            fake.refuse_put = False
            worker.tick()
            self.assertEqual(fake.subscriptions, [{'chat': -100, 'topic': None}])
