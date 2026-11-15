"""Approval verbs against an in-memory t6 database."""
import unittest

from test_person import MemoryRegistry
from t6.approval import Approvals
from t6.preview import PreviewTokens
from t6.verbs import Done, Preview, Refused


class ApprovalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = MemoryRegistry()
        self.addCleanup(self.registry.db.close)
        with self.registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES ('t6n_11111111111111111111111111111111', 'vendor', '[]', '[]', 42, 0, 1)")
        self.actor = 't6n_11111111111111111111111111111111'
        self.approvals = Approvals(self.registry, PreviewTokens('test'))

    def test_request_is_previewed_and_confirmed_once(self) -> None:
        values = {'gate': 'grade', 'target': 'cut 1', 'tapper_rule': 'person:' + self.actor}
        preview = self.approvals.write('request', values, actor=self.actor, idempotency_key='request-1')
        self.assertIsInstance(preview, Preview)
        assert isinstance(preview, Preview)
        self.assertNotIn('approval_id', preview.plan)
        done = self.approvals.write('request', values, actor=self.actor, idempotency_key='request-1', confirm=preview.token)
        self.assertIsInstance(done, Done)
        assert isinstance(done, Done)
        self.assertTrue(done.result['approval_id'].startswith('t6a_'))
        stored = self.approvals.get(done.result['approval_id'])
        assert stored is not None
        self.assertEqual(stored['state'], 'requested')
        self.assertIsInstance(self.approvals.write('request', values, actor=self.actor, idempotency_key='request-1', confirm=preview.token), Refused)

    def test_all_gates_and_unknown_gate(self) -> None:
        for gate in ('vfx committee', 'grade', 'delivery sign-off', 'drive wipe', 'fleet promotion'):
            with self.subTest(gate=gate):
                self.assertIsInstance(self.approvals.write('request', {'gate': gate, 'target': 'cut'}, actor=self.actor), Preview)
        self.assertIsInstance(self.approvals.write('request', {'gate': 'mystery', 'target': 'cut'}, actor=self.actor), Refused)
        self.assertIsInstance(self.approvals.write('request', {'gate': 'grade', 'target': 'cut', 'tapper_rule': ''}, actor=self.actor), Refused)

    def test_decisions_require_the_allowed_person_and_denials_need_a_reason(self) -> None:
        values = {'gate': 'grade', 'target': 'cut', 'tapper_rule': 'person:' + self.actor}
        preview = self.approvals.write('request', values, actor=self.actor, idempotency_key='request')
        assert isinstance(preview, Preview)
        done = self.approvals.write('request', values, actor=self.actor, idempotency_key='request', confirm=preview.token)
        assert isinstance(done, Done)
        approval = done.result['approval_id']
        with self.registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES ('stranger', 'staff', '[]', '[]', 99, 0, 1)")
        self.assertIsInstance(self.approvals.write('grant', {'approval': approval}, actor='stranger'), Refused)
        self.assertTrue(any(entry.action == 'approval_refused' and entry.actor == 'stranger' for entry in self.registry.audit(approval)))
        self.assertIsInstance(self.approvals.write('deny', {'approval': approval, 'reason': ''}, actor=self.actor), Refused)
        decision = {'approval': approval, 'reason': 'needs changes'}
        preview = self.approvals.write('deny', decision, actor=self.actor, idempotency_key='deny')
        assert isinstance(preview, Preview)
        self.assertIsInstance(self.approvals.write('deny', decision, actor=self.actor, idempotency_key='deny', confirm=preview.token), Done)
        record = self.approvals.get(approval)
        assert record is not None
        self.assertEqual((record['state'], record['decided_by'], record['reason']), ('denied', self.actor, 'needs changes'))
        self.assertIsInstance(self.approvals.write('grant', {'approval': approval}, actor=self.actor), Refused)

    def test_project_activity_and_idempotent_request_retry(self) -> None:
        from t6.registry import Registered
        self.registry._new_id = lambda: 't6p_' + '1' * 24
        project = self.registry.register(actor=self.actor, idempotency_key='project')
        assert isinstance(project, Registered)
        values = {'gate': 'delivery sign-off', 'target': project.project.id, 'tapper_rule': 'person:' + self.actor}
        preview = self.approvals.write('request', values, actor=self.actor, idempotency_key='request-project')
        assert isinstance(preview, Preview)
        done = self.approvals.write('request', values, actor=self.actor, idempotency_key='request-project', confirm=preview.token)
        assert isinstance(done, Done)
        retry = self.approvals.write('request', values, actor=self.actor, idempotency_key='request-project')
        assert isinstance(retry, Preview)
        repeated = self.approvals.write('request', values, actor=self.actor, idempotency_key='request-project', confirm=retry.token)
        assert isinstance(repeated, Done)
        self.assertEqual(done.result, repeated.result)
        entries = [item for item in self.registry.activity(project.project.id) if item.action == 'approval_request']
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].subject_id, done.result['approval_id'])
        self.assertTrue(any(item.action == 'approval_request' for item in self.registry.audit(done.result['approval_id'])))

    def test_client_cut_targets_version_and_records_grant(self) -> None:
        from t6.deliverable import DeliverableStore
        from t6.registry import Registered
        from t6.version import VersionStore
        self.registry._new_id = lambda: 't6p_' + '1' * 24
        project = self.registry.register(actor=self.actor, idempotency_key='project', notion_page_id='p')
        assert isinstance(project, Registered)
        store = DeliverableStore(self.registry, PreviewTokens('test'))
        rows = [{'page_id': 'd', 'project_page': 'p', 'code': 'FTR'}]
        migration = store.migrate(rows, actor=self.actor)
        deliverable = store.migrate(rows, actor=self.actor, apply=True, digest=migration['digest'])['entries'][0]['t6_id']
        values = {'gate': 'client-cut', 'target': deliverable, 'tapper_rule': 'person:' + self.actor}
        self.assertIsInstance(self.approvals.write('request', values, actor=self.actor), Refused)
        with self.registry._transaction() as db:
            db.execute('INSERT INTO versions VALUES (?, ?, ?, NULL, NULL, NULL)', ('v1', deliverable, 'file1'))
            db.execute('INSERT INTO versions VALUES (?, ?, ?, NULL, NULL, NULL)', ('v2', deliverable, 'file2'))
        preview = self.approvals.write('request', values, actor=self.actor, idempotency_key='request-cut')
        assert isinstance(preview, Preview)
        self.assertEqual(preview.plan['target'], 'v2')
        explicit_values = {**values, 'version': 'v1'}
        explicit = self.approvals.write('request', explicit_values, actor=self.actor, idempotency_key='explicit')
        assert isinstance(explicit, Preview)
        self.assertEqual(explicit.plan['target'], 'v1')
        explicit_done = self.approvals.write('request', explicit_values, actor=self.actor, idempotency_key='explicit', confirm=explicit.token)
        assert isinstance(explicit_done, Done)
        approval = self.approvals.get(explicit_done.result['approval_id'])
        assert approval is not None
        self.assertEqual(approval['target'], 'v1')
        self.assertIsInstance(self.approvals.write('request', {**values, 'version': 'bad'}, actor=self.actor), Refused)
        with self.registry._transaction() as db:
            db.execute('INSERT INTO versions VALUES (?, ?, ?, NULL, NULL, NULL)', ('v3', deliverable, 'file3'))
        done = self.approvals.write('request', values, actor=self.actor, idempotency_key='request-cut', confirm=preview.token)
        assert isinstance(done, Done)
        self.assertEqual(done.result['target'], 'v2')
        decision = {'approval': done.result['approval_id']}
        grant = self.approvals.write('grant', decision, actor=self.actor, idempotency_key='grant-cut')
        assert isinstance(grant, Preview)
        self.assertIsInstance(self.approvals.write('grant', decision, actor=self.actor, idempotency_key='grant-cut', confirm=grant.token), Done)
        version = VersionStore(self.registry, PreviewTokens('test')).get('v2')
        assert version is not None
        self.assertEqual(version['approvals'][0]['state'], 'granted')
        self.assertTrue(any(item.action == 'approval_grant' and 'v2' in item.summary for item in self.registry.activity(project.project.id)))

    def test_commands_catalogue_exposes_the_three_verbs(self) -> None:
        from t6.verbs import catalogue
        found = {entry['verb'] for entry in catalogue() if entry['noun'] == 'approval'}
        self.assertEqual(found, {'request', 'grant', 'deny'})


class ApprovalCardTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        from t6.operator import ActionStore, OperatorService
        from t6.person import People
        from t6.server import Grant, ServerConfig, WebhookApp
        from t6.server.queue import JobQueue
        from t6.server.verbs import VerbAPI
        from test_operator import FakeGateway
        self.registry = MemoryRegistry()
        self.addCleanup(self.registry.db.close)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.actor = 't6n_' + '1' * 32
        self.stranger = 't6n_' + '2' * 32
        with self.registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES (?, 'vendor', '[]', '[]', 42, 0, 1)", (self.actor,))
            db.execute("INSERT INTO persons VALUES (?, 'vendor', '[]', '[]', 99, 0, 1)", (self.stranger,))
        self.people = People(self.registry, PreviewTokens('test'))
        self.actions = ActionStore(self.tmp.name + '/actions')
        service = OperatorService(self.registry, self.actions, (42,), people=self.people)
        self.queue = JobQueue(self.tmp.name + '/queue')
        config = ServerConfig(operator_ids=(42,), grants=(Grant('worker', operator=True, claim=frozenset({'main'}), submit=frozenset({'main'})),))
        self.app = WebhookApp(self.queue, config, registry=self.registry, operator=service)
        self.api = VerbAPI(self.people, self.queue, {'person': self.actor, 'gateway': 'gateway'}, lambda _: None, self.app)
        self.gateway = FakeGateway()
        from unittest.mock import patch
        env = patch.dict('os.environ', {'T6_PREVIEW_SIGNING_KEY': 'test'})
        env.start()
        self.addCleanup(env.stop)

    def send(self, path: str, body: dict, *, secret: str = 'person', headers: dict | None = None) -> dict:  # type: ignore[type-arg]
        import json
        from t6.server import Request
        response = self.api(Request('POST', path, {'Authorization': 'Bearer ' + secret, **(headers or {})}, json.dumps(body).encode()))
        self.assertEqual(response.status, 200, response.body)
        return response.body  # type: ignore[return-value]

    def drain(self) -> None:
        from test_operator import GATEWAY_SECRET
        from test_server_app import InProcess
        from t6.worker import QueueClient, Worker
        from t6.worker.gateway import GatewayClient
        from t6.worker.operator import CardDelivery, OperatorClient
        transport = InProcess(self.app)
        worker = Worker(QueueClient(transport, 'worker'), 'main', CardDelivery(OperatorClient(transport, 'worker'), GatewayClient(self.gateway, GATEWAY_SECRET)).handlers())
        while worker.run_once():
            pass

    def test_group_link_command_delivers_approval_and_grant_confirms_link(self) -> None:
        from t6.registry import Registered
        self.registry._new_id = lambda: 't6p_' + '3' * 24
        project = self.registry.register(actor=self.actor, idempotency_key='chat-project')
        assert isinstance(project, Registered)
        with self.registry._transaction() as db:
            db.execute("INSERT INTO person_permissions VALUES (?, 'operator')", (self.actor,))
        body = {'kind': 'command', 'command': 'link', 'args': project.project.id,
                'chat': -100, 'topic': 12, 'from': {'id': 42}, 'message_id': 123}
        result = self.send('/tap', body, secret='gateway', headers={'X-Telegram-User-Id': '42', 'Idempotency-Key': 'cmd-1'})
        self.assertEqual(result['outcome'], 'accepted')
        self.drain()
        self.assertIn('chat -100, topic 12', self.gateway.messages[1]['text'])
        payload = self.gateway.messages[1]['buttons'][0][0]['payload']
        result = self.send('/tap', {'data': payload, 'from': {'id': 42}, 'chat': 42, 'message_id': 1001}, secret='gateway', headers={'X-Telegram-User-Id': '42', 'Idempotency-Key': 'tap-link'})
        self.assertEqual(result['outcome'], 'granted')
        with self.registry._transaction() as db:
            self.assertEqual(db.execute('SELECT project_id, chat_id, topic_id, state FROM chat_links').fetchone(), (project.project.id, -100, 12, 'active'))

    def test_request_delivers_one_card_and_only_the_allowed_person_grants_once(self) -> None:
        values = {'gate': 'grade', 'target': 'cut', 'tapper_rule': 'person:' + self.actor, 'idempotency_key': 'one'}
        preview = self.send('/v1/approval/request', values)
        self.assertEqual(preview['outcome'], 'preview')
        done = self.send('/v1/approval/request', {**values, 'confirm': preview['token']})
        self.assertEqual(done['outcome'], 'done')
        self.drain()
        self.assertEqual(self.gateway.sends, 1)
        message = self.gateway.messages[1]
        payload = message['buttons'][0][0]['payload']
        def tap(who: int, key: str) -> dict:  # type: ignore[type-arg]
            return self.send('/tap', {'data': payload, 'from': {'id': who}, 'chat': 42, 'message_id': 1001}, secret='gateway', headers={'X-Telegram-User-Id': str(who), 'Idempotency-Key': key})
        self.assertEqual(tap(99, 'stranger')['outcome'], 'rejected')
        self.assertEqual(tap(42, 'allowed')['outcome'], 'granted')
        self.assertEqual(tap(42, 'again')['outcome'], 'already_handled')
        approval = Approvals(self.registry, PreviewTokens('test')).get(done['result']['approval_id'])
        assert approval is not None
        self.assertEqual((approval['state'], approval['decided_by']), ('granted', self.actor))
        self.assertEqual(self.actions.rejections()[0][0], 'not_allowed')

    def test_card_denial_records_selected_reason(self) -> None:
        values = {'gate': 'grade', 'target': 'cut', 'tapper_rule': 'person:' + self.actor, 'idempotency_key': 'denial'}
        preview = self.send('/v1/approval/request', values)
        done = self.send('/v1/approval/request', {**values, 'confirm': preview['token']})
        self.drain()
        payload = self.gateway.messages[1]['buttons'][0][1]['payload']
        def tap(data: str, key: str) -> dict:  # type: ignore[type-arg]
            return self.send('/tap', {'data': data, 'from': {'id': 42}, 'chat': 42, 'message_id': 1001}, secret='gateway', headers={'X-Telegram-User-Id': '42', 'Idempotency-Key': key})
        self.assertEqual(tap(payload, 'deny')['outcome'], 'choose_reason')
        self.drain()
        payload = self.gateway.messages[1]['buttons'][0][0]['payload']
        self.assertEqual(tap(payload, 'reason')['outcome'], 'denied')
        approval = Approvals(self.registry, PreviewTokens('test')).get(done['result']['approval_id'])
        assert approval is not None
        self.assertEqual((approval['state'], approval['reason']), ('denied', 'Changes requested'))

    def test_request_survives_restart_before_card_creation(self) -> None:
        from t6.server import WebhookApp
        store = Approvals(self.registry, PreviewTokens('test'))
        values = {'gate': 'drive wipe', 'target': 'drive', 'tapper_rule': 'person:' + self.actor}
        preview = store.write('request', values, actor=self.actor, idempotency_key='recovery')
        assert isinstance(preview, Preview)
        done = store.write('request', values, actor=self.actor, idempotency_key='recovery', confirm=preview.token)
        assert isinstance(done, Done)
        self.app = WebhookApp(self.queue, self.app._config, registry=self.registry, operator=self.app._operator)
        self.drain()
        self.assertEqual(self.gateway.sends, 1)

    def test_gate_specific_cli_aliases_are_callable(self) -> None:
        import io
        from contextlib import redirect_stdout
        from unittest.mock import patch
        from t6.cli import main
        with patch('t6.client.Client.call', return_value=Done(result={})) as call, redirect_stdout(io.StringIO()):
            self.assertEqual(main(['approval', 'request-grade', 'cut']), 0)
        self.assertEqual(call.call_args.args[2].gate, 'grade')

    def test_gate_specific_api_alias_requests_the_named_gate(self) -> None:
        result = self.send('/v1/approval/request-grade', {'target': 'cut', 'idempotency_key': 'alias'})
        self.assertEqual(result['outcome'], 'preview')
        self.assertEqual(result['plan']['gate'], 'grade')

    def test_cli_denial_updates_the_existing_card(self) -> None:
        values = {'gate': 'fleet promotion', 'target': 'pilot', 'tapper_rule': 'person:' + self.actor, 'idempotency_key': 'request-cli'}
        preview = self.send('/v1/approval/request', values)
        done = self.send('/v1/approval/request', {**values, 'confirm': preview['token']})
        self.drain()
        decision = {'approval': done['result']['approval_id'], 'reason': 'pilot failed', 'idempotency_key': 'cli-deny'}
        preview = self.send('/v1/approval/deny', decision)
        self.assertEqual(preview['outcome'], 'preview')
        result = self.send('/v1/approval/deny', {**decision, 'confirm': preview['token']})
        self.assertEqual(result['outcome'], 'done')
        self.drain()
        self.assertEqual(self.gateway.messages[1]['buttons'], [])
        self.assertIn('Denied', self.gateway.messages[1]['text'])
        approval = Approvals(self.registry, PreviewTokens('test')).get(done['result']['approval_id'])
        assert approval is not None
        self.assertEqual(approval['reason'], 'pilot failed')
