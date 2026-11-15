import json
import sqlite3
import unittest
from contextlib import contextmanager

from t6.abang import Abang
from t6.preview import PreviewTokens
from t6.registry.sqlite import SqliteProjectRegistry, _migrate
from t6.verbs import Done, NeedsPerson, Preview
from t6.worker.gateway import GatewayClient
from t6.worker.linked_chats import LinkedChats


class MemoryRegistry(SqliteProjectRegistry):
    def __init__(self):
        self.db = sqlite3.connect(':memory:')
        self._clock = lambda: 123.0
        _migrate(self.db)
        self.db.execute("INSERT INTO persons VALUES ('vendor', 'vendor', '[]', '[]', 42, 0, 1)")
        self.db.execute("INSERT INTO chat_links VALUES ('link', 'project', -100, NULL, 'active')")
        self.db.execute("INSERT INTO linked_chat_members VALUES (-100, 42, 'vendor', 1)")
        self.db.commit()

    @contextmanager
    def _transaction(self):
        try:
            yield self.db
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise


class Gateway:
    def __init__(self):
        self.keys = {}
        self.items = []
        self.calls = []

    def send(self, method, path, headers, body):
        data = json.loads(body) if body else {}
        self.calls.append((method, path, data))
        if method == 'PUT':
            return 200, {}
        if path.startswith('/updates'):
            after = int(path.split('=')[1])
            return 200, {'items': [i for i in self.items if i['seq'] > after]}
        if method == 'GET':
            return 200, {'state': 'sent', 'telegram_message_id': 101}
        key = data['idempotency_key']
        self.keys.setdefault(key, len(self.keys) + 1)
        return 202, {'message_ref': self.keys[key]}


class AbangTests(unittest.TestCase):
    def setUp(self):
        self.db = MemoryRegistry()
        self.store = Abang(self.db, PreviewTokens('secret'))
        self.gateway = Gateway()
        self.worker = LinkedChats(self.db, GatewayClient(self.gateway, 'secret'))

    def write(self, verb, values, key):
        preview = self.store.write(verb, values, actor='coordinator', idempotency_key=key, confirm=None)
        self.assertIsInstance(preview, Preview)
        result = self.store.write(verb, values, actor='coordinator', idempotency_key=key, confirm=preview.token)
        self.assertIsInstance(result, Done)
        return result.result['request_id']

    def test_relay_once_and_ignore_plain_reply(self):
        rid = self.write('request', {'person': 'vendor', 'destination': '-200/7', 'note': 'Please'}, 'one')
        self.worker.tick()
        self.gateway.items = [{'seq': 1, 'kind': 'update', 'chat': -100, 'message_id': 102, 'reply_to_message_id': 101, 'from': {'id': 42}, 'has_file': False, 'has_link': False}]
        self.worker.tick()
        self.assertEqual(self.db.db.execute('SELECT state FROM abang_requests').fetchone()[0], 'requested')
        self.gateway.items.append({'seq': 2, 'kind': 'update', 'chat': -100, 'message_id': 103, 'reply_to_message_id': 101, 'from': {'id': 42}, 'has_file': True, 'has_link': False})
        self.worker.tick()
        self.worker.tick()
        forwards = [call for call in self.gateway.calls if call[1] == '/forward']
        self.assertEqual(len(forwards), 1)
        self.assertEqual(forwards[0][2], {'from_chat': -100, 'message_id': 103, 'to': -200, 'topic': 7, 'idempotency_key': 'abang:forward:' + rid})
        self.assertEqual(self.db.db.execute('SELECT state FROM abang_requests').fetchone()[0], 'delivered')
        self.assertTrue(any(call[1] == '/edit' and call[2]['text'] == 'Delivered' for call in self.gateway.calls))
        self.assertEqual(len([key for key in self.gateway.keys if key.startswith('abang:request:')]), 1)

    def test_retry_and_link_reply(self):
        values = {'person': 'vendor', 'destination': '-200', 'note': None}
        rid = self.write('request', values, 'retry')
        self.assertEqual(self.write('request', values, 'retry'), rid)
        self.assertEqual(self.db.db.execute('SELECT COUNT(*) FROM abang_requests').fetchone()[0], 1)
        self.worker.tick()
        self.gateway.items = [{'seq': 1, 'kind': 'update', 'chat': -100, 'message_id': 104, 'reply_to_message_id': 101, 'from': {'id': 42}, 'has_file': False, 'has_link': True}]
        self.worker.tick()
        self.assertEqual(self.db.db.execute('SELECT state FROM abang_requests').fetchone()[0], 'delivered')
        self.assertEqual(self.db.db.execute('SELECT state FROM abang_history ORDER BY id').fetchall(), [('requested',), ('forwarding',), ('delivered',)])

    def test_missing_person_and_cancel(self):
        self.assertIsInstance(self.store.write('request', {'person': '@handle', 'destination': '-200', 'note': None}, actor='coordinator', idempotency_key='x', confirm=None), NeedsPerson)
        rid = self.write('request', {'person': 'vendor', 'destination': '-200', 'note': None}, 'one')
        self.worker.tick()
        self.write('cancel', {'request': rid}, 'cancel')
        self.gateway.items = [{'seq': 1, 'kind': 'update', 'chat': -100, 'message_id': 103, 'reply_to_message_id': 101, 'from': {'id': 42}, 'has_file': False, 'has_link': True}]
        self.worker.tick()
        self.assertFalse(any(call[1] == '/forward' for call in self.gateway.calls))
        self.assertEqual(self.db.db.execute('SELECT state FROM abang_requests').fetchone()[0], 'cancelled')
