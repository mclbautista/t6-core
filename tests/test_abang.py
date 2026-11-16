import json
import sqlite3
import unittest
from collections.abc import Iterator, Mapping
import tempfile
from unittest.mock import patch
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

    def test_a_freelancer_can_be_asked_and_staff_or_a_client_cannot(self) -> None:
        with self.db._transaction() as db:
            db.execute("UPDATE persons SET kind = 'freelancer' WHERE id = 'vendor'")
        self.write('request', {'person': 'vendor', 'destination': '-200', 'note': None}, 'freelancer')
        self.assertEqual(self.db.db.execute('SELECT state FROM abang_requests').fetchone()[0], 'requested')
        for kind in ('staff', 'client'):
            with self.db._transaction() as db:
                db.execute('UPDATE persons SET kind = ? WHERE id = ?', (kind, 'vendor'))
            refused = self.store.write('request', {'person': 'vendor', 'destination': '-200', 'note': None}, actor='coordinator', idempotency_key=kind, confirm=None)
            self.assertIsInstance(refused, NeedsPerson)

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

    def test_non_reply_file_offers_candidate_card(self) -> None:
        rid = self.write('request', {'person': 'vendor', 'destination': '-200', 'note': None}, 'candidate')
        self.worker.tick()
        self.gateway.items = [{'seq': 1, 'kind': 'update', 'chat': -100, 'message_id': 104, 'from': {'id': 42}, 'has_file': True}]
        self.worker.tick()
        cards = [call[2] for call in self.gateway.calls if call[1] == '/notify' and call[2]['text'].startswith('Is this it?')]
        self.assertEqual(len(cards), 1)
        self.assertEqual([button['text'] for button in cards[0]['buttons'][0]], ['Yes', 'No'])
        self.assertFalse(any(call[1] == '/forward' for call in self.gateway.calls))
        self.assertTrue(cards[0]['buttons'][0][0]['payload'].startswith('abang:'))

    def test_a_file_replying_to_another_message_is_a_candidate(self) -> None:
        self.write('request', {'person': 'vendor', 'destination': '-200', 'note': None}, 'other-reply')
        self.worker.tick()
        self.gateway.items = [{'seq': 1, 'kind': 'update', 'chat': -100, 'message_id': 106, 'reply_to_message_id': 555, 'from': {'id': 42}, 'has_file': True}]
        self.worker.tick()
        cards = [call[2] for call in self.gateway.calls if call[1] == '/notify' and call[2]['text'].startswith('Is this it?')]
        self.assertEqual(len(cards), 1)
        self.assertFalse(any(call[1] == '/forward' for call in self.gateway.calls))

    def test_candidate_taps_allow_only_freelancer_and_forward_once(self) -> None:
        from t6.person import People
        from t6.server import Request, ServerConfig, WebhookApp
        from t6.server.queue import JobQueue
        from t6.server.verbs import VerbAPI
        self.db.db.execute("INSERT INTO persons VALUES ('coordinator', 'staff', '[]', '[]', 43, 0, 1)")
        self.db.db.commit()
        self.write('request', {'person': 'vendor', 'destination': '-200', 'note': None}, 'candidate')
        self.worker.tick()
        self.gateway.items = [{'seq': 1, 'kind': 'update', 'chat': -100, 'message_id': 104, 'from': {'id': 42}, 'has_link': True}]
        self.worker.tick()
        card = next(call[2] for call in self.gateway.calls if call[1] == '/notify' and call[2]['text'].startswith('Is this it?'))
        yes = card['buttons'][0][0]['payload']
        with tempfile.TemporaryDirectory() as tmp, patch.dict('os.environ', {'T6_PREVIEW_SIGNING_KEY': 'secret'}):
            queue = JobQueue(tmp + '/queue')
            api = VerbAPI(People(self.db, PreviewTokens('secret')), queue, {'gateway': 'gateway'}, lambda _: None, WebhookApp(queue, ServerConfig(), registry=self.db))
            def tap(who: int, key: str) -> Any:
                body = {'data': yes, 'from': {'id': who}}
                headers = {'Authorization': 'Bearer gateway', 'X-Telegram-User-Id': str(who), 'Idempotency-Key': key}
                return api(Request('POST', '/tap', headers, json.dumps(body).encode())).body['outcome']
            self.assertEqual(tap(43, 'denied'), 'rejected')
            self.assertEqual(tap(42, 'yes'), 'accepted')
            self.assertIn(tap(42, 'again'), ('already_handled', 'rejected'))
        self.worker.tick()
        self.worker.tick()
        self.assertEqual(len([call for call in self.gateway.calls if call[1] == '/forward']), 1)
        self.assertEqual(self.db.db.execute("SELECT actor FROM abang_history WHERE state = 'forwarding'").fetchone()[0], 'vendor')
        self.assertEqual(self.db.db.execute("SELECT count(*) FROM audit WHERE action = 'tap_rejected'").fetchone()[0], 1)

    def test_no_keeps_request_open_for_a_later_reply(self) -> None:
        from t6.person import People
        from t6.server import Request, ServerConfig, WebhookApp
        from t6.server.queue import JobQueue
        from t6.server.verbs import VerbAPI
        self.write('request', {'person': 'vendor', 'destination': '-200', 'note': None}, 'no-case')
        self.worker.tick()
        self.gateway.items = [{'seq': 1, 'kind': 'update', 'chat': -100, 'message_id': 104, 'from': {'id': 42}, 'has_file': True}]
        self.worker.tick()
        card = next(call[2] for call in self.gateway.calls if call[1] == '/notify' and call[2]['text'].startswith('Is this it?'))
        with tempfile.TemporaryDirectory() as tmp, patch.dict('os.environ', {'T6_PREVIEW_SIGNING_KEY': 'secret'}):
            queue = JobQueue(tmp + '/queue')
            api = VerbAPI(People(self.db, PreviewTokens('secret')), queue, {'gateway': 'gateway'}, lambda _: None, WebhookApp(queue, ServerConfig(), registry=self.db))
            body = {'data': card['buttons'][0][1]['payload'], 'from': {'id': 42}}
            headers = {'Authorization': 'Bearer gateway', 'X-Telegram-User-Id': '42', 'Idempotency-Key': 'no'}
            self.assertEqual(api(Request('POST', '/tap', headers, json.dumps(body).encode())).body['outcome'], 'accepted')
        self.gateway.items.append({'seq': 2, 'kind': 'update', 'chat': -100, 'message_id': 105, 'reply_to_message_id': 101, 'from': {'id': 42}, 'has_file': True})
        self.worker.tick()
        forwards = [call[2] for call in self.gateway.calls if call[1] == '/forward']
        self.assertEqual([call['message_id'] for call in forwards], [105])

    def test_nudge_at_three_days_and_not_after_cancel_or_delivery(self) -> None:
        from t6.worker.internal_queue import InternalJobQueue, InternalQueueClient
        from t6.worker.runner import Worker
        now = [123.0]
        self.db._clock = lambda: now[0]
        queue = InternalJobQueue.__new__(InternalJobQueue)
        queue._db, queue._clock, queue._max_attempts = self.db, self.db._clock, 3
        client = InternalQueueClient(queue)
        runner = Worker(client, 'main', {'abang_nudge': self.worker.nudge})
        with self.db._transaction() as db:
            db.execute("INSERT INTO persons VALUES ('coordinator', 'staff', '[]', '[]', 7, 0, 1)")
        self.write('request', {'person': 'vendor', 'destination': '-200', 'note': None}, 'completed')
        self.worker.tick()
        self.gateway.items = [{'seq': 1, 'kind': 'update', 'chat': -100, 'message_id': 103, 'reply_to_message_id': 101, 'from': {'id': 42}, 'has_file': True}]
        self.worker.tick()
        self.write('request', {'person': 'vendor', 'destination': '-200', 'note': None}, 'nudge')
        cancelled = self.write('request', {'person': 'vendor', 'destination': '-200', 'note': None}, 'cancelled')
        self.write('cancel', {'request': cancelled}, 'stop')
        self.worker.tick()
        now[0] += 3 * 86400 - 1
        self.assertFalse(runner.run_once())
        now[0] += 1
        while runner.run_once():
            pass
        nudges = [call for call in self.gateway.calls if call[1] == '/notify' and 'no file for 3 days' in call[2]['text']]
        self.assertEqual(len(nudges), 1)
        self.assertEqual(nudges[0][2]['to'], 7)
        self.assertFalse(runner.run_once())

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
