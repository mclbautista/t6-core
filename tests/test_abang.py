import json
import sqlite3
import unittest
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from t6.abang import Abang
from t6.preview import PreviewTokens
from t6.registry.sqlite import SqliteProjectRegistry, _migrate
from t6.verbs import Done, NeedsPerson, Preview
from t6.worker.gateway import GatewayClient
from t6.worker.linked_chats import LinkedChats


class MemoryRegistry(SqliteProjectRegistry):
    def __init__(self) -> None:
        self.db = sqlite3.connect(':memory:')
        self._clock = lambda: 123.0
        _migrate(self.db)
        self.db.execute("INSERT INTO persons VALUES ('vendor', 'vendor', '[]', '[]', 42, 0, 1)")
        self.db.execute("INSERT INTO chat_links VALUES ('link', 'project', -100, NULL, 'active')")
        self.db.execute("INSERT INTO linked_chat_members VALUES (-100, 42, 'vendor', 1)")
        self.db.commit()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.db
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise


class Gateway:
    def __init__(self) -> None:
        self.keys: dict[str, int] = {}
        self.items: list[dict[str, Any]] = []
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.states: dict[int, dict[str, Any]] = {}

    def send(self, method: str, path: str, headers: Mapping[str, str], body: bytes) -> tuple[int, dict[str, Any]]:
        data = json.loads(body) if body else {}
        self.calls.append((method, path, data))
        if method == 'PUT':
            return 200, {}
        if path.startswith('/updates'):
            after = int(path.split('=')[1])
            return 200, {'items': [i for i in self.items if i['seq'] > after]}
        if method == 'GET':
            return 200, self.states.get(int(path.rsplit('/', 1)[1]), {'state': 'sent', 'telegram_message_id': 101})
        key = data['idempotency_key']
        self.keys.setdefault(key, len(self.keys) + 1)
        return 202, {'message_ref': self.keys[key]}


class AbangTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = MemoryRegistry()
        self.addCleanup(self.db.db.close)
        self.store = Abang(self.db, PreviewTokens('secret'))
        self.gateway = Gateway()
        self.worker = LinkedChats(self.db, GatewayClient(self.gateway, 'secret'), (42,))

    def write(self, verb: str, values: dict[str, Any], key: str) -> str:
        preview = self.store.write(verb, values, actor='coordinator', idempotency_key=key, confirm=None)
        assert isinstance(preview, Preview)
        result = self.store.write(verb, values, actor='coordinator', idempotency_key=key, confirm=preview.token)
        assert isinstance(result, Done)
        return str(result.result['request_id'])

    def test_relay_once_and_ignore_plain_reply(self) -> None:
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

    def test_retry_and_link_reply(self) -> None:
        values = {'person': 'vendor', 'destination': '-200', 'note': None}
        rid = self.write('request', values, 'retry')
        self.assertEqual(self.write('request', values, 'retry'), rid)
        self.assertEqual(self.db.db.execute('SELECT COUNT(*) FROM abang_requests').fetchone()[0], 1)
        self.worker.tick()
        self.gateway.items = [{'seq': 1, 'kind': 'update', 'chat': -100, 'message_id': 104, 'reply_to_message_id': 101, 'from': {'id': 42}, 'has_file': False, 'has_link': True}]
        self.worker.tick()
        self.assertEqual(self.db.db.execute('SELECT state FROM abang_requests').fetchone()[0], 'delivered')
        self.assertEqual(self.db.db.execute('SELECT state FROM abang_history ORDER BY id').fetchall(), [('requested',), ('forwarding',), ('delivered',)])

    def test_missing_person_and_cancel(self) -> None:
        self.assertIsInstance(self.store.write('request', {'person': '@handle', 'destination': '-200', 'note': None}, actor='coordinator', idempotency_key='x', confirm=None), NeedsPerson)
        rid = self.write('request', {'person': 'vendor', 'destination': '-200', 'note': None}, 'one')
        self.worker.tick()
        self.write('cancel', {'request': rid}, 'cancel')
        self.gateway.items = [{'seq': 1, 'kind': 'update', 'chat': -100, 'message_id': 103, 'reply_to_message_id': 101, 'from': {'id': 42}, 'has_file': False, 'has_link': True}]
        self.worker.tick()
        self.assertFalse(any(call[1] == '/forward' for call in self.gateway.calls))
        self.assertEqual(self.db.db.execute('SELECT state FROM abang_requests').fetchone()[0], 'cancelled')

    def test_request_message_asks_for_a_reply_with_the_file_or_link(self) -> None:
        self.write('request', {'person': 'vendor', 'destination': '-200', 'note': 'Graded trailer'}, 'ask')
        self.worker.tick()
        text = next(call[2]['text'] for call in self.gateway.calls if call[1] == '/notify')
        self.assertIn('reply to this message with the file or a link', text)
        self.assertIn('Graded trailer', text)

    def test_a_request_that_could_not_be_sent_fails_and_does_not_hold_the_chat(self) -> None:
        rid = self.write('request', {'person': 'vendor', 'destination': '-200', 'note': None}, 'lost')
        self.gateway.states[1] = {'state': 'failed'}
        self.gateway.items = [{'seq': 1, 'kind': 'update', 'chat': -100, 'message_id': 102, 'reply_to_message_id': 101, 'from': {'id': 42}, 'has_file': True, 'has_link': False}]
        self.worker.tick()
        self.worker.tick()
        self.assertEqual(self.db.db.execute('SELECT state FROM abang_requests').fetchone()[0], 'failed')
        self.assertEqual(self.db.db.execute('SELECT seq FROM linked_chat_cursor').fetchone()[0], 1)
        self.assertEqual(self.db.db.execute('SELECT state FROM abang_history ORDER BY id').fetchall(), [('requested',), ('failed',)])
        self.assertFalse(any(call[1] == '/forward' for call in self.gateway.calls))
        self.assertTrue(any(call[1] == '/notify' and rid in call[2]['text'] and 'failed' in call[2]['text'] for call in self.gateway.calls))

    def test_a_forward_that_fails_for_good_ends_the_request(self) -> None:
        self.write('request', {'person': 'vendor', 'destination': '-200', 'note': None}, 'badforward')
        self.worker.tick()
        self.gateway.states[2] = {'state': 'failed'}
        self.gateway.items = [{'seq': 1, 'kind': 'update', 'chat': -100, 'message_id': 103, 'reply_to_message_id': 101, 'from': {'id': 42}, 'has_file': True, 'has_link': False}]
        self.worker.tick()
        self.assertEqual(self.db.db.execute('SELECT state FROM abang_requests').fetchone()[0], 'failed')
        calls = len(self.gateway.calls)
        self.worker.tick()
        self.assertFalse(any(call[1] == '/forward' for call in self.gateway.calls[calls:]))
        self.assertEqual(self.db.db.execute('SELECT state FROM abang_history ORDER BY id').fetchall(), [('requested',), ('forwarding',), ('failed',)])

    def test_request_in_a_linked_topic_is_tracked_for_dead_topic_detection(self) -> None:
        with self.db._transaction() as db:
            db.execute("INSERT INTO chat_links VALUES ('topic-link', 'project', -100, 5, 'active')")
            db.execute("INSERT INTO abang_requests VALUES ('request', 'vendor', -100, 5, -200, NULL, NULL, 'coordinator', 'requested', NULL, NULL, NULL, NULL, NULL)")
        self.worker.tick()
        self.assertEqual(self.db.db.execute('SELECT chat_id, topic_id, links FROM linked_chat_sends').fetchall(), [(-100, 5, '["topic-link"]')])
