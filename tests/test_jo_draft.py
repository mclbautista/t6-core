"""`jo draft` through the API server verb seam with fake Notion: invented Projects and people."""
import os
import tempfile
import threading
import unittest
from typing import Any
from unittest.mock import patch

from t6.client import Client
from t6.notion.generated import ContactsRow, JobOrdersRow, ProjectsRow
from t6.notion.testing import FakeNotion, relation, rich_text, select, title
from t6.person import People
from t6.preview import PreviewTokens
from t6.privacy import Person, Registry
from t6.registry import Rotation
from t6.registry.sqlite import SqliteProjectRegistry
from t6.server.http import make_server
from t6.server.queue import JobQueue
from t6.server.verbs import VerbAPI
from t6.verbs import Done, JoDraftInput, Refused

SOURCES = ('projects', 'job_orders', 'deliverables', 'meeting_notes', 'contacts', 'cameras')


class JoDraftTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.notion = FakeNotion()
        for source in SOURCES:
            self.notion.add_snapshot_data_source(source, source)
        self.registry = SqliteProjectRegistry(self.tmp.name + '/registry')
        self.project = self.notion.add_page('projects', {ProjectsRow.notion_name('name'): title('Harbor Lights')})
        self.registry.register(actor='setup', idempotency_key='p', notion_page_id=self.project['id'], code='HBR', rotation=Rotation.IN)
        self.maya = self.notion.add_page('contacts', {ContactsRow.notion_name('name'): title('Maya Reyes'),
                                                      ContactsRow.notion_name('telegram_handle'): rich_text('mayar'),
                                                      ContactsRow.notion_name('telegram_user_id'): rich_text('11')})
        self.lea = self.notion.add_page('contacts', {ContactsRow.notion_name('name'): title('Lea Ong'),
                                                     ContactsRow.notion_name('telegram_handle'): rich_text('@leaong')})
        operator = 't6n_' + '1' * 32
        with self.registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES (?, 'staff', '[]', '[]', NULL, 0, 1)", (operator,))
            db.execute("INSERT INTO person_permissions VALUES (?, 'operator')", (operator,))
        api = VerbAPI(People(self.registry, PreviewTokens('key'), {}), JobQueue(self.tmp.name + '/queue'),
                      {'secret': 'reports', 'operator': operator}, lambda _: self.notion.client(),
                      privacy_registry=Registry(people=[Person(name='Maya Reyes')]), data_sources={name: name for name in SOURCES})
        env = patch.dict(os.environ, {'T6_NOTION_TOKEN': 'fake', 'T6_STANDING_CC': self.lea['id']})
        env.start()
        self.addCleanup(env.stop)
        server = make_server(('127.0.0.1', 0), api)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        self.operator = Client(f'http://127.0.0.1:{server.server_port}', 'operator')
        self.reports = Client(f'http://127.0.0.1:{server.server_port}', 'secret')

    def jo(self, number: str | None, department: str = 'COOR', project: dict[str, Any] | None = None) -> None:
        fields: dict[str, Any] = {JobOrdersRow.notion_name('name'): title(f'HBR - {department} - JO #{number}'),
                                  JobOrdersRow.notion_name('projects'): relation((project or self.project)['id']),
                                  JobOrdersRow.notion_name('department'): select(department)}
        if number is not None:
            fields[JobOrdersRow.notion_name('jo_number')] = rich_text(f'#{number}')
        self.notion.add_page('job_orders', fields)

    def draft(self, line: str, date: str = '2026-11-25') -> Any:
        return self.operator.jo_draft(JoDraftInput(line=line, date=date))

    def test_draft_has_header_title_task_assigned_and_cc(self) -> None:
        self.jo('118')
        result = self.draft('HBR ftr export and upload seq 5 to pix share, for Maya')
        assert isinstance(result, Done)
        lines = result.result['text'].splitlines()
        self.assertEqual(lines[0], 'HBR - (?) - JO #119')
        self.assertEqual(lines[1], 'November 25 / Wednesday')
        self.assertIn('Title: Harbor Lights FTR', lines)
        self.assertIn('Task: ftr export and upload seq 5 to pix share', lines)
        self.assertIn('Assigned: @mayar', lines)
        self.assertIn('cc: @leaong', lines)
        self.assertIn('Purpose: (?)', lines)
        self.assertIn('Status:', lines)
        self.assertEqual(result.result['number_note'], '#119 (last: #118 COOR)')

    def test_number_is_highest_plus_one_and_gaps_stay(self) -> None:
        for number in ('3', '118', '40'):
            self.jo(number)
        result = self.draft('HBR tlr color pass, for Maya')
        assert isinstance(result, Done)
        self.assertEqual(result.result['text'].splitlines()[0], 'HBR - (?) - JO #119')

    def test_first_job_order_of_a_project_is_number_one(self) -> None:
        result = self.draft('HBR ep07 audio fix, for Maya')
        assert isinstance(result, Done)
        self.assertEqual(result.result['text'].splitlines()[0], 'HBR - (?) - JO #1')
        self.assertIn('Title: Harbor Lights Ep07', result.result['text'])
        self.assertIsNone(result.result['number_note'])

    def test_other_projects_numbers_do_not_count(self) -> None:
        other = self.notion.add_page('projects', {ProjectsRow.notion_name('name'): title('Other Show')})
        self.jo('500', project=other)
        self.jo('7')
        result = self.draft('HBR ftr conform, for Maya')
        assert isinstance(result, Done)
        self.assertEqual(result.result['text'].splitlines()[0], 'HBR - (?) - JO #8')

    def test_a_line_without_a_deliverable_code_gets_a_finding(self) -> None:
        self.jo('1')
        result = self.draft('HBR conform the reel, for Maya')
        assert isinstance(result, Done)
        self.assertIn('Title: Harbor Lights\n', result.result['text'])
        self.assertIn('no_deliverable_code', [f['code'] for f in result.result['findings']])

    def test_unknown_project_and_unknown_person_are_refused_with_distinct_rules(self) -> None:
        unknown_project = self.draft('ZZZ ftr export, for Maya')
        assert isinstance(unknown_project, Refused)
        self.assertEqual(unknown_project.rule, 'project')
        unknown_person = self.draft('HBR ftr export, for Nobody Here')
        assert isinstance(unknown_person, Refused)
        self.assertEqual(unknown_person.rule, 'person')
        no_person = self.draft('HBR ftr export')
        assert isinstance(no_person, Refused)
        self.assertEqual(no_person.rule, 'person')

    def test_report_totals_equal_the_steps_and_hold_no_names(self) -> None:
        self.jo('4')
        result = self.draft('HBR ftr export, for Maya')
        assert isinstance(result, Done)
        report = result.result['report']
        self.assertEqual([s['step'] for s in report['steps']], ['bundle', 'formatter'])
        self.assertEqual(report['total_ms'], sum(s['ms'] for s in report['steps']))
        for name in ('Maya', 'Harbor', 'export'):
            self.assertNotIn(name, str(report))

    def test_only_an_operator_may_draft(self) -> None:
        result = self.reports.jo_draft(JoDraftInput(line='HBR ftr export, for Maya', date='2026-11-25'))
        assert isinstance(result, Refused)
        self.assertEqual(result.rule, 'permission')

    def test_draft_writes_nothing(self) -> None:
        self.jo('9')
        before = len(self.notion.client().query_data_source('job_orders'))
        self.draft('HBR ftr export, for Maya')
        self.assertEqual(len(self.notion.client().query_data_source('job_orders')), before)

    def test_jo_draft_is_not_on_the_mcp_server(self) -> None:
        from t6.verbs import VERBS
        entry = next(v for v in VERBS if (v.noun, v.verb) == ('jo', 'draft'))
        self.assertFalse(entry.read_only)


class NextNumberTests(unittest.TestCase):
    def test_a_taken_number_steps_up(self) -> None:
        from t6.jo_draft import next_number
        number, note = next_number([('#10', 'EDIT'), ('9', 'COOR')], lambda n: n in (11, 12))
        self.assertEqual((number, note), (13, '#13 (last: #10 EDIT)'))

    def test_parse_line_takes_the_code_the_tail_name_and_the_deliverable(self) -> None:
        from t6.jo_draft import parse_line
        line = parse_line("MFG ftr task export and upload seq 5 to pix share to netflix, for Majo")
        assert line is not None
        self.assertEqual((line.code, line.assignee, line.deliverable), ('MFG', 'Majo', 'FTR'))
        self.assertEqual(line.task, 'ftr task export and upload seq 5 to pix share to netflix')
        self.assertIsNone(parse_line('   '))


if __name__ == '__main__':
    unittest.main()
