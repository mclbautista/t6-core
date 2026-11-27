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

    def test_project_label_from_target_and_format_decision_notice(self) -> None:
        from t6.chat_link import format_decision_notice, project_label_from_target

        pid = 't6p_' + '1' * 24
        self.assertEqual(project_label_from_target(f'Big Show [ABC123] {pid}: chat -100, topic 12 (Edit)'), 'Big Show [ABC123]')
        self.assertEqual(project_label_from_target(f'Big Show {pid}: chat -100, topic none'), 'Big Show')
        self.assertEqual(project_label_from_target(f'[ABC123] {pid}: chat -100, topic 12'), '[ABC123]')
        self.assertEqual(project_label_from_target(f'{pid}: chat -100, topic 12'), pid)

        self.assertEqual(format_decision_notice('link-chat', 'grant', 'Big Show [ABC123]'), 'Linked to Big Show [ABC123]')
        self.assertEqual(format_decision_notice('unlink-chat', 'grant', 'Big Show [ABC123]'), 'Unlinked from Big Show [ABC123]')
        self.assertEqual(
            format_decision_notice('link-chat', 'deny', 'Big Show [ABC123]', denier='Ada', reason='Wrong topic'),
            'Link to Big Show [ABC123] was not approved by Ada: Wrong topic',
        )
        self.assertEqual(
            format_decision_notice('unlink-chat', 'deny', 'Big Show [ABC123]', denier='Ada', reason=None),
            'Unlink from Big Show [ABC123] was not approved by Ada',
        )
        self.assertEqual(
            format_decision_notice('link-chat', 'deny', pid, denier=self.actor, reason='  '),
            f'Link to {pid} was not approved by {self.actor}',
        )
        self.assertEqual(
            format_decision_notice('link-chat', 'deny', 'A & <B> [X]', denier='Ada <Admin>', reason='a < b & c > d'),
            'Link to A &amp; &lt;B&gt; [X] was not approved by Ada &lt;Admin&gt;: a &lt; b &amp; c &gt; d',
        )

    def test_grant_link_and_unlink_send_decision_notices_and_track_topic(self) -> None:
        from test_abang import Gateway
        from t6.worker import Worker
        from t6.worker.gateway import GatewayClient
        from t6.worker.internal_queue import InternalJobQueue, InternalQueueClient
        from t6.worker.linked_chats import LinkedChats

        with self.registry._transaction() as db:
            db.execute("UPDATE projects SET code = 'ABC123', code_form = 'abc123', rotation = 'in' WHERE id = ?", (self.project,))
            db.execute("INSERT INTO bindings (system, page_id, project_id, bound_at, active) VALUES ('notion', ?, ?, 'now', 1)", ('a' * 32, self.project))
        self.links = ChatLinks(self.registry, self.tokens, lambda page: 'Big Show' if page == 'a' * 32 else None)
        gateway = Gateway()
        worker = LinkedChats(self.registry, GatewayClient(gateway, 'secret'))
        runner = Worker(InternalQueueClient(InternalJobQueue(self.registry)), 'main', {'chat_link_notice': worker.chat_link_notice})

        values = {'project': self.project, 'chat': -100, 'topic': 12}
        linked = self.approved('link-chat', values, 'grant-link')
        link_approval = linked.result['approval_id']
        with self.registry._transaction() as db:
            jobs = db.execute("SELECT kind, dedupe_key FROM internal_jobs WHERE kind = 'chat_link_notice'").fetchall()
        self.assertEqual(jobs, [('chat_link_notice', f'chat_link_notice:{link_approval}')])
        while runner.run_once():
            pass
        notifies = [call[2] for call in gateway.calls if call[1] == '/notify']
        self.assertEqual(len(notifies), 1)
        self.assertEqual(
            notifies[0],
            {
                'to': -100,
                'topic': 12,
                'text': 'Linked to Big Show [ABC123]',
                'format': 'html',
                'buttons': [],
                'idempotency_key': f'chat-link-decision:{link_approval}',
            },
        )
        with self.registry._transaction() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM linked_chat_sends WHERE chat_id = -100 AND topic_id = 12').fetchone()[0], 1)

        unlinked = self.approved('unlink-chat', values, 'grant-unlink')
        unlink_approval = unlinked.result['approval_id']
        while runner.run_once():
            pass
        notifies = [call[2] for call in gateway.calls if call[1] == '/notify']
        self.assertEqual(len(notifies), 2)
        self.assertEqual(
            notifies[1],
            {
                'to': -100,
                'topic': 12,
                'text': 'Unlinked from Big Show [ABC123]',
                'format': 'html',
                'buttons': [],
                'idempotency_key': f'chat-link-decision:{unlink_approval}',
            },
        )
        with self.registry._transaction() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM linked_chat_sends WHERE chat_id = -100 AND topic_id = 12').fetchone()[0], 1)

        none_topic = self.approved('link-chat', {'project': self.project, 'chat': -200, 'topic': None}, 'grant-none')
        while runner.run_once():
            pass
        notifies = [call[2] for call in gateway.calls if call[1] == '/notify']
        self.assertEqual(len(notifies), 3)
        self.assertNotIn('topic', notifies[2])
        self.assertEqual(notifies[2]['to'], -200)
        self.assertEqual(notifies[2]['idempotency_key'], f'chat-link-decision:{none_topic.result["approval_id"]}')

    def test_deny_link_and_unlink_send_notice_with_person_name_and_fallbacks(self) -> None:
        from test_abang import Gateway
        from t6.worker import Worker
        from t6.worker.client import WorkerJob
        from t6.worker.gateway import GatewayClient
        from t6.worker.internal_queue import InternalJobQueue, InternalQueueClient
        from t6.worker.linked_chats import LinkedChats

        with self.registry._transaction() as db:
            db.execute("UPDATE projects SET code = 'ABC123', code_form = 'abc123', rotation = 'in' WHERE id = ?", (self.project,))
            db.execute("INSERT INTO bindings (system, page_id, project_id, bound_at, active) VALUES ('notion', ?, ?, 'now', 1)", ('a' * 32, self.project))
            db.execute("INSERT INTO bindings (system, page_id, project_id, bound_at, active) VALUES ('notion', ?, ?, 'now', 1)", ('b' * 32, self.actor))
        self.links = ChatLinks(self.registry, self.tokens, lambda page: 'Big Show' if page == 'a' * 32 else None)

        values = {'project': self.project, 'chat': -100, 'topic': 12}
        preview = self.links.write('link-chat', values, actor=self.actor, idempotency_key='deny-link')
        assert isinstance(preview, Preview)
        deny = {'approval': preview.plan['approval_id'], 'reason': 'Wrong room'}
        d_prev = self.approvals.write('deny', deny, actor=self.actor, idempotency_key='d1')
        assert isinstance(d_prev, Preview)
        self.assertIsInstance(self.approvals.write('deny', deny, actor=self.actor, idempotency_key='d1', confirm=d_prev.token), Done)

        gateway = Gateway()
        worker = LinkedChats(self.registry, GatewayClient(gateway, 'secret'), person_name=lambda page: ' Ada Lovelace ' if page == 'b' * 32 else None)
        runner = Worker(InternalQueueClient(InternalJobQueue(self.registry)), 'main', {'chat_link_notice': worker.chat_link_notice})
        while runner.run_once():
            pass
        notifies = [call[2] for call in gateway.calls if call[1] == '/notify']
        self.assertEqual(notifies[0]['text'], 'Link to Big Show [ABC123] was not approved by Ada Lovelace: Wrong room')
        self.assertEqual(notifies[0]['topic'], 12)

        again = self.links.write('link-chat', values, actor=self.actor, idempotency_key='retry-after-deny')
        assert isinstance(again, Preview)
        self.assertNotEqual(again.plan['approval_id'], preview.plan['approval_id'])

        with self.registry._transaction() as db:
            db.execute("UPDATE projects SET code = NULL, code_form = NULL, rotation = 'out' WHERE id = ?", (self.project,))
            db.execute("DELETE FROM bindings WHERE project_id = ?", (self.project,))
            db.execute("INSERT INTO chat_links VALUES ('t6h_1', ?, -300, 7, 'active')", (self.project,))
        plain_links = ChatLinks(self.registry, self.tokens)
        unlink_prev = plain_links.write('unlink-chat', {'project': self.project, 'chat': -300, 'topic': 7}, actor=self.actor, idempotency_key='deny-unlink')
        assert isinstance(unlink_prev, Preview)
        deny_u = {'approval': unlink_prev.plan['approval_id'], 'reason': 'Keep linked'}
        du_prev = self.approvals.write('deny', deny_u, actor=self.actor, idempotency_key='d2')
        assert isinstance(du_prev, Preview)
        self.assertIsInstance(self.approvals.write('deny', deny_u, actor=self.actor, idempotency_key='d2', confirm=du_prev.token), Done)

        def broken_person(page: str) -> str | None:
            raise OSError('Notion down')

        fallback_worker = LinkedChats(self.registry, GatewayClient(gateway, 'secret'), person_name=broken_person)
        fallback_runner = Worker(InternalQueueClient(InternalJobQueue(self.registry)), 'main', {'chat_link_notice': fallback_worker.chat_link_notice})
        while fallback_runner.run_once():
            pass
        notifies = [call[2] for call in gateway.calls if call[1] == '/notify']
        self.assertEqual(notifies[1]['text'], f'Unlink from {self.project} was not approved by {self.actor}: Keep linked')

        self.assertTrue(
            fallback_worker.chat_link_notice(
                WorkerJob(
                    99,
                    'main',
                    'chat_link_notice',
                    'chat_link_notice:t6a_noreason',
                    {
                        'approval_id': 't6a_noreason',
                        'action': 'unlink-chat',
                        'decision': 'deny',
                        'chat': -300,
                        'topic': None,
                        'label': self.project,
                        'decided_by': self.actor,
                        'reason': None,
                    },
                    'tok',
                    1,
                )
            )
        )
        notifies = [call[2] for call in gateway.calls if call[1] == '/notify']
        self.assertEqual(notifies[2]['text'], f'Unlink from {self.project} was not approved by {self.actor}')

    def test_decision_notice_is_idempotent_across_retries(self) -> None:
        from test_abang import Gateway
        from t6.chat_link import decided
        from t6.worker.client import WorkerJob
        from t6.worker.gateway import GatewayClient
        from t6.worker.linked_chats import LinkedChats

        values = {'project': self.project, 'chat': -100, 'topic': 12}
        done = self.approved('link-chat', values, 'idem')
        approval_id = done.result['approval_id']
        with self.registry._transaction() as db:
            decided(self.registry, db, approval_id, 'grant', self.actor, None, 123.0)
            rows = db.execute("SELECT id, payload FROM internal_jobs WHERE kind = 'chat_link_notice'").fetchall()
        self.assertEqual(len(rows), 1)
        job_id, raw_payload = rows[0]
        gateway = Gateway()
        worker = LinkedChats(self.registry, GatewayClient(gateway, 'secret'))
        job = WorkerJob(job_id, 'main', 'chat_link_notice', f'chat_link_notice:{approval_id}', json.loads(raw_payload), 'tok', 1)
        self.assertTrue(worker.chat_link_notice(job))
        self.assertTrue(worker.chat_link_notice(job))
        self.assertEqual(len(gateway.keys), 1)
        self.assertIn(f'chat-link-decision:{approval_id}', gateway.keys)


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


class ProjectLinksVerbTests(unittest.TestCase):
    def setUp(self) -> None:
        import os
        import tempfile
        import threading
        from t6.notion.testing import FakeNotion, project_properties
        from t6.privacy import Registry as PrivacyRegistry
        from t6.registry import Rotation
        from t6.registry.sqlite import SqliteProjectRegistry
        from t6.server.http import make_server
        from t6.server.queue import JobQueue
        from t6.server.verbs import VerbAPI

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.registry = SqliteProjectRegistry(self.tmp.name + '/registry')
        self.queue = JobQueue(self.tmp.name + '/queue')
        self.tokens = PreviewTokens('test')
        self.operator = 't6n_' + '1' * 32
        self.admin = 't6n_' + '2' * 32
        self.staff = 't6n_' + '3' * 32
        self.coordinator = 't6n_' + '4' * 32
        self.inactive_op = 't6n_' + '5' * 32
        with self.registry._transaction() as db:
            for pid, active in ((self.operator, 1), (self.admin, 1), (self.staff, 1), (self.coordinator, 1), (self.inactive_op, 0)):
                db.execute("INSERT INTO persons VALUES (?, 'staff', '[]', '[]', NULL, 0, ?)", (pid, active))
            db.execute("INSERT INTO person_permissions VALUES (?, 'operator')", (self.operator,))
            db.execute("INSERT INTO person_permissions VALUES (?, 'admin')", (self.admin,))
            db.execute("INSERT INTO person_permissions VALUES (?, 'coordinator')", (self.coordinator,))
            db.execute("INSERT INTO person_permissions VALUES (?, 'operator')", (self.inactive_op,))

        self.fake = FakeNotion()
        self.fake.add_snapshot_data_source('projects', 'projects')
        self.page_id = str(self.fake.add_page('projects', project_properties(name='  Big Show  ', project_type='Feature Film'))['id'])

        self.registry._new_id = lambda: 't6p_' + 'a' * 24
        reg = self.registry.register(actor=self.operator, idempotency_key='p1', notion_page_id=self.page_id, code='ABC-123', rotation=Rotation.IN)
        assert isinstance(reg, Registered)
        self.project_id = reg.project.id

        self.api = VerbAPI(
            People(self.registry, self.tokens),
            self.queue,
            {
                'operator-secret': self.operator,
                'admin-secret': self.admin,
                'staff-secret': self.staff,
                'coord-secret': self.coordinator,
                'inactive-secret': self.inactive_op,
                'mcp-secret': 'mcp',
                'gateway-secret': 'gateway',
            },
            lambda _: self.fake.client(),
            privacy_registry=PrivacyRegistry(),
        )
        self.server = make_server(('127.0.0.1', 0), self.api)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join)
        self.addCleanup(self.server.shutdown)
        self.base_url = f'http://127.0.0.1:{self.server.server_port}'
        self.env = patch.dict(os.environ, {'T6_NOTION_TOKEN': 'fake'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_full_links_card_by_normalized_code_and_id(self) -> None:
        from t6.client import Client
        from t6.verbs import ProjectLinksInput, ProjectLinksOutput

        with self.registry._transaction() as db:
            db.execute("INSERT INTO media_locations VALUES ('t6m_1', ?, ?, 'frameio-project', 'fp-uuid-1', 'frameio-project', 1, NULL)", (self.project_id, self.project_id))
            db.execute("INSERT INTO chat_links VALUES ('t6h_1', ?, -1001, 12, 'active')", (self.project_id,))
            db.execute("INSERT INTO chat_links VALUES ('t6h_2', ?, -1001, NULL, 'active')", (self.project_id,))
            db.execute("INSERT INTO chat_links VALUES ('t6h_3', ?, -1002, 7, 'inactive')", (self.project_id,))
            db.execute("INSERT INTO chat_links VALUES ('t6h_4', ?, -1001, 99, 'active')", (self.project_id,))
            db.execute("INSERT INTO linked_chat_topics VALUES (-1001, 12, 'ABC-123 - Edit')")

        client = Client(self.base_url, 'operator-secret')
        by_code = client.project_links(ProjectLinksInput(project=' abc 123 '))
        assert isinstance(by_code, Done)
        parsed = ProjectLinksOutput.model_validate(by_code.result)
        self.assertEqual(parsed.project_id, self.project_id)
        self.assertEqual(parsed.name, 'Big Show')
        self.assertEqual(parsed.code, 'ABC-123')
        self.assertEqual(parsed.notion_url, f"https://www.notion.so/{self.page_id.replace('-', '')}")
        self.assertEqual(parsed.frameio_url, 'https://next.frame.io/project/fp-uuid-1')
        self.assertEqual(
            [item.model_dump(include={'id', 'chat', 'topic', 'topic_name', 'state'}) for item in parsed.chat_links],
            [
                {'id': 't6h_2', 'chat': -1001, 'topic': None, 'topic_name': None, 'state': 'active'},
                {'id': 't6h_1', 'chat': -1001, 'topic': 12, 'topic_name': 'ABC-123 - Edit', 'state': 'active'},
                {'id': 't6h_4', 'chat': -1001, 'topic': 99, 'topic_name': None, 'state': 'active'},
                {'id': 't6h_3', 'chat': -1002, 'topic': 7, 'topic_name': None, 'state': 'inactive'},
            ],
        )

        by_id = client.project_links(ProjectLinksInput(project=self.project_id))
        self.assertEqual(by_id, by_code)

    def test_out_of_rotation_and_unknown_projects(self) -> None:
        from t6.client import Client
        from t6.registry import Rotation
        from t6.verbs import ProjectLinksInput

        self.registry._new_id = lambda: 't6p_' + 'b' * 24
        out_reg = self.registry.register(actor=self.operator, idempotency_key='p2', code='OLD99', rotation=Rotation.OUT)
        assert isinstance(out_reg, Registered)

        client = Client(self.base_url, 'operator-secret')
        by_id = client.project_links(ProjectLinksInput(project=out_reg.project.id))
        assert isinstance(by_id, Done)
        self.assertEqual(by_id.result['project_id'], out_reg.project.id)
        self.assertEqual(by_id.result['code'], 'OLD99')
        self.assertIsNone(by_id.result['notion_url'])
        self.assertIsNone(by_id.result['frameio_url'])
        self.assertEqual(by_id.result['chat_links'], [])

        for bad in ('OLD99', 'NOPE', 't6p_' + 'f' * 24, '', '   ', '---'):
            with self.subTest(bad=bad):
                res = client.project_links(ProjectLinksInput(project=bad))
                assert isinstance(res, Refused)
                self.assertEqual(res.rule, 'project')

    def test_missing_links_and_broken_notion_degrade_cleanly(self) -> None:
        from t6.client import Client
        from t6.notion import NotionError
        from t6.notion.source import NotionSource
        from t6.registry import Rotation
        from t6.verbs import ProjectLinksInput

        self.registry._new_id = lambda: 't6p_' + 'c' * 24
        bare = self.registry.register(actor=self.operator, idempotency_key='p3', code='BARE1', rotation=Rotation.IN)
        assert isinstance(bare, Registered)
        with self.registry._transaction() as db:
            db.execute("INSERT INTO bindings (system, page_id, project_id, bound_at, active) VALUES ('notion', 'deadbeef', ?, 1, 0)", (bare.project.id,))
            db.execute("INSERT INTO media_locations VALUES ('t6m_off', ?, ?, 'frameio-project', 'fp-off', 'frameio-project', 0, NULL)", (bare.project.id, bare.project.id))

        client = Client(self.base_url, 'operator-secret')
        bare_res = client.project_links(ProjectLinksInput(project='BARE1'))
        assert isinstance(bare_res, Done)
        self.assertEqual(
            bare_res.result,
            {'project_id': bare.project.id, 'name': None, 'code': 'BARE1', 'notion_url': None, 'frameio_url': None, 'chat_links': []},
        )

        class BrokenNotion:
            def retrieve_page(self, page_id: str) -> dict[str, Any]:
                raise NotionError(503, 'service_unavailable', 'Notion is down')

        old_source = self.api.notion_source
        self.api.notion_source = NotionSource(lambda: BrokenNotion())  # type: ignore[arg-type, return-value]
        try:
            broken_res = client.project_links(ProjectLinksInput(project=self.project_id))
            assert isinstance(broken_res, Done)
            self.assertIsNone(broken_res.result['name'])
            self.assertEqual(broken_res.result['notion_url'], f"https://www.notion.so/{self.page_id.replace('-', '')}")
        finally:
            self.api.notion_source = old_source

    def test_crm_links_by_kind_from_the_crm_log(self) -> None:
        from t6.client import Client
        from t6.verbs import ProjectCrmLinksInput

        class Sheet:
            workbook = 'synthetic-crm'

            def tabs(self) -> dict[str, tuple[tuple[Any, ...], ...]]:
                header = ('Date', 'Project', 'Kind', 'Note', 'Link', 'By', 'Remind on', 'Done')
                return {'Log': (header,
                                (46000, f'Big Show \u00b7 {self_id}', 'quote', None, 'https://example.invalid/q', None, None, None),
                                (46001, f'Big Show \u00b7 {self_id}', 'billing', None, 'https://example.invalid/b', None, None, None),
                                (46002, 'Someone Else \u00b7 t6p_' + 'e' * 24, 'quote', None, 'https://example.invalid/x', None, None, None))}

        self_id = self.project_id
        client = Client(self.base_url, 'operator-secret')
        with patch('t6.crm.config.crm_source', lambda environ: Sheet()):
            by_code = client.project_crm_links(ProjectCrmLinksInput(project='abc 123'))
            missing = client.project_crm_links(ProjectCrmLinksInput(project='NOPE'))
        assert isinstance(by_code, Done)
        self.assertEqual(by_code.result, {'project_id': self.project_id, 'links': {
            'quote': ['https://example.invalid/q'], 'billing': ['https://example.invalid/b'], 'gmail_thread': []}})
        assert isinstance(missing, Refused)
        self.assertEqual(missing.rule, 'project')
        with patch.dict('os.environ', {}, clear=False), patch('t6.crm.config.crm_source', lambda environ: None):
            off = client.project_crm_links(ProjectCrmLinksInput(project='abc 123'))
        assert isinstance(off, Refused)
        self.assertEqual((off.rule, off.reason), ('config', 'Set T6_CRM_SHEET_ID'))

    def test_crm_links_reports_a_sheet_read_failure(self) -> None:
        from t6.client import Client
        from t6.verbs import ProjectCrmLinksInput

        def broken(environ: Any) -> Any:
            raise ValueError('Google Sheets read failed (503)')

        with patch('t6.crm.config.crm_source', broken):
            res = Client(self.base_url, 'operator-secret').project_crm_links(ProjectCrmLinksInput(project='abc 123'))
        assert isinstance(res, Refused)
        self.assertEqual((res.rule, res.reason), ('crm_sheet', 'Google Sheets read failed (503)'))

    # --- link states (#689) ---

    def _request(self, verb: str, chat: int, topic: int | None, key: str, actor: str | None = None) -> Preview:
        links = ChatLinks(self.registry, self.tokens)
        preview = links.write(verb, {'project': self.project_id, 'chat': chat, 'topic': topic}, actor=actor or self.operator, idempotency_key=key)
        assert isinstance(preview, Preview)
        return preview

    def _decide(self, preview: Preview, verb: str, key: str, reason: str | None = None) -> None:
        approvals = Approvals(self.registry, self.tokens)
        values = {'approval': preview.plan['approval_id']} | ({'reason': reason or 'no'} if verb == 'deny' else {})
        step = approvals.write(verb, values, actor=self.operator, idempotency_key=key)
        assert isinstance(step, Preview)
        self.assertIsInstance(approvals.write(verb, values, actor=self.operator, idempotency_key=key, confirm=step.token), Done)

    def _confirm(self, preview: Preview, verb: str, chat: int, topic: int | None, key: str) -> None:
        done = ChatLinks(self.registry, self.tokens).write(verb, {'project': self.project_id, 'chat': chat, 'topic': topic}, actor=self.operator, idempotency_key=key, confirm=preview.token)
        self.assertIsInstance(done, Done)

    def _linked(self, chat: int, topic: int | None, key: str) -> None:
        preview = self._request('link-chat', chat, topic, key)
        self._decide(preview, 'grant', key + '-g')
        self._confirm(preview, 'link-chat', chat, topic, key)

    def _states(self) -> dict[tuple[int, int | None], dict[str, Any]]:
        from t6.client import Client
        from t6.verbs import ProjectLinksInput
        res = Client(self.base_url, 'operator-secret').project_links(ProjectLinksInput(project=self.project_id))
        assert isinstance(res, Done)
        return {(item['chat'], item['topic']): item for item in res.result['chat_links']}

    def test_every_link_state_is_returned(self) -> None:
        clock = [1_700_000_000.0]
        self.registry._clock = lambda: clock[0]
        self._linked(-1, 1, 'active')
        self._linked(-2, 2, 'gone')
        self._confirm(self._unlink_approved(-2, 2, 'gone-u'), 'unlink-chat', -2, 2, 'gone-u')
        self._linked(-3, 3, 'dead')
        with self.registry._transaction() as db:
            link = db.execute('SELECT id FROM chat_links WHERE chat_id = -3').fetchone()[0]
            db.execute("UPDATE chat_links SET state = 'inactive' WHERE id = ?", (link,))
            # 18:00 UTC on 23 Nov is already 24 Nov in Manila
            db.execute("INSERT INTO chat_link_history (link_id, state, actor, at) VALUES (?, 'inactive', 'gateway', ?)", (link, 1_763_920_800.0))
            db.execute("INSERT INTO linked_chat_topics VALUES (-4, 4, 'ABC-123 - Edit')")
        waiting = self._request('link-chat', -4, 4, 'waiting')
        denied = self._request('link-chat', -5, 5, 'denied')
        self._decide(denied, 'deny', 'denied-d', 'wrong show')
        got = self._states()
        self.assertEqual({key: item['state'] for key, item in got.items()},
                         {(-1, 1): 'active', (-2, 2): 'inactive', (-3, 3): 'dead', (-4, 4): 'waiting', (-5, 5): 'denied'})
        self.assertIsNone(got[(-2, 2)]['dead_since'])
        self.assertEqual(got[(-3, 3)]['dead_since'], '2025-11-24')
        self.assertEqual((got[(-4, 4)]['approval_id'], got[(-4, 4)]['requested_by'], got[(-4, 4)]['topic_name'], got[(-4, 4)]['id']), (waiting.plan['approval_id'], self.operator, 'ABC-123 - Edit', None))
        self.assertEqual((got[(-5, 5)]['approval_id'], got[(-5, 5)]['decided_by'], got[(-5, 5)]['reason']), (denied.plan['approval_id'], self.operator, 'wrong show'))
        self.assertIsNone(got[(-1, 1)]['approval_id'])

    def _unlink_approved(self, chat: int, topic: int | None, key: str) -> Preview:
        preview = self._request('unlink-chat', chat, topic, key)
        self._decide(preview, 'grant', key + '-g')
        return preview

    def test_denied_then_requested_again_shows_waiting(self) -> None:
        first = self._request('link-chat', -1, 1, 'a')
        self._decide(first, 'deny', 'a-d')
        self.assertEqual(self._states()[(-1, 1)]['state'], 'denied')
        again = self._request('link-chat', -1, 1, 'b', actor=self.staff)
        got = self._states()
        self.assertEqual(len(got), 1)
        self.assertEqual((got[(-1, 1)]['state'], got[(-1, 1)]['approval_id'], got[(-1, 1)]['requested_by']), ('waiting', again.plan['approval_id'], self.staff))

    def test_dead_link_with_a_new_request_shows_waiting_then_active_then_inactive(self) -> None:
        self._linked(-1, 1, 'l')
        with self.registry._transaction() as db:
            link = db.execute('SELECT id FROM chat_links').fetchone()[0]
            db.execute("UPDATE chat_links SET state = 'inactive' WHERE id = ?", (link,))
            db.execute("INSERT INTO chat_link_history (link_id, state, actor, at) VALUES (?, 'inactive', 'gateway', ?)", (link, self.registry._clock()))
        self.assertEqual(self._states()[(-1, 1)]['state'], 'dead')
        relink = self._request('link-chat', -1, 1, 'r')
        waiting = self._states()[(-1, 1)]
        self.assertEqual((waiting['state'], waiting['id'], waiting['dead_since']), ('waiting', link, None))
        self._decide(relink, 'grant', 'r-g')
        self._confirm(relink, 'link-chat', -1, 1, 'r')
        self.assertEqual(self._states()[(-1, 1)]['state'], 'active')
        self._confirm(self._unlink_approved(-1, 1, 'u'), 'unlink-chat', -1, 1, 'u')
        self.assertEqual(self._states()[(-1, 1)]['state'], 'inactive')

    def test_denied_then_granted_then_unlinked_is_inactive_not_denied(self) -> None:
        clock = [100.0]
        self.registry._clock = lambda: clock[0]
        self._decide(self._request('link-chat', -1, 1, 'a'), 'deny', 'a-d')
        clock[0] += 10
        self._linked(-1, 1, 'b')
        clock[0] += 10
        self._confirm(self._unlink_approved(-1, 1, 'u'), 'unlink-chat', -1, 1, 'u')
        self.assertEqual(self._states()[(-1, 1)]['state'], 'inactive')

    def test_active_link_stays_active_with_a_denied_unlink_or_an_older_denial(self) -> None:
        clock = [100.0]
        self.registry._clock = lambda: clock[0]
        self._decide(self._request('link-chat', -1, 1, 'a'), 'deny', 'a-d')
        clock[0] += 10
        self._linked(-1, 1, 'b')
        clock[0] += 10
        self._decide(self._request('unlink-chat', -1, 1, 'u'), 'deny', 'u-d')
        self.assertEqual(self._states()[(-1, 1)]['state'], 'active')

    def test_a_reused_preview_gives_one_waiting_entry_and_a_granted_unconfirmed_link_is_omitted(self) -> None:
        self._request('link-chat', -1, 1, 'a')
        self._request('link-chat', -1, 1, 'b')
        self.assertEqual([k for k in self._states()], [(-1, 1)])
        granted = self._request('link-chat', -2, 2, 'c')
        self._decide(granted, 'grant', 'c-g')
        self.assertNotIn((-2, 2), self._states())

    def test_operator_only_authorization(self) -> None:
        from t6.client import Client
        from t6.verbs import ProjectLinksInput

        for secret in ('staff-secret', 'coord-secret', 'inactive-secret', 'mcp-secret', 'gateway-secret'):
            with self.subTest(secret=secret):
                res = Client(self.base_url, secret).project_links(ProjectLinksInput(project=self.project_id))
                assert isinstance(res, Refused)
                self.assertEqual(res.rule, 'permission')

        for secret in ('operator-secret', 'admin-secret'):
            with self.subTest(secret=secret):
                res = Client(self.base_url, secret).project_links(ProjectLinksInput(project=self.project_id))
                self.assertIsInstance(res, Done)

    def test_cli_and_catalogue(self) -> None:
        import contextlib
        import io
        from t6.cli import main

        entry = next(v for v in catalogue() if v['noun'] == 'project' and v['verb'] == 'links')
        self.assertEqual(entry['role'], 'operator')
        self.assertTrue(entry['read_only'])

        with patch.dict('os.environ', {'T6_API_URL': self.base_url, 'T6_API_SECRET': 'operator-secret'}):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = main(['project', 'links', 'ABC-123'])
            self.assertEqual(code, 0)
            payload = json.loads(out.getvalue())
            self.assertEqual(payload['outcome'], 'done')
            self.assertEqual(payload['result']['project_id'], self.project_id)
            self.assertEqual(payload['result']['name'], 'Big Show')

            err_out = io.StringIO()
            with contextlib.redirect_stdout(err_out):
                bad_code = main(['project', 'links', 'UNKNOWN'])
            self.assertEqual(bad_code, 2)
            self.assertEqual(json.loads(err_out.getvalue())['rule'], 'project')

