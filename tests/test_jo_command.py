"""`/jo` pushed by the gateway: the draft in one message, the run report in a second (spec #791, ticket #795)."""
import json
import os
import tempfile
import unittest
from typing import Any
from unittest.mock import patch

from t6.notion.generated import ContactsRow, JobOrdersRow, ProjectsRow
from t6.notion.testing import FakeNotion, relation, rich_text, select, title
from t6.person import People
from t6.preview import PreviewTokens
from t6.registry import Rotation
from t6.registry.sqlite import SqliteProjectRegistry
from t6.server import Request, ServerConfig, WebhookApp
from t6.server.queue import JobQueue
from t6.server.verbs import VerbAPI

SOURCES = ('projects', 'job_orders', 'deliverables', 'meeting_notes', 'contacts', 'cameras')
OPERATOR, STAFF = 't6n_' + '1' * 32, 't6n_' + '2' * 32


class JoCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {'T6_PREVIEW_SIGNING_KEY': 'test', 'T6_NOTION_TOKEN': 'fake'})
        env.start()
        self.addCleanup(env.stop)
        self.notion = FakeNotion()
        for source in SOURCES:
            self.notion.add_snapshot_data_source(source, source)
        self.registry = SqliteProjectRegistry(self.tmp.name + '/registry')
        project = self.notion.add_page('projects', {ProjectsRow.notion_name('name'): title('Harbor Lights')})
        self.registry.register(actor='setup', idempotency_key='p', notion_page_id=project['id'], code='HBR', rotation=Rotation.IN)
        self.notion.add_page('job_orders', {JobOrdersRow.notion_name('name'): title('HBR - COOR - JO #118'),
                                            JobOrdersRow.notion_name('projects'): relation(project['id']),
                                            JobOrdersRow.notion_name('department'): select('COOR'),
                                            JobOrdersRow.notion_name('jo_number'): rich_text('#118')})
        self.notion.add_page('contacts', {ContactsRow.notion_name('name'): title('Maya Reyes'),
                                          ContactsRow.notion_name('telegram_handle'): rich_text('mayar')})
        with self.registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES (?, 'staff', '[]', '[]', 7, 0, 1)", (OPERATOR,))
            db.execute("INSERT INTO person_permissions VALUES (?, 'operator')", (OPERATOR,))
            db.execute("INSERT INTO persons VALUES (?, 'staff', '[]', '[]', 8, 0, 1)", (STAFF,))
        self.queue = JobQueue(self.tmp.name + '/queue')
        self.api = VerbAPI(People(self.registry, PreviewTokens('test')), self.queue, {'gateway': 'gateway'}, lambda _: self.notion.client(),
                           WebhookApp(self.queue, ServerConfig(), registry=self.registry), data_sources={name: name for name in SOURCES})

    def jo(self, who: int, args: str, key: str, chat: int | None = None) -> Any:
        body = {'kind': 'command', 'command': 'jo', 'args': args, 'chat': who if chat is None else chat, 'topic': None, 'from': {'id': who}}
        return self.api(Request('POST', '/tap', {'Authorization': 'Bearer gateway', 'X-Telegram-User-Id': str(who), 'Idempotency-Key': key}, json.dumps(body).encode()))

    def replies(self) -> list[tuple[str, Any, str]]:
        return [(job.dedupe_key, job.payload['chat'], str(job.payload['text'])) for job in self.queue.waiting('main') if job.kind == 'chat.reply']

    def test_an_operator_gets_the_draft_then_the_report_as_two_plain_messages(self) -> None:
        self.assertEqual(self.jo(7, 'HBR ftr export seq 5, for Maya', 'c1').body['outcome'], 'accepted')
        (draft, report) = self.replies()
        self.assertEqual((draft[0], draft[1]), ('chat.reply:c1', 7))
        self.assertIn('HBR - (?) - JO #119', draft[2])
        self.assertIn('Assigned: @mayar', draft[2])
        self.assertIn('Number: #119 (last: #118 COOR)', draft[2])
        self.assertEqual((report[0], report[1]), ('chat.reply:c1:report', 7))
        self.assertTrue(report[2].startswith('Run report:'))
        self.assertIn('bundle:', report[2])
        self.assertNotIn('Harbor', report[2])
        self.assertNotIn('Maya', report[2])
        self.assertNotIn('<', draft[2] + report[2])

    def test_a_resent_command_sends_nothing_twice(self) -> None:
        self.jo(7, 'HBR ftr export, for Maya', 'c1')
        self.jo(7, 'HBR ftr export, for Maya', 'c1')
        self.assertEqual(len(self.replies()), 2)

    def test_someone_not_allowed_gets_the_standard_refusal_and_no_draft(self) -> None:
        self.assertEqual(self.jo(8, 'HBR ftr export, for Maya', 'c2').body['outcome'], 'rejected')
        self.assertEqual(self.replies(), [('chat.reply:c2', 8, 'Only an operator can use /jo.')])

    def test_an_unknown_project_or_person_is_one_line(self) -> None:
        self.jo(7, 'ZZZ ftr export, for Maya', 'c3')
        self.jo(7, 'HBR ftr export, for Nobody Here', 'c4')
        texts = [r[2] for r in self.replies()]
        self.assertEqual(len(texts), 2)
        self.assertTrue(texts[0].startswith('/jo: ') and '\n' not in texts[0])
        self.assertTrue(texts[1].startswith('/jo: ') and '\n' not in texts[1])

    def test_it_only_answers_in_the_operators_own_dm_and_needs_a_line(self) -> None:
        self.jo(7, 'HBR ftr export, for Maya', 'c5', chat=-100)
        self.jo(7, '  ', 'c6')
        self.assertEqual([r[2] for r in self.replies()], ['Use /jo in a DM with the bot.', 'Usage: /jo PROJECT CODE, what to do, for NAME'])


if __name__ == '__main__':
    unittest.main()
