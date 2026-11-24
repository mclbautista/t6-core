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
