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
        with self.registry._transaction() as db:
            self.assertEqual([r[0] for r in db.execute('SELECT state FROM chat_link_history ORDER BY id')], ['active', 'inactive'])
        actions = [a.action for a in self.registry.activity(self.project)]
        self.assertEqual(actions.count('project_link_chat'), 1)
        self.assertEqual(actions.count('project_unlink_chat'), 1)
        self.assertEqual(len([a for a in self.registry.audit(self.project) if a.action.startswith('project_')]), 2)

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
