import json
import os
import tempfile
import unittest
from typing import Any
from unittest.mock import patch

from test_abang import Gateway, MemoryRegistry
from t6.notion.generated import ContactsRow
from t6.notion.testing import FakeNotion, rich_text, title
from t6.person import People
from t6.preview import PreviewTokens
from t6.server import Request, ServerConfig, WebhookApp
from t6.server.queue import JobQueue
from t6.server.verbs import VerbAPI
from t6.worker.gateway import GatewayClient
from t6.worker.linked_chats import LinkedChats

COORDINATOR, VENDOR, OTHER = 't6n_' + '1' * 32, 't6n_' + '2' * 32, 't6n_' + '3' * 32


class AbangCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = MemoryRegistry()
        self.addCleanup(self.registry.db.close)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {'T6_PREVIEW_SIGNING_KEY': 'test', 'T6_NOTION_TOKEN': 'fake'})
        env.start()
        self.addCleanup(env.stop)
        self.notion = FakeNotion()
        self.notion.add_snapshot_data_source('contacts', 'contacts')
        page = self.notion.add_page('contacts', {'Name': title('Placeholder Freelancer'), ContactsRow.notion_name('telegram_handle'): rich_text('freelancer')})
        with self.registry._transaction() as db:
            db.execute("DELETE FROM persons WHERE id IN ('vendor', 'coordinator')")
            db.execute("DELETE FROM linked_chat_members")
            db.execute("DELETE FROM chat_links")
            db.execute("INSERT INTO persons VALUES (?, 'staff', '[]', '[]', 7, 0, 1)", (COORDINATOR,))
            db.execute("INSERT INTO person_permissions VALUES (?, 'coordinator')", (COORDINATOR,))
            db.execute("INSERT INTO persons VALUES (?, 'vendor', '[]', '[]', 42, 0, 1)", (VENDOR,))
            db.execute("INSERT INTO persons VALUES (?, 'staff', '[]', '[]', 8, 0, 1)", (OTHER,))
            db.execute("INSERT INTO person_permissions VALUES (?, 'coordinator')", (OTHER,))
            db.execute("INSERT INTO linked_chat_members VALUES (-100, 42, ?, 1)", (VENDOR,))
            for link, project, chat, topic in (('src', 'p1', -100, None), ('dst', 'p1', -200, 5), ('dst2', 'p1', -300, None), ('other', 'p2', -400, None)):
                db.execute("INSERT INTO chat_links VALUES (?, ?, ?, ?, 'active')", (link, project, chat, topic))
            db.execute("INSERT INTO linked_chat_topics VALUES (-200, 5, 'Editorial')")
            db.execute("INSERT INTO projects (id, revision, code, code_form, rotation, created_at) VALUES ('p1', 1, 'ABC123', 'abc123', 'in', 0)")
            from t6.registry.core import normalize_page_id
            db.execute("INSERT INTO bindings (system, page_id, project_id, bound_at, active) VALUES ('notion', ?, ?, 0, 1)", (normalize_page_id(page['id']), VENDOR))
        queue = JobQueue(tmp.name + '/queue')
        self.api = VerbAPI(People(self.registry, PreviewTokens('test')), queue, {'gateway': 'gateway'}, lambda _: self.notion.client(),
                           WebhookApp(queue, ServerConfig(), registry=self.registry), data_sources={'contacts': 'contacts'})
        self.gateway = Gateway()
        self.worker = LinkedChats(self.registry, GatewayClient(self.gateway, 'secret'))

    def push(self, who: int, body: dict[str, Any], key: str) -> Any:
        body = {**body, 'from': {'id': who}}
        return self.api(Request('POST', '/tap', {'Authorization': 'Bearer gateway', 'X-Telegram-User-Id': str(who), 'Idempotency-Key': key}, json.dumps(body).encode()))

    def command(self, who: int, args: str, key: str) -> Any:
        return self.push(who, {'kind': 'command', 'command': 'abang', 'args': args, 'chat': -100, 'topic': None}, key)

    def state(self) -> list[Any]:
        with self.registry._transaction() as db:
            return db.execute('SELECT id, state FROM abang_drafts').fetchall()

    def test_command_offers_one_button_per_other_linked_chat_of_the_projects(self) -> None:
        self.assertEqual(self.command(7, '@freelancer graded trailer', 'cmd-1').body['outcome'], 'accepted')
        self.assertEqual(self.command(7, '@freelancer graded trailer', 'cmd-1').body['outcome'], 'accepted')
        self.assertEqual(len(self.state()), 1)
        with self.registry._transaction() as db:
            self.assertEqual(db.execute('SELECT chat, topic, label FROM abang_draft_options ORDER BY n').fetchall(), [(-200, 5, 'ABC123 · Editorial'), (-300, None, 'ABC123')])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM abang_requests').fetchone()[0], 0)
        self.worker.tick()
        sent = [call[2] for call in self.gateway.calls if call[1] == '/notify']
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]['to'], -100)
        self.assertEqual([row[0]['text'] for row in sent[0]['buttons']], ['ABC123 · Editorial', 'ABC123'])

    def test_only_the_coordinator_who_typed_it_can_tap_and_the_tap_confirms(self) -> None:
        self.command(7, '@freelancer graded trailer', 'cmd-2')
        draft = self.state()[0][0]

        def tap(who: int, key: str) -> Any:
            return self.push(who, {'kind': 'tap', 'data': f'abang-dest:{draft}:0'}, key)

        self.assertEqual(tap(8, 'tap-other').body['outcome'], 'rejected')
        self.assertEqual(tap(42, 'tap-vendor').body['outcome'], 'rejected')
        with self.registry._transaction() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM abang_requests').fetchone()[0], 0)
        self.assertEqual(tap(7, 'tap-coordinator').body['outcome'], 'accepted')
        with self.registry._transaction() as db:
            self.assertEqual(db.execute('SELECT person_id, source_chat, source_topic, destination_chat, destination_topic, note, actor, state FROM abang_requests').fetchall(), [(VENDOR, -100, None, -200, 5, 'graded trailer', COORDINATOR, 'requested')])
        self.assertEqual(self.state()[0][1], 'used')
        self.assertEqual(tap(7, 'tap-again').body['outcome'], 'rejected')

    def test_a_t6_id_still_works_and_unknown_or_non_vendor_people_do_not(self) -> None:
        self.assertEqual(self.command(7, VENDOR, 'by-id').body['outcome'], 'accepted')
        self.assertEqual(self.command(7, '@nobody', 'unknown').body['outcome'], 'rejected')
        self.assertEqual(self.command(7, OTHER, 'staff').body['outcome'], 'rejected')
        self.assertEqual(self.command(42, VENDOR, 'not-a-coordinator').body['outcome'], 'rejected')
        self.assertEqual(len(self.state()), 1)

    def test_a_chat_with_no_other_linked_destination_gives_no_draft(self) -> None:
        with self.registry._transaction() as db:
            db.execute("DELETE FROM chat_links WHERE id IN ('dst', 'dst2')")
        self.assertEqual(self.command(7, '@freelancer', 'lonely').body['outcome'], 'rejected')
        self.assertEqual(self.state(), [])


if __name__ == '__main__':
    unittest.main()
