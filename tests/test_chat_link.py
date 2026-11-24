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
            [item.model_dump() for item in parsed.chat_links],
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

