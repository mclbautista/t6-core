import json
import unittest
from typing import Any
from unittest.mock import patch

from test_person import MemoryRegistry
from t6.approval import Approvals
from t6.chat_link import ChatLinks
from t6.person import People
from t6.preview import PreviewTokens
from t6.registry import Registered
from t6.verbs import Done, Preview, Refused, catalogue


class ChatLinkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = MemoryRegistry()
        self.addCleanup(self.registry.db.close)
        self.actor = 't6n_' + '1' * 32
        with self.registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES (?, 'staff', '[]', '[]', 42, 0, 1)", (self.actor,))
            db.execute("INSERT INTO person_permissions VALUES (?, 'operator')", (self.actor,))
        self.registry._new_id = lambda: 't6p_' + '1' * 24
        project = self.registry.register(actor=self.actor, idempotency_key='register')
        assert isinstance(project, Registered)
        self.project = project.project.id
        self.tokens = PreviewTokens('test')
        self.links = ChatLinks(self.registry, self.tokens)
        self.approvals = Approvals(self.registry, self.tokens)

    def approved(self, verb: str, values: dict[str, Any], key: str) -> Done:
        preview = self.links.write(verb, values, actor=self.actor, idempotency_key=key)
        assert isinstance(preview, Preview)
        self.assertEqual((preview.plan['chat'], preview.plan['topic']), (values['chat'], values['topic']))
        again = self.links.write(verb, values, actor=self.actor, idempotency_key=key)
        assert isinstance(again, Preview)
        self.assertEqual(again.token, preview.token)
        self.assertIsInstance(self.links.write(verb, values, actor=self.actor, idempotency_key=key, confirm=preview.token), Refused)
        decision = {'approval': preview.plan['approval_id']}
        grant = self.approvals.write('grant', decision, actor=self.actor, idempotency_key=key + '-grant')
        assert isinstance(grant, Preview)
        self.assertIsInstance(self.approvals.write('grant', decision, actor=self.actor, idempotency_key=key + '-grant', confirm=grant.token), Done)
        result = self.links.write(verb, values, actor=self.actor, idempotency_key=key, confirm=preview.token)
        assert isinstance(result, Done)
        return result

    def test_link_unlink_history_and_noop(self) -> None:
        values = {'project': self.project, 'chat': -100, 'topic': 12}
        first = self.approved('link-chat', values, 'link')
        self.assertTrue(first.result['chat_link_id'].startswith('t6h_'))
        self.assertIsInstance(self.links.write('link-chat', values, actor=self.actor), Done)
        self.approved('unlink-chat', values, 'unlink')
        self.approved('link-chat', values, 'relink')
        self.approved('unlink-chat', values, 'unlink-again')
        with self.registry._transaction() as db:
            self.assertEqual([r[0] for r in db.execute('SELECT state FROM chat_link_history ORDER BY id')], ['active', 'inactive', 'active', 'inactive'])
        actions = [a.action for a in self.registry.activity(self.project)]
        self.assertEqual(actions.count('project_link_chat'), 2)
        self.assertEqual(actions.count('project_unlink_chat'), 2)
        self.assertEqual(len([a for a in self.registry.audit(self.project) if a.action.startswith('project_')]), 4)

    def test_linking_and_unlinking_queue_a_subscription_update_at_once(self) -> None:
        from test_abang import Gateway
        from t6.worker.client import WorkerJob
        from t6.worker.gateway import GatewayClient
        from t6.worker.linked_chats import LinkedChats
        values = {'project': self.project, 'chat': -100, 'topic': 12}
        gateway = Gateway()
        worker = LinkedChats(self.registry, GatewayClient(gateway, 'secret'))
        job = WorkerJob(1, 'main', 'chat_subscriptions', 'x', {}, 'token', 1)
        self.approved('link-chat', values, 'link')
        with self.registry._transaction() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM internal_jobs WHERE kind = 'chat_subscriptions'").fetchone()[0], 1)
        self.assertTrue(worker.replace_subscriptions(job))
        self.assertEqual(gateway.calls[-1], ('PUT', '/subscriptions', [{'chat': -100, 'topic': 12}]))
        self.approved('unlink-chat', values, 'unlink')
        with self.registry._transaction() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM internal_jobs WHERE kind = 'chat_subscriptions'").fetchone()[0], 2)
        self.assertTrue(worker.replace_subscriptions(job))
        self.assertEqual(gateway.calls[-1], ('PUT', '/subscriptions', []))

    def test_catalogue(self) -> None:
        self.assertTrue({'link-chat', 'unlink-chat'} <= {v['verb'] for v in catalogue() if v['noun'] == 'project'})

    def test_gateway_silent_refusal(self) -> None:
        import tempfile
        from t6.server import Request
        from t6.server.queue import JobQueue
        from t6.server.verbs import VerbAPI
        from t6.server import ServerConfig, WebhookApp
        with tempfile.TemporaryDirectory() as tmp, patch.dict('os.environ', {'T6_PREVIEW_SIGNING_KEY': 'test'}):
            queue = JobQueue(tmp + '/queue')
            api = VerbAPI(People(self.registry, self.tokens), queue, {'gateway': 'gateway'}, lambda _: None, WebhookApp(queue, ServerConfig(), registry=self.registry))
            def push(who: int) -> Any:
                body = {'kind': 'command', 'command': 'link', 'args': self.project, 'chat': -100, 'topic': 12, 'from': {'id': who}}
                return api(Request('POST', '/tap', {'Authorization': 'Bearer gateway', 'X-Telegram-User-Id': str(who), 'Idempotency-Key': 'cmd-1'}, json.dumps(body).encode()))
            self.assertEqual(push(99).body['outcome'], 'rejected')
            with self.registry._transaction() as db:
                db.execute('DELETE FROM person_permissions WHERE person_id = ?', (self.actor,))
            self.assertEqual(push(42).body['outcome'], 'rejected')
            with self.registry._transaction() as db:
                self.assertEqual(db.execute('SELECT count(*) FROM chat_links').fetchone()[0], 0)

    def test_refused_commands_get_one_reply_each(self) -> None:
        import tempfile
        from t6.server import Request, ServerConfig, WebhookApp
        from t6.server.queue import JobQueue
        from t6.server.verbs import VerbAPI
        with tempfile.TemporaryDirectory() as tmp, patch.dict('os.environ', {'T6_PREVIEW_SIGNING_KEY': 'test'}):
            queue = JobQueue(tmp + '/queue')
            api = VerbAPI(People(self.registry, self.tokens), queue, {'gateway': 'gateway'}, lambda _: None, WebhookApp(queue, ServerConfig(), registry=self.registry))
            def push(who: int, command: str, args: str, key: str, topic: int | None = 12, chat: Any = -100) -> Any:
                body = {'kind': 'command', 'command': command, 'args': args, 'chat': chat, 'topic': topic, 'from': {'id': who}}
                return api(Request('POST', '/tap', {'Authorization': 'Bearer gateway', 'X-Telegram-User-Id': str(who), 'Idempotency-Key': key}, json.dumps(body).encode()))
            def replies() -> list[Any]:
                return [(job.kind, job.dedupe_key, dict(job.payload)) for job in queue.waiting('main') if job.kind == 'chat.reply']
            self.assertEqual(push(42, 'unlink', '', 'k1').body, {'outcome': 'rejected'})
            self.assertEqual(replies(), [('chat.reply', 'chat.reply:k1', {'chat': -100, 'topic': 12, 'text': 'Usage: /unlink CODE', 'key': 'k1'})])
            push(42, 'unlink', '', 'k1')
            self.assertEqual(len(replies()), 1)
            push(42, 'link', '', 'k2')
            push(42, 'link', 'A B', 'k3', topic=None)
            self.assertEqual([r[2]['text'] for r in replies()[1:]], ['Usage: /link CODE', 'Usage: /link CODE'])
            self.assertIsNone(replies()[2][2]['topic'])
            self.assertEqual(push(42, 'link', 'NOPE', 'k4').body, {'outcome': 'rejected'})
            self.assertRegex(replies()[3][2]['text'], r'^/link NOPE: .+')
            push(99, 'link', self.project, 'k5')
            push(42, 'link', self.project, 'k6', chat=None)
            self.assertEqual(len(replies()), 4)
            with self.registry._transaction() as db:
                db.execute('DELETE FROM person_permissions WHERE person_id = ?', (self.actor,))
            push(42, 'unlink', self.project, 'k7')
            self.assertEqual(replies()[4][2]['text'], 'Only a coordinator can use /unlink.')

    def test_preview_and_approval_name_a_known_topic(self) -> None:
        with self.registry._transaction() as db:
            db.execute("INSERT INTO linked_chat_topics VALUES (-100, 12, 'ABC123 - Edit')")
        preview = self.links.write('link-chat', {'project': self.project, 'chat': -100, 'topic': 12}, actor=self.actor, idempotency_key='named')
        assert isinstance(preview, Preview)
        self.assertEqual(preview.plan['topic_name'], 'ABC123 - Edit')
        with self.registry._transaction() as db:
            target = db.execute('SELECT target FROM approvals WHERE id = ?', (preview.plan['approval_id'],)).fetchone()[0]
        self.assertEqual(target, f'{self.project}: chat -100, topic 12 (ABC123 - Edit)')

    def test_topic_name_prefills_link_command(self) -> None:
        import tempfile
        from t6.server import Request, ServerConfig, WebhookApp
        from t6.server.queue import JobQueue
        from t6.server.verbs import VerbAPI
        with self.registry._transaction() as db:
            db.execute("UPDATE projects SET code = 'ABC123', code_form = 'abc123', rotation = 'in' WHERE id = ?", (self.project,))
            db.execute("INSERT INTO linked_chat_topics VALUES (-100, 12, 'ABC123 - Edit')")
        with tempfile.TemporaryDirectory() as tmp, patch.dict('os.environ', {'T6_PREVIEW_SIGNING_KEY': 'test'}):
            queue = JobQueue(tmp + '/queue')
            api = VerbAPI(People(self.registry, self.tokens), queue, {'gateway': 'gateway'}, lambda _: None, WebhookApp(queue, ServerConfig(), registry=self.registry))
            body = {'kind': 'command', 'command': 'link', 'args': '', 'chat': -100, 'topic': 12, 'from': {'id': 42}}
            response = api(Request('POST', '/tap', {'Authorization': 'Bearer gateway', 'X-Telegram-User-Id': '42', 'Idempotency-Key': 'prefill'}, json.dumps(body).encode()))
            self.assertEqual(response.body['outcome'], 'accepted')
            self.assertEqual([job for job in queue.waiting('main') if job.kind == 'chat.reply'], [])
            with self.registry._transaction() as db:
                request = json.loads(db.execute('SELECT request FROM chat_link_previews').fetchone()[0])
                self.assertEqual(request['values']['project'], 'ABC123')
