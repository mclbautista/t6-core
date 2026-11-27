"""`project links` asked by chat and topic (spec #685, gateway #132 and #134), through the verb API on a temporary registry."""
import functools
import os
import tempfile
import threading
import unittest
from typing import Any
from unittest.mock import patch

from t6.client import Client, Result
from t6.person import People
from t6.preview import PreviewTokens
from t6.privacy import Registry as PrivacyRegistry
from t6.registry import Registered, Rotation
from t6.registry.sqlite import SqliteProjectRegistry
from t6.server.http import make_server
from t6.server.queue import JobQueue
from t6.server.verbs import VerbAPI
from t6.notion.testing import FakeNotion, project_properties
from t6.verbs import Done, ProjectChatInput, ProjectLinksInput, ProjectLinksOutput, Refused

CHAT = -1001


class ProjectLinksChatTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.registry = SqliteProjectRegistry(self.tmp.name + '/registry')
        self.operator, self.staff, self.coordinator = ('t6n_' + c * 32 for c in '123')
        with self.registry._transaction() as db:
            for pid in (self.operator, self.staff, self.coordinator):
                db.execute("INSERT INTO persons VALUES (?, 'staff', '[]', '[]', ?, 0, 1)", (pid, {self.operator: 11, self.staff: 12, self.coordinator: 13}[pid]))
            db.execute("INSERT INTO person_permissions VALUES (?, 'operator')", (self.operator,))
            db.execute("INSERT INTO person_permissions VALUES (?, 'coordinator')", (self.coordinator,))
        self.fake = FakeNotion()
        self.fake.add_snapshot_data_source('projects', 'projects')
        self.ids: dict[str, str] = {}
        for n, (code, name) in enumerate((('ABC-123', 'Big Show'), ('XYZ-9', 'Other Show'), ('LONE-1', 'Lonely'))):
            page = str(self.fake.add_page('projects', project_properties(name=name, project_type='Feature Film'))['id'])
            self.registry._new_id = functools.partial(lambda n: 't6p_' + str(n) * 24, n)
            registered = self.registry.register(actor=self.operator, idempotency_key=f'p{n}', notion_page_id=page, code=code, rotation=Rotation.IN)
            assert isinstance(registered, Registered)
            self.ids[code] = registered.project.id
        with self.registry._transaction() as db:
            for n, (code, chat, topic, state) in enumerate((
                ('ABC-123', CHAT, 12, 'active'), ('XYZ-9', CHAT, None, 'active'), ('LONE-1', CHAT, 7, 'active'),
                ('LONE-1', CHAT, 99, 'inactive'), ('XYZ-9', -2002, 5, 'active'),
            )):
                db.execute('INSERT INTO chat_links VALUES (?, ?, ?, ?, ?)', (f't6h_{n}', self.ids[code], chat, topic, state))
        self.api = VerbAPI(
            People(self.registry, PreviewTokens('test'), {'gateway': frozenset({'project_links'})}),
            JobQueue(self.tmp.name + '/queue'),
            {'operator-secret': self.operator, 'staff-secret': self.staff, 'coord-secret': self.coordinator,
             'gateway-secret': 'gateway', 'reports-secret': 'reports', 'mcp-secret': 'mcp'},
            lambda _: self.fake.client(), privacy_registry=PrivacyRegistry())
        self.server = make_server(('127.0.0.1', 0), self.api)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        self.base_url = f'http://127.0.0.1:{self.server.server_port}'
        env = patch.dict(os.environ, {'T6_NOTION_TOKEN': 'fake'})
        env.start()
        self.addCleanup(env.stop)

    def ask(self, secret: str, **inputs: Any) -> Result:
        return Client(self.base_url, secret).project_links(ProjectLinksInput(**inputs))

    def read(self, secret: str, **inputs: Any) -> dict[str, Any]:
        done = self.ask(secret, **inputs)
        assert isinstance(done, Done)
        return dict(done.result)

    def test_gateway_reads_the_projects_linked_to_a_chat_and_topic_in_link_order(self) -> None:
        out = ProjectLinksOutput.model_validate(self.read('gateway-secret', chat=CHAT, topic=12))
        # Topic 12 and the whole-chat link match; topic 7 and the inactive link do not.
        self.assertEqual([card.code for card in out.projects], ['ABC-123', 'XYZ-9'])
        self.assertEqual([card.name for card in out.projects], ['Big Show', 'Other Show'])  # real names, not masked
        self.assertIsNone(out.project_id)
        first = out.projects[0]
        self.assertEqual((first.project_id, first.links, first.links_unavailable), (self.ids['ABC-123'], {}, False))
        self.assertEqual([(link.chat, link.topic, link.state) for link in first.chat_links], [(CHAT, 12, 'active')])
        self.assertEqual([card.code for card in ProjectLinksOutput.model_validate(self.read('gateway-secret', chat=CHAT)).projects], ['XYZ-9'])
        self.assertEqual(ProjectLinksOutput.model_validate(self.read('gateway-secret', chat=CHAT, topic=5)).projects[0].code, 'XYZ-9')

    def test_gateway_reads_a_project_linked_to_the_chat_and_is_refused_alike_for_any_other(self) -> None:
        out = ProjectLinksOutput.model_validate(self.read('gateway-secret', chat=CHAT, project='abc 123'))
        self.assertEqual((out.project_id, out.code, out.name), (self.ids['ABC-123'], 'ABC-123', 'Big Show'))  # the single-project fields stay
        self.assertEqual([card.project_id for card in out.projects], [self.ids['ABC-123']])
        self.assertEqual(out.chat_links, out.projects[0].chat_links)
        for project in (self.ids['ABC-123'], 'XYZ-9', 'NOPE'):
            with self.subTest(project=project):
                self.assertIsInstance(self.ask('gateway-secret', chat=CHAT, project=project), Done if project != 'NOPE' else Refused)
        refusals = [self.ask('gateway-secret', chat=-2002, project='ABC-123'), self.ask('gateway-secret', chat=CHAT, project='NOPE'),
                    self.ask('gateway-secret', chat=CHAT, project=self.ids['LONE-1'] + 'x')]
        self.assertEqual({(r.rule, r.reason) for r in refusals if isinstance(r, Refused)}, {('chat_scope', 'Project is not linked to this chat')})
        self.assertEqual(len(refusals), 3)
        inactive = self.ask('gateway-secret', chat=-2002, project='LONE-1')  # only an inactive link elsewhere
        assert isinstance(inactive, Refused)
        self.assertEqual(inactive.rule, 'chat_scope')

    def test_gateway_without_a_chat_is_refused(self) -> None:
        refused = self.ask('gateway-secret', project='ABC-123')
        assert isinstance(refused, Refused)
        self.assertEqual(refused.rule, 'permission')

    def test_the_grant_allows_only_this_read(self) -> None:
        client = Client(self.base_url, 'gateway-secret')
        refused = client.project_link_chat(ProjectChatInput(project='ABC-123', chat=CHAT, topic=3))
        assert isinstance(refused, Refused)
        self.assertEqual(refused.rule, 'permission')
        refusal = People(self.registry, PreviewTokens('t'), {'gateway': frozenset({'project_links'})}).authorize('gateway', 'operator')
        assert refusal is not None
        self.assertEqual(refusal.rule, 'permission')
        with self.registry._transaction() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM chat_links WHERE chat_id = ? AND topic_id = 3", (CHAT,)).fetchone()[0], 0)

    def test_others_are_refused_on_a_chat_read(self) -> None:
        for secret in ('reports-secret', 'mcp-secret', 'staff-secret', 'coord-secret'):
            for inputs in ({'chat': CHAT}, {'chat': CHAT, 'project': 'ABC-123'}):
                with self.subTest(secret=secret, **inputs):
                    refused = self.ask(secret, **inputs)
                    assert isinstance(refused, Refused)
                    self.assertEqual(refused.rule, 'permission')

    def test_operator_reads_any_project_with_or_without_a_chat(self) -> None:
        for inputs in ({'project': 'LONE-1'}, {'project': 'LONE-1', 'chat': -777}, {'project': 'LONE-1', 'chat': CHAT, 'topic': 1}):
            with self.subTest(**inputs):
                done = self.ask('operator-secret', **inputs)
                assert isinstance(done, Done)
                self.assertEqual(ProjectLinksOutput.model_validate(done.result).code, 'LONE-1')
        refused = self.ask('operator-secret', project='NOPE')
        assert isinstance(refused, Refused)
        self.assertEqual(refused.rule, 'project')
        by_chat = self.read('operator-secret', chat=CHAT, topic=7)
        self.assertEqual([card['code'] for card in by_chat['projects']], ['XYZ-9', 'LONE-1'])

    def test_input_refusals(self) -> None:
        for inputs in ({}, {'topic': 3}):
            with self.subTest(**inputs):
                refused = self.ask('operator-secret', **inputs)
                assert isinstance(refused, Refused)
                self.assertEqual(refused.rule, 'input')
        refused = self.ask('gateway-secret', topic=3, project='ABC-123')
        assert isinstance(refused, Refused)
        self.assertIn(refused.rule, ('input', 'permission'))
        refused = self.ask('operator-secret', topic=3, project='ABC-123')
        assert isinstance(refused, Refused)
        self.assertEqual(refused.rule, 'input')

    def test_the_card_still_carries_crm_links_for_each_project(self) -> None:
        card = {'quote': ['https://example.com/q']}
        with patch('t6.verbs._card_links', lambda project_id: (card, False) if project_id == self.ids['XYZ-9'] else ({}, True)):
            projects = ProjectLinksOutput.model_validate(self.read('gateway-secret', chat=CHAT, topic=12)).projects
        self.assertEqual([(p.code, p.links, p.links_unavailable) for p in projects], [('ABC-123', {}, True), ('XYZ-9', card, False)])

    def test_unlinked_topic_says_whether_the_asker_can_link_and_suggests_the_code(self) -> None:
        def ask(asker: int | None, topic_name: str | None = 'ABC123 - Edit', topic: int = 50, secret: str = 'gateway-secret') -> dict[str, Any]:
            return self.read(secret, chat=CHAT - 1, topic=topic, topic_name=topic_name, asker_telegram_id=asker)
        coordinator = ask(13)
        self.assertEqual((coordinator['projects'], coordinator['can_link'], coordinator['suggested_code']), ([], True, 'ABC123'))
        self.assertEqual(ask(11)['suggested_code'], 'ABC123')  # an operator may link too
        for asker in (12, 999, None):  # plain staff, no Person record, nobody given
            with self.subTest(asker=asker):
                self.assertEqual((ask(asker)['can_link'], ask(asker)['suggested_code']), (False, None))
        general = ask(13, 'General Chat')
        self.assertEqual((general['can_link'], general['suggested_code']), (True, None))
        self.assertEqual(ask(13, None)['suggested_code'], None)
        self.assertEqual(ask(13, '   ')['suggested_code'], None)

    def test_topic_name_falls_back_to_the_recorded_topic_and_is_ignored_on_a_linked_topic(self) -> None:
        with self.registry._transaction() as db:
            db.execute("INSERT INTO linked_chat_topics VALUES (?, 60, 'QQ1 - Color')", (CHAT - 1,))
        self.assertEqual(self.read('gateway-secret', chat=CHAT - 1, topic=60, asker_telegram_id=13)['suggested_code'], 'QQ1')
        linked = self.read('gateway-secret', chat=CHAT, topic=12, topic_name='ABC123 - Edit', asker_telegram_id=13)
        self.assertEqual((len(linked['projects']), linked['can_link'], linked['suggested_code']), (2, False, None))

    def test_an_operator_person_asking_for_themselves_can_link(self) -> None:
        done = self.read('operator-secret', chat=CHAT - 1, topic=1, topic_name='AB - X')
        self.assertEqual((done['can_link'], done['suggested_code']), (True, 'AB'))

    def test_the_client_sends_only_the_inputs_it_has(self) -> None:
        self.assertEqual(ProjectLinksInput(chat=CHAT).model_dump(mode='json', exclude_none=True), {'chat': CHAT})


class TopicCodeTests(unittest.TestCase):
    def test_topic_code_reads_the_code_before_the_dash(self) -> None:
        from t6.chat_link import topic_code
        self.assertEqual(topic_code(' ABC123 - Edit'), 'ABC123')
        self.assertEqual([topic_code(n) for n in (None, '', 'General', 'ABC-123 - Edit', 'ABC123-Edit')], [None, None, None, None, None])
