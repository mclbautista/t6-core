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

    def test_a_new_preview_of_the_same_link_reuses_the_open_approval(self) -> None:
        values = {'project': self.project, 'chat': -100, 'topic': 12}
        first = self.links.write('link-chat', values, actor=self.actor, idempotency_key='a')
        second = self.links.write('link-chat', values, actor=self.actor, idempotency_key='b')
        unlink_values = {'project': self.project, 'chat': -100, 'topic': 13}
        other = self.links.write('link-chat', unlink_values, actor=self.actor, idempotency_key='c')
        assert isinstance(first, Preview) and isinstance(second, Preview) and isinstance(other, Preview)
        self.assertEqual(first.plan['approval_id'], second.plan['approval_id'])
        self.assertNotEqual(first.plan['approval_id'], other.plan['approval_id'])
        with self.registry._transaction() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM approvals WHERE state = 'requested'").fetchone()[0], 2)
        decision = {'approval': first.plan['approval_id']}
        grant = self.approvals.write('grant', decision, actor=self.actor, idempotency_key='g')
        assert isinstance(grant, Preview)
        self.assertIsInstance(self.approvals.write('grant', decision, actor=self.actor, idempotency_key='g', confirm=grant.token), Done)
        self.assertIsInstance(self.links.write('link-chat', values, actor=self.actor, idempotency_key='b', confirm=second.token), Done)

    def test_a_denied_approval_does_not_block_a_new_link_preview(self) -> None:
        values = {'project': self.project, 'chat': -100, 'topic': 12}
        first = self.links.write('link-chat', values, actor=self.actor, idempotency_key='a')
        assert isinstance(first, Preview)
        deny = {'approval': first.plan['approval_id'], 'reason': 'no'}
        decision = self.approvals.write('deny', deny, actor=self.actor, idempotency_key='d')
        assert isinstance(decision, Preview)
        self.assertIsInstance(self.approvals.write('deny', deny, actor=self.actor, idempotency_key='d', confirm=decision.token), Done)
        again = self.links.write('link-chat', values, actor=self.actor, idempotency_key='b')
        assert isinstance(again, Preview)
        self.assertNotEqual(again.plan['approval_id'], first.plan['approval_id'])

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

    def test_approval_names_project_by_name_and_code(self) -> None:
        with self.registry._transaction() as db:
            db.execute("UPDATE projects SET code = 'ABC123', code_form = 'abc123', rotation = 'in' WHERE id = ?", (self.project,))
            db.execute("INSERT INTO bindings (system, page_id, project_id, bound_at, active) VALUES ('notion', ?, ?, 'now', 1)", ('a' * 32, self.project))
            db.execute("INSERT INTO linked_chat_topics VALUES (-100, 12, 'Edit')")
        links = ChatLinks(self.registry, self.tokens, lambda page: ' Big Show ' if page == 'a' * 32 else None)
        preview = links.write('link-chat', {'project': self.project, 'chat': -100, 'topic': 12}, actor=self.actor, idempotency_key='labelled')
        assert isinstance(preview, Preview)
        with self.registry._transaction() as db:
            target = db.execute('SELECT target FROM approvals WHERE id = ?', (preview.plan['approval_id'],)).fetchone()[0]
        self.assertEqual(target, f'Big Show [ABC123] {self.project}: chat -100, topic 12 (Edit)')

    def test_approval_still_made_when_the_name_cannot_be_read(self) -> None:
        def broken(page: str) -> str | None:
            raise OSError('Notion is down')
        with self.registry._transaction() as db:
            db.execute("INSERT INTO bindings (system, page_id, project_id, bound_at, active) VALUES ('notion', ?, ?, 'now', 1)", ('a' * 32, self.project))
        preview = ChatLinks(self.registry, self.tokens, broken).write('link-chat', {'project': self.project, 'chat': -100, 'topic': None}, actor=self.actor, idempotency_key='broken')
        assert isinstance(preview, Preview)
        with self.registry._transaction() as db:
            target = db.execute('SELECT target FROM approvals WHERE id = ?', (preview.plan['approval_id'],)).fetchone()[0]
        self.assertEqual(target, f'{self.project}: chat -100, topic none')

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


class ChatLinkReminderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = [1_700_000_000.0]
        self.registry = MemoryRegistry()
        self.addCleanup(self.registry.db.close)
        self.registry._clock = lambda: self.now[0]
        self.actor = 't6n_' + '1' * 32
        with self.registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES (?, 'staff', '[]', '[]', 42, 0, 1)", (self.actor,))
            db.execute("INSERT INTO person_permissions VALUES (?, 'operator')", (self.actor,))
            db.execute("INSERT INTO bindings (system, page_id, project_id, bound_at, active) VALUES ('notion', ?, ?, 1.0, 1)", ('b' * 32, self.actor))
        self.registry._new_id = lambda: 't6p_' + '1' * 24
        project = self.registry.register(actor=self.actor, idempotency_key='register')
        assert isinstance(project, Registered)
        self.project = project.project.id
        with self.registry._transaction() as db:
            db.execute("UPDATE projects SET code = 'ABC123', code_form = 'abc123', rotation = 'in' WHERE id = ?", (self.project,))
            db.execute("INSERT INTO bindings (system, page_id, project_id, bound_at, active) VALUES ('notion', ?, ?, 1.0, 1)", ('a' * 32, self.project))
            db.execute("INSERT INTO linked_chat_topics VALUES (-100, 12, 'Edit')")
        self.tokens = PreviewTokens('test')
        self.approvals = Approvals(self.registry, self.tokens)
        self.links = ChatLinks(self.registry, self.tokens, lambda page: 'Big Show' if page == 'a' * 32 else None)

    def _runner(self, person_name: Any = None, operators: tuple[int, ...] = (900,)) -> tuple[Any, Any, Any]:
        from test_abang import Gateway
        from t6.worker.gateway import GatewayClient
        from t6.worker.internal_queue import InternalJobQueue, InternalQueueClient
        from t6.worker.linked_chats import LinkedChats
        from t6.worker.runner import Worker

        gateway = Gateway()
        linked = LinkedChats(self.registry, GatewayClient(gateway, 'secret'), operators, person_name=person_name)
        queue = InternalJobQueue(self.registry, clock=lambda: self.now[0])
        worker = Worker(InternalQueueClient(queue), 'main', {'chat_link_reminder': linked.remind_chat_link})
        return gateway, queue, worker

    def test_reminder_sent_once_after_24h_and_approval_stays_requested(self) -> None:
        from t6.chat_link import CHAT_LINK_REMINDER_SECONDS

        preview = self.links.write('link-chat', {'project': self.project, 'chat': -100, 'topic': 12}, actor=self.actor, idempotency_key='ask')
        assert isinstance(preview, Preview)
        approval_id = preview.plan['approval_id']
        gateway, _, worker = self._runner(person_name=lambda page: 'Jane Doe' if page == 'b' * 32 else None)

        self.now[0] += CHAT_LINK_REMINDER_SECONDS - 1
        self.assertFalse(worker.run_once())
        self.assertEqual(gateway.calls, [])

        self.now[0] += 1
        self.assertTrue(worker.run_once())
        self.assertFalse(worker.run_once())
        notifies = [call for call in gateway.calls if call[1] == '/notify']
        self.assertEqual(len(notifies), 1)
        sent = notifies[0][2]
        self.assertEqual(sent['to'], 900)
        self.assertEqual(sent['buttons'], [])
        self.assertEqual(sent['idempotency_key'], f'chat-link:reminder:{approval_id}:900')
        self.assertEqual(
            sent['text'],
            f'<b>Reminder: chat link approval waiting 24h</b>\n'
            f'Big Show [ABC123] {self.project}: chat -100, topic 12 (Edit)\n'
            f'Requested by: Jane Doe ({self.actor})\n'
            f'Approval: {approval_id}',
        )

        self.now[0] += CHAT_LINK_REMINDER_SECONDS * 8
        self.assertFalse(worker.run_once())
        self.assertEqual(len([call for call in gateway.calls if call[1] == '/notify']), 1)
        with self.registry._transaction() as db:
            self.assertEqual(db.execute('SELECT state FROM approvals WHERE id = ?', (approval_id,)).fetchone(), ('requested',))
        grant = self.approvals.write('grant', {'approval': approval_id}, actor=self.actor, idempotency_key='grant-late')
        assert isinstance(grant, Preview)
        self.assertIsInstance(self.approvals.write('grant', {'approval': approval_id}, actor=self.actor, idempotency_key='grant-late', confirm=grant.token), Done)

    def test_no_reminder_when_granted_before_24h(self) -> None:
        from t6.chat_link import CHAT_LINK_REMINDER_SECONDS

        preview = self.links.write('link-chat', {'project': self.project, 'chat': -100, 'topic': 12}, actor=self.actor, idempotency_key='ask')
        assert isinstance(preview, Preview)
        approval_id = preview.plan['approval_id']
        self.now[0] += 3600
        grant = self.approvals.write('grant', {'approval': approval_id}, actor=self.actor, idempotency_key='grant')
        assert isinstance(grant, Preview)
        self.assertIsInstance(self.approvals.write('grant', {'approval': approval_id}, actor=self.actor, idempotency_key='grant', confirm=grant.token), Done)

        gateway, queue, worker = self._runner()
        self.now[0] += CHAT_LINK_REMINDER_SECONDS
        self.assertTrue(worker.run_once())
        self.assertEqual(gateway.calls, [])
        jobs = queue.find('chat_link_reminder')
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].state, 'done')

    def test_no_reminder_when_denied_before_24h(self) -> None:
        from t6.chat_link import CHAT_LINK_REMINDER_SECONDS

        preview = self.links.write('link-chat', {'project': self.project, 'chat': -100, 'topic': 12}, actor=self.actor, idempotency_key='ask')
        assert isinstance(preview, Preview)
        approval_id = preview.plan['approval_id']
        self.now[0] += 3600
        deny = self.approvals.write('deny', {'approval': approval_id, 'reason': 'not now'}, actor=self.actor, idempotency_key='deny')
        assert isinstance(deny, Preview)
        self.assertIsInstance(self.approvals.write('deny', {'approval': approval_id, 'reason': 'not now'}, actor=self.actor, idempotency_key='deny', confirm=deny.token), Done)

        gateway, queue, worker = self._runner()
        self.now[0] += CHAT_LINK_REMINDER_SECONDS
        self.assertTrue(worker.run_once())
        self.assertEqual(gateway.calls, [])
        jobs = queue.find('chat_link_reminder')
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].state, 'done')

    def test_reused_open_approval_does_not_queue_second_reminder(self) -> None:
        first = self.links.write('link-chat', {'project': self.project, 'chat': -100, 'topic': 12}, actor=self.actor, idempotency_key='k1')
        second = self.links.write('link-chat', {'project': self.project, 'chat': -100, 'topic': 12}, actor=self.actor, idempotency_key='k2')
        assert isinstance(first, Preview) and isinstance(second, Preview)
        self.assertEqual(first.plan['approval_id'], second.plan['approval_id'])
        with self.registry._transaction() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM internal_jobs WHERE kind = 'chat_link_reminder'").fetchone()[0], 1)

    def test_reminder_falls_back_when_person_name_unavailable_and_escapes_html(self) -> None:
        from t6.chat_link import CHAT_LINK_REMINDER_SECONDS

        with self.registry._transaction() as db:
            db.execute("INSERT INTO linked_chat_topics VALUES (-100, 15, '<Edit & Cut>')")
        links = ChatLinks(self.registry, self.tokens, lambda page: 'Show & <Co>' if page == 'a' * 32 else None)
        preview = links.write('link-chat', {'project': self.project, 'chat': -100, 'topic': 15}, actor=self.actor, idempotency_key='escape')
        assert isinstance(preview, Preview)
        approval_id = preview.plan['approval_id']

        def broken(_page: str) -> str | None:
            raise OSError('Notion down')

        gateway, _, worker = self._runner(person_name=broken)
        self.now[0] += CHAT_LINK_REMINDER_SECONDS
        self.assertTrue(worker.run_once())
        text = gateway.calls[0][2]['text']
        self.assertIn(f'Show &amp; &lt;Co&gt; [ABC123] {self.project}: chat -100, topic 15 (&lt;Edit &amp; Cut&gt;)', text)
        self.assertIn(f'Requested by: {self.actor}', text)
        self.assertIn(f'Approval: {approval_id}', text)

    def test_worker_retry_idempotency(self) -> None:
        from t6.chat_link import CHAT_LINK_REMINDER_SECONDS
        from t6.worker.gateway import GatewayClient, GatewayRejected
        from t6.worker.internal_queue import InternalJobQueue, InternalQueueClient
        from t6.worker.linked_chats import LinkedChats
        from t6.worker.runner import Worker

        class FlakyGateway:
            def __init__(self) -> None:
                self.keys: list[str] = []
                self.fail_next = True

            def send(self, method: str, path: str, headers: Any, body: bytes) -> tuple[int, dict[str, Any]]:
                data = json.loads(body)
                self.keys.append(data['idempotency_key'])
                if self.fail_next:
                    self.fail_next = False
                    return 503, {'detail': 'down'}
                return 202, {'message_ref': 1}

        preview = self.links.write('link-chat', {'project': self.project, 'chat': -100, 'topic': 12}, actor=self.actor, idempotency_key='retry')
        assert isinstance(preview, Preview)
        approval_id = preview.plan['approval_id']
        gateway = FlakyGateway()
        linked = LinkedChats(self.registry, GatewayClient(gateway, 'secret'), (900,))
        queue = InternalJobQueue(self.registry, clock=lambda: self.now[0])
        worker = Worker(InternalQueueClient(queue), 'main', {'chat_link_reminder': linked.remind_chat_link})

        self.now[0] += CHAT_LINK_REMINDER_SECONDS
        self.assertTrue(worker.run_once())
        self.assertEqual(queue.find('chat_link_reminder')[0].state, 'queued')
        self.now[0] += 60
        self.assertTrue(worker.run_once())
        self.assertEqual(queue.find('chat_link_reminder')[0].state, 'done')
        self.assertEqual(gateway.keys, [f'chat-link:reminder:{approval_id}:900', f'chat-link:reminder:{approval_id}:900'])
        with self.assertRaises(GatewayRejected):
            GatewayClient(FlakyGateway(), 'secret').notify(900, 'x', [], 'k')

    def test_reminder_without_operators_fails_instead_of_dropping(self) -> None:
        from t6.chat_link import CHAT_LINK_REMINDER_SECONDS

        preview = self.links.write('link-chat', {'project': self.project, 'chat': -100, 'topic': 12}, actor=self.actor, idempotency_key='no-ops')
        assert isinstance(preview, Preview)
        gateway, queue, worker = self._runner(operators=())
        self.now[0] += CHAT_LINK_REMINDER_SECONDS
        self.assertTrue(worker.run_once())
        self.assertEqual(gateway.calls, [])
        job = queue.find('chat_link_reminder')[0]
        self.assertEqual(job.attempts, 1)
        self.assertEqual(job.state, 'queued')
        self.assertIn('T6_OPERATOR_IDS', str(job.result))

