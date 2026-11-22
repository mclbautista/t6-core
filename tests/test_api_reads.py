"""Read verbs through the HTTP client and fake Notion."""
import os
import tempfile
import threading
import unittest
from typing import Any
from unittest.mock import patch

from t6.client import APIError, Client
from t6.notion.testing import FakeNotion, title, relation, rich_text, date, select
from t6.notion.generated import JobOrdersRow, DeliverablesRow, MeetingNotesRow
from t6.person import People
from t6.preview import PreviewTokens
from t6.registry.sqlite import SqliteProjectRegistry
from t6.server.http import make_server
from t6.server.queue import JobQueue
from t6.server.verbs import VerbAPI
from t6.verbs import Done, Preview, Refused
from t6.privacy import Person, Registry


class ReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.notion = FakeNotion()
        for source in ('projects', 'job_orders', 'deliverables', 'meeting_notes', 'contacts', 'cameras'):
            self.notion.add_snapshot_data_source(source, source)
        self.notion.add_page('projects', {'Name': title('Client Corp campaign')})
        self.person = self.notion.add_page('contacts', {'Name': title('Ana Cruz')})
        self.camera = self.notion.add_page('cameras', {'Name': title('Camera One')})
        self.project = self.notion.add_page('projects', {'Name': title('Example Show')})
        self.jo = self.notion.add_page('job_orders', {JobOrdersRow.notion_name('name'): title('Work'), JobOrdersRow.notion_name('telegram_key'): rich_text('-100:3'), JobOrdersRow.notion_name('projects'): relation(self.project['id']), JobOrdersRow.notion_name('assigned'): relation(self.person['id'])})
        self.notion.add_page('deliverables', {DeliverablesRow.notion_name('name'): title('Trailer'), DeliverablesRow.notion_name('projects'): relation(self.project['id'])})
        self.note = self.notion.add_page('meeting_notes', {MeetingNotesRow.notion_name('name'): title('Notes'), MeetingNotesRow.notion_name('project'): relation(self.project['id']), MeetingNotesRow.notion_name('meeting_date'): date('2026-11-12')})
        with self.notion.client() as client:
            client.append_block_children(self.note['id'], [{'type': 'paragraph', 'paragraph': {'rich_text': [{'type': 'text', 'text': {'content': 'Meeting details'}, 'annotations': {'bold': True}}]}}])
        registry = SqliteProjectRegistry(self.tmp.name + '/registry')
        self.registry = registry
        operator = 't6n_' + '1' * 32
        with registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES (?, 'staff', '[]', '[]', NULL, 0, 1)", (operator,))
            db.execute("INSERT INTO person_permissions VALUES (?, 'operator')", (operator,))
        self.api = VerbAPI(People(registry, PreviewTokens('key'), {'watchdog': frozenset({'unmasked'})}), JobQueue(self.tmp.name + '/queue'),
                           {'secret': 'reports', 'operator': operator, 'mcp-secret': 'mcp', 'unmasked-secret': 'watchdog'}, lambda _: self.notion.client(),
                           privacy_registry=Registry(people=[Person(name='Client Corp')]),
                           data_sources={name: name for name in ('projects', 'job_orders', 'deliverables', 'meeting_notes', 'contacts', 'cameras')})
        env = patch.dict(os.environ, {'T6_NOTION_TOKEN': 'fake'})
        env.start()
        self.addCleanup(env.stop)
        self.server = make_server(('127.0.0.1', 0), self.api)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(self.server.shutdown)
        self.client = Client(f'http://127.0.0.1:{self.server.server_port}', 'secret')

    def test_project_list_uses_server_owned_source(self) -> None:
        from t6.verbs import ProjectListInput
        result = self.client.project_list(ProjectListInput())
        self.assertIsInstance(result, Done)
        assert isinstance(result, Done)
        self.assertEqual(result.result['projects'][1]['name'], 'Example Show')

    def test_health_reports_projects_with_a_blank_name_as_a_data_gap_with_page_ids_only(self) -> None:
        health = self.client.health()
        assert isinstance(health, dict)
        self.assertEqual(health['data_gaps'], {'projects_without_name': {'count': 0, 'page_ids': []}})
        blank = self.notion.add_page('projects', {'Name': title('')})
        spaces = self.notion.add_page('projects', {'Name': title('  ')})
        after = self.client.health()
        assert isinstance(after, dict)
        gaps = after['data_gaps']['projects_without_name']
        self.assertEqual(gaps['count'], 2)
        self.assertEqual(sorted(gaps['page_ids']), sorted([blank['id'], spaces['id']]))
        self.assertNotIn('Example Show', str(gaps))
        anonymous = Client(f'http://127.0.0.1:{self.server.server_port}', '').health()
        assert isinstance(anonymous, dict)
        self.assertNotIn('data_gaps', anonymous)

    def test_bay_list_is_a_typed_client_read(self) -> None:
        from t6.verbs import BayListInput
        result = self.client.bay_list(BayListInput())
        self.assertIsInstance(result, Done)
        assert isinstance(result, Done)
        self.assertEqual(result.result, {'bays': []})

    def test_project_list_returns_project_health_beside_health(self) -> None:
        from t6.verbs import ProjectListInput
        from t6.notion.generated import ProjectsRow
        from t6.notion.testing import select
        self.notion.add_page('projects', {ProjectsRow.notion_name('name'): title('Health show'),
                                          ProjectsRow.notion_name('health'): select('Fine'),
                                          ProjectsRow.notion_name('project_health'): select('At risk')})
        result = self.client.project_list(ProjectListInput())
        assert isinstance(result, Done)
        project = next(p for p in result.result['projects'] if p['name'] == 'Health show')
        self.assertEqual((project['health'], project['project_health']), ('Fine', 'At risk'))
        plain = next(p for p in result.result['projects'] if p['name'] == 'Example Show')
        self.assertIsNone(plain['project_health'])

    def test_project_reads_return_date_ends_and_supervisors(self) -> None:
        from t6.verbs import ProjectGetInput, ProjectListInput
        from t6.notion.generated import ProjectsRow
        supe = self.notion.add_page('contacts', {'Name': title('Ben Lim')})
        bound = self.bound_person(supe)
        unbound = self.notion.add_page('contacts', {'Name': title('Cy Tan')})
        self.notion.add_page('projects', {
            ProjectsRow.notion_name('name'): title('Range show'),
            ProjectsRow.notion_name('playdate'): date('2026-11-16', '2026-11-20'),
            ProjectsRow.notion_name('submission_deadline'): date('2026-11-10T09:00:00.000+00:00', '2026-11-10T17:30:00.000+00:00'),
            ProjectsRow.notion_name('archival_deadline'): date('2026-12-01'),
            ProjectsRow.notion_name('prod_coor_supe'): relation(supe['id'], unbound['id'])})
        result = self.client.project_list(ProjectListInput())
        assert isinstance(result, Done)
        project = next(p for p in result.result['projects'] if p['name'] == 'Range show')
        self.assertEqual((project['playdate'], project['playdate_end']), ('2026-11-16', '2026-11-20'))
        self.assertEqual(project['submission_deadline_end'][:19], '2026-11-10T17:30:00')
        self.assertEqual((project['archival_deadline'], project['archival_deadline_end']), ('2026-12-01', None))
        self.assertEqual(project['prod_coor_supe_ids'], [bound, unbound['id']])
        got = self.client.project_get(ProjectGetInput(project=project['page_id']))
        assert isinstance(got, Done)
        self.assertEqual(got.result['project']['playdate_end'], '2026-11-20')
        self.assertEqual(got.result['project']['prod_coor_supe_ids'], [bound, unbound['id']])
        plain = next(p for p in result.result['projects'] if p['name'] == 'Example Show')
        self.assertEqual((plain['playdate_end'], plain['prod_coor_supe_ids']), (None, []))

    def test_project_code_is_the_registrys_and_notion_project_code_is_notions(self) -> None:
        from t6.registry import Registered, Rotation
        from t6.verbs import ProjectGetInput, ProjectListInput
        from t6.notion.generated import ProjectsRow
        from t6.notion.testing import select
        page = self.notion.add_page('projects', {ProjectsRow.notion_name('name'): title('Coded show'),
                                                 ProjectsRow.notion_name('project_code'): select('CODE')})
        registered = self.registry.register(actor='setup', idempotency_key='coded', notion_page_id=page['id'], code=None, rotation=Rotation.IN)
        assert isinstance(registered, Registered)
        result = self.client.project_list(ProjectListInput())
        assert isinstance(result, Done)
        project = next(p for p in result.result['projects'] if p['name'] == 'Coded show')
        self.assertEqual((project['project_code'], project['notion_project_code']), (None, 'CODE'))
        got = self.client.project_get(ProjectGetInput(project=registered.project.id))
        assert isinstance(got, Done)
        self.assertEqual((got.result['project']['project_code'], got.result['project']['notion_project_code']), (None, 'CODE'))

    def test_jo_reads_return_every_related_project(self) -> None:
        from t6.verbs import JoListInput
        second = self.notion.add_page('projects', {'Name': title('Second Show')})
        both = self.notion.add_page('job_orders', {JobOrdersRow.notion_name('name'): title('Two projects'),
                                                   JobOrdersRow.notion_name('projects'): relation(self.project['id'], second['id'])})
        result = self.client.jo_list(JoListInput())
        assert isinstance(result, Done)
        order = next(o for o in result.result['job_orders'] if o['page_id'] == both['id'])
        self.assertEqual(order['project_ids'], [self.project['id'], second['id']])
        self.assertEqual(order['project_id'], self.project['id'])
        single = next(o for o in result.result['job_orders'] if o['page_id'] == self.jo['id'])
        self.assertEqual(single['project_ids'], [self.project['id']])

    def test_departments_include_misc_only_on_request(self) -> None:
        from t6.verbs import DepartmentsInput
        self.notion.add_option('job_orders', 'Department', 'MISC')
        self.notion.add_option('job_orders', 'Department', 'AE')
        plain = self.client.departments(DepartmentsInput())
        with_misc = self.client.departments(DepartmentsInput(include_misc=True))
        assert isinstance(plain, Done) and isinstance(with_misc, Done)
        self.assertNotIn('MISC', [d['name'] for d in plain.result['departments']])
        self.assertIn('MISC', [d['name'] for d in with_misc.result['departments']])

    def test_studio_notices_return_bold_runs_beside_the_plain_text(self) -> None:
        from t6.verbs import StudioNoticesInput
        self.notion._data_sources['projects']['description'] = [
            {'type': 'text', 'plain_text': 'Upcoming Holidays and Leaves', 'annotations': {'bold': True}},
            {'type': 'text', 'plain_text': '\n- Nov 20 Ana on leave', 'annotations': {'bold': False}}]
        result = self.client.studio_notices(StudioNoticesInput())
        assert isinstance(result, Done)
        self.assertEqual(result.result['description'], 'Upcoming Holidays and Leaves\n- Nov 20 Ana on leave')
        self.assertEqual(result.result['runs'], [{'text': 'Upcoming Holidays and Leaves', 'bold': True},
                                                 {'text': '\n- Nov 20 Ana on leave', 'bold': False}])

    def test_deliverable_list_declared_schema_describes_the_reply(self) -> None:
        from t6.verbs import DeliverableListInput, DeliverableListOutput
        from t6.deliverable import DeliverableStore
        from t6.registry import Registered
        from t6.verbs import Preview
        project = self.registry.register(actor='setup', idempotency_key='schema-project', notion_page_id=self.project['id'])
        assert isinstance(project, Registered)
        store = DeliverableStore(self.registry, PreviewTokens('key'))
        values = {'template': 'film', 'project': project.project.id, 'code': 'TLR', 'episode': None, 'due': None, 'air': None}
        preview = store.define(values, actor='setup', idempotency_key='schema-deliverable')
        assert isinstance(preview, Preview)
        store.define(values, actor='setup', idempotency_key='schema-deliverable', confirm=preview.token)
        result = self.client.deliverable_list(DeliverableListInput(project=project.project.id))
        assert isinstance(result, Done)
        parsed = DeliverableListOutput.model_validate(result.result)
        self.assertEqual(set(parsed.deliverables[0].model_dump()), set(result.result['deliverables'][0]))

    def test_deliverable_candidates_lists_notion_only_pages_with_name_status_and_class(self) -> None:
        from t6.verbs import DeliverableCandidatesInput, DeliverableCandidatesOutput
        row = DeliverablesRow
        page = self.notion.add_page('deliverables', {row.notion_name('name'): title('Client Corp Finale'), row.notion_name('status'): select('Ready'),
                                                     row.notion_name('deliverable_class'): select('Master'), row.notion_name('projects'): relation(self.project['id'])})
        self.notion.add_page('deliverables', {row.notion_name('name'): title('Elsewhere'), row.notion_name('projects'): relation(self.notion.add_page('projects', {'Name': title('Other')})['id'])})
        granted = Client(self.client.base_url, 'unmasked-secret')
        result = granted.deliverable_candidates(DeliverableCandidatesInput(project=self.project['id']))
        assert isinstance(result, Done)
        DeliverableCandidatesOutput.model_validate(result.result)
        by_page = {c['page_id']: c for c in result.result['candidates']}
        self.assertEqual(set(by_page[page['id']]), {'page_id', 'name', 'status', 'deliverable_class'})
        self.assertEqual((by_page[page['id']]['name'], by_page[page['id']]['status'], by_page[page['id']]['deliverable_class']), ('Client Corp Finale', 'Ready', 'Master'))
        self.assertEqual(sorted(c['name'] for c in result.result['candidates']), ['Client Corp Finale', 'Trailer'])

    def test_deliverable_candidates_names_are_masked_unless_unmasked(self) -> None:
        from t6.verbs import DeliverableCandidatesInput
        row = DeliverablesRow
        self.notion.add_page('deliverables', {row.notion_name('name'): title('Client Corp Finale'), row.notion_name('projects'): relation(self.project['id'])})
        masked = self.client.deliverable_candidates(DeliverableCandidatesInput(project=self.project['id']))
        assert isinstance(masked, Done)
        self.assertNotIn('Client Corp', str(masked.result))
        self.assertEqual(len(masked.result['candidates']), 2)

    def test_deliverable_candidates_includes_registered_pages_once_and_leaves_list_alone(self) -> None:
        from t6.verbs import DeliverableCandidatesInput, DeliverableListInput
        project = self.registry.register(actor='setup', idempotency_key='cand-project', notion_page_id=self.project['id'])
        from t6.registry import Registered
        assert isinstance(project, Registered)
        result = self.client.deliverable_candidates(DeliverableCandidatesInput(project=project.project.id))
        assert isinstance(result, Done)
        self.assertEqual([c['name'] for c in result.result['candidates']], ['Trailer'])
        listed = self.client.deliverable_list(DeliverableListInput(project=project.project.id))
        assert isinstance(listed, Done)
        self.assertEqual(listed.result, {'deliverables': []})

    def test_project_notes_returns_dated_lines(self) -> None:
        from t6.verbs import ProjectListInput, ProjectNotesInput
        from t6.notion.generated import ProjectsRow
        self.notion.add_page('projects', {ProjectsRow.notion_name('name'): title('Notes show'), ProjectsRow.notion_name('pm_notes'): rich_text('As of Nov 1, 2026\n- Waiting kay client\nAs of 2026-11-09: Received na')})
        projects = self.client.project_list(ProjectListInput())
        assert isinstance(projects, Done)
        page = next(p['page_id'] for p in projects.result['projects'] if p['name'] == 'Notes show')
        result = self.client.project_notes(ProjectNotesInput(project=page))
        assert isinstance(result, Done)
        self.assertEqual(result.result['lines'], [{'as_of': '2026-11-01', 'text': 'Waiting kay client'}, {'as_of': '2026-11-09', 'text': 'Received na'}])

    def test_concern_notes_scan_uses_masked_llm_and_records_change(self) -> None:
        from t6.llm.testing import FakeProvider
        from t6.server.llm import LLMService
        from t6.registry import Registered
        from t6.verbs import ConcernScanInput, ConcernChangesInput
        from t6.notion.generated import ProjectsRow
        provider = FakeProvider().reply('{"choice":"new","quote":"Waiting on [person 1]","waiting_on":"[person 1]"}')
        self.api.llm_service = LLMService({'fake': provider}, 'fake', Registry(people=[Person(name='Client Corp')]))
        page = self.notion.add_page('projects', {'Name': title('Scan'), ProjectsRow.notion_name('pm_notes'): rich_text('As of 2026-11-01\nWaiting on Client Corp')})
        project = self.registry.register(actor='setup', idempotency_key='scan-project', notion_page_id=page['id'])
        assert isinstance(project, Registered)
        owner = 't6n_' + '1' * 32
        with self.registry._transaction() as db:
            db.execute("INSERT INTO project_person_roles VALUES (?, ?, 'coordinator')", (project.project.id, owner))
        operator = Client(self.client.base_url, 'operator')
        result = operator.concern_scan(ConcernScanInput(project=project.project.id, owner=owner))
        assert isinstance(result, Done)
        changes = operator.concern_changes(ConcernChangesInput(project=project.project.id, since='2020-01-01'))
        assert isinstance(changes, Done)
        self.assertEqual(changes.result['changes'][0]['quote'], 'Waiting on Client Corp')
        self.assertNotIn('Client Corp', str(provider.calls[0].messages))

    def test_concern_raise_owner_must_coordinate_the_project(self) -> None:
        from t6.registry import Registered
        from t6.verbs import ConcernRaiseInput
        page = self.notion.add_page('projects', {'Name': title('Raise')})
        project = self.registry.register(actor='setup', idempotency_key='raise-project', notion_page_id=page['id'])
        assert isinstance(project, Registered)
        owner, other = 't6n_' + '1' * 32, 't6n_' + '2' * 32
        with self.registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES (?, 'staff', '[]', '[]', NULL, 0, 1)", (other,))
            db.execute("INSERT INTO project_person_roles VALUES (?, ?, 'coordinator')", (project.project.id, owner))
        operator = Client(self.client.base_url, 'operator')

        def raised(person: str) -> object:
            return operator.concern_raise(ConcernRaiseInput(project=project.project.id, owner=person, waiting_on='client', anchor='Waiting on client', as_of='2026-11-01', idempotency_key='raise-' + person))

        refused = raised(other)
        assert isinstance(refused, Refused)
        self.assertEqual(refused.rule, 'owner')
        with patch.dict(os.environ, {'T6_PREVIEW_SIGNING_KEY': 'key'}):
            self.assertIsInstance(raised(owner), Preview)

    def test_raise_at_psr_lists_quiet_concerns_only_and_is_masked_for_a_service(self) -> None:
        from t6.registry import Registered
        from t6.verbs import ConcernListInput
        page = self.notion.add_page('projects', {'Name': title('Psr')})
        project = self.registry.register(actor='setup', idempotency_key='psr-project', notion_page_id=page['id'])
        assert isinstance(project, Registered)
        owner = 't6n_' + '1' * 32
        with self.registry._transaction() as db:
            for concern, state, opened in (('t6c_old_open', 'open', 0.0), ('t6c_quiet', 'quiet', 0.0), ('t6c_expired', 'expired', 0.0)):
                db.execute("INSERT INTO concerns (id, project_id, owner_id, waiting_on, anchor, as_of, opened_at, evidence_at, state) VALUES (?, ?, ?, 'Client Corp', 'Waiting on Client Corp', '2026-11-01', ?, 0, ?)", (concern, project.project.id, owner, opened, state))
        listed = self.client.concern_list(ConcernListInput(raise_at_psr=True, project=project.project.id))
        assert isinstance(listed, Done)
        self.assertEqual([c['id'] for c in listed.result['concerns']], ['t6c_quiet'])
        row = listed.result['concerns'][0]
        self.assertEqual((row['label'], row['owner_id']), ('no news for 2 weeks', owner))
        self.assertNotIn('Client Corp', str(row))
        self.assertIsInstance(self.client.concern_list(ConcernListInput(project=project.project.id)), Refused)

    def test_concern_raise_may_leave_owner_and_waiting_on_empty_unless_a_person_raises(self) -> None:
        from t6.registry import Registered
        from t6.verbs import ConcernRaiseInput
        page = self.notion.add_page('projects', {'Name': title('Empty')})
        project = self.registry.register(actor='setup', idempotency_key='empty-project', notion_page_id=page['id'])
        assert isinstance(project, Registered)
        operator = Client(self.client.base_url, 'operator')
        with patch.dict(os.environ, {'T6_PREVIEW_SIGNING_KEY': 'key'}):
            flag = operator.concern_raise(ConcernRaiseInput(project=project.project.id, anchor='No editor is named', as_of='2026-11-16', source='flag_rule', idempotency_key='flag'))
            self.assertIsInstance(flag, Preview)
            person = operator.concern_raise(ConcernRaiseInput(project=project.project.id, anchor='No editor is named', as_of='2026-11-16', idempotency_key='person'))
        assert isinstance(person, Refused)
        self.assertEqual(person.rule, 'input')

    def seed_concerns(self) -> str:
        from t6.registry import Registered
        page = self.notion.add_page('projects', {'Name': title('Open')})
        project = self.registry.register(actor='setup', idempotency_key='open-project', notion_page_id=page['id'])
        assert isinstance(project, Registered)
        with self.registry._transaction() as db:
            for concern, state, opened, source in (('t6c_new', 'open', 300.0, 'flag_rule'), ('t6c_old', 'quiet', 100.0, 'person'), ('t6c_gone', 'closed', 0.0, 'person'), ('t6c_mid', 'expired', 200.0, 'pm_notes_read')):
                db.execute("INSERT INTO concerns (id, project_id, owner_id, waiting_on, anchor, text, text_key, source, as_of, opened_at, evidence_at, state) VALUES (?, ?, NULL, '', 'Waiting on Client Corp', 'Client Corp has not replied', ?, ?, '2026-11-01', ?, 0, ?)", (concern, project.project.id, concern, source, opened, state))
        return project.project.id

    def test_concern_list_open_returns_every_open_state_oldest_first(self) -> None:
        from t6.verbs import ConcernListInput
        project = self.seed_concerns()
        listed = Client(self.client.base_url, 'unmasked-secret').concern_list(ConcernListInput(open=True, project=project))
        assert isinstance(listed, Done)
        rows = listed.result['concerns']
        self.assertEqual([(c['id'], c['state'], c['source'], c['owner_id'], c['waiting_on']) for c in rows], [('t6c_old', 'quiet', 'person', None, ''), ('t6c_mid', 'expired', 'pm_notes_read', None, ''), ('t6c_new', 'open', 'flag_rule', None, '')])
        self.assertEqual(sorted(rows[0]), sorted(['id', 'project_id', 'owner_id', 'waiting_on', 'text', 'source', 'anchor', 'as_of', 'opened_at', 'evidence_at', 'state', 'settles_id', 'age_seconds', 'chronic']))
        self.assertTrue(all(c['chronic'] for c in rows))
        self.assertEqual(rows[0]['text'], 'Client Corp has not replied')

    def test_concern_list_needs_exactly_one_of_the_two_lists(self) -> None:
        from t6.verbs import ConcernListInput
        project = self.seed_concerns()
        for inputs in (ConcernListInput(project=project), ConcernListInput(raise_at_psr=True, open=True, project=project)):
            refused = self.client.concern_list(inputs)
            assert isinstance(refused, Refused)
            self.assertEqual(refused.rule, 'input')

    def test_raise_at_psr_reply_keeps_exactly_its_old_shape(self) -> None:
        from t6.verbs import ConcernListInput
        project = self.seed_concerns()
        listed = Client(self.client.base_url, 'unmasked-secret').concern_list(ConcernListInput(raise_at_psr=True, project=project))
        assert isinstance(listed, Done)
        row = listed.result['concerns'][0]
        self.assertEqual(sorted(row), ['age_seconds', 'id', 'label', 'owner_id', 'project_id', 'waiting_on'])
        self.assertEqual((row['id'], row['owner_id'], row['waiting_on'], row['label']), ('t6c_old', None, '', 'no news for 2 weeks'))

    def test_concern_text_is_masked_for_a_service_without_real_name_access(self) -> None:
        from t6.verbs import ConcernGetInput, ConcernListInput
        project = self.seed_concerns()
        masked = self.client.concern_list(ConcernListInput(open=True, project=project))
        assert isinstance(masked, Done)
        self.assertNotIn('Client Corp', str(masked.result))
        self.assertEqual(masked.result['concerns'][0]['id'], 't6c_old')
        record = self.client.concern_get(ConcernGetInput(concern='t6c_new'))
        assert isinstance(record, Done)
        self.assertNotIn('Client Corp', str(record.result))
        real = Client(self.client.base_url, 'unmasked-secret')
        for result in (real.concern_list(ConcernListInput(open=True, project=project)), real.concern_get(ConcernGetInput(concern='t6c_new'))):
            assert isinstance(result, Done)
            self.assertIn('Client Corp has not replied', str(result.result))

    def test_the_three_main_reads_are_masked_for_the_mcp_service(self) -> None:
        import json
        from t6.registry import Registered
        from t6.notion.generated import ProjectsRow
        from t6.server.app import Request
        page = self.notion.add_page('projects', {'Name': title('Masked'), ProjectsRow.notion_name('pm_notes'): rich_text('As of 2026-11-01\nWaiting on Client Corp')})
        project = self.registry.register(actor='setup', idempotency_key='masked-project', notion_page_id=page['id'])
        assert isinstance(project, Registered)
        with self.registry._transaction() as db:
            db.execute("INSERT INTO concerns (id, project_id, owner_id, waiting_on, anchor, as_of, opened_at, evidence_at, state) VALUES ('t6c_m', ?, ?, 'Client Corp', 'Waiting on Client Corp', '2026-11-01', 0, 0, 'quiet')", (project.project.id, 't6n_' + '1' * 32))
            db.execute("INSERT INTO concern_pm_changes (project_id, concern_id, action, quote, at) VALUES (?, 't6c_m', 'raised', 'Waiting on Client Corp', 4000000000.0)", (project.project.id,))
        for path, body in (('/v1/concern/list', {'raise_at_psr': True, 'project': project.project.id}),
                           ('/v1/concern/changes', {'project': project.project.id, 'since': '2020-01-01'}),
                           ('/v1/project/notes', {'project': page['id']})):
            with self.subTest(path):
                def call(secret: str, header: bool, path: str = path, body: Any = body) -> str:
                    headers = {'Authorization': 'Bearer ' + secret, **({'X-T6-MCP': '1'} if header else {})}
                    return json.dumps(self.api(Request('POST', path, headers, json.dumps(body).encode())).body)
                self.assertNotIn('Client Corp', call('mcp-secret', False))
                self.assertNotIn('Client Corp', call('mcp-secret', True))
                self.assertIn('Client Corp', call('operator', True))
                self.assertIn('Client Corp', call('operator', False))

    def seed_drive(self, label: str) -> str:
        """Register a Drive the way a person does: `drive add`, preview then confirm."""
        from t6.drive_verbs import DriveVerbs
        verbs = DriveVerbs(self.registry, PreviewTokens('seed-signing'))
        values = {'label': label}
        preview = verbs.write('add', values, actor='setup', idempotency_key='seed-' + label)
        assert isinstance(preview, Preview)
        done = verbs.write('add', values, actor='setup', idempotency_key='seed-' + label, confirm=preview.token)
        assert isinstance(done, Done)
        return str(done.result['drive_ids'][0])

    def test_drive_reads_are_called_through_the_client(self) -> None:
        from t6.verbs import DriveGetInput, DriveListInput
        drive = self.seed_drive('Example master')
        listed = self.client.drive_list(DriveListInput(q='example'))
        assert isinstance(listed, Done)
        self.assertEqual([d['id'] for d in listed.result['drives']], [drive])
        got = self.client.drive_get(DriveGetInput(drive=drive))
        assert isinstance(got, Done)
        self.assertEqual(got.result['drive']['label'], 'Example master')

    def test_drive_reads_are_masked_for_the_mcp_service(self) -> None:
        import json
        from t6.server.app import Request
        drive = self.seed_drive('Client Corp master')
        for path, body in (('/v1/drive/get', {'drive': drive}), ('/v1/drive/list', {})):
            with self.subTest(path):
                def call(secret: str, header: bool, path: str = path, body: Any = body) -> str:
                    headers = {'Authorization': 'Bearer ' + secret, **({'X-T6-MCP': '1'} if header else {})}
                    return json.dumps(self.api(Request('POST', path, headers, json.dumps(body).encode())).body)
                self.assertNotIn('Client Corp', call('mcp-secret', False))
                self.assertNotIn('Client Corp', call('mcp-secret', True))
                self.assertIn('Client Corp', call('operator', True))
                self.assertIn('Client Corp', call('operator', False))

    def test_project_flags_are_read_without_ids_or_age(self) -> None:
        from t6.verbs import ProjectFlagsInput
        result = self.client.project_flags(ProjectFlagsInput(project=self.project['id']))
        assert isinstance(result, Done)
        self.assertIn({'kind': 'data_gap', 'field': 'health', 'detail': 'missing health update'}, result.result['flags'])
        self.assertTrue(all('id' not in flag and 'age' not in flag for flag in result.result['flags']))

    def project_with(self, name: str, status: str | None = None, **properties: Any) -> dict[str, Any]:
        from t6.notion.generated import ProjectsRow
        values = {ProjectsRow.notion_name('name'): title(name), **{ProjectsRow.notion_name(key): value for key, value in properties.items()}}
        if status is not None:
            values[ProjectsRow.notion_name('status')] = select(status)
        return self.notion.add_page('projects', values)

    def test_project_reads_return_the_results_of_the_status_rules(self) -> None:
        from t6.verbs import ProjectGetInput, ProjectListInput
        expected = {
            '🚨 In-Progress': ('in-progress', 'in_progress', True, ['playdate', 'submission_deadline']),
            'in-progress': ('in-progress', 'in_progress', True, ['playdate', 'submission_deadline']),
            '🏎. In-Progress TVC': ('in-progress tvc', 'in_progress', True, ['playdate', 'submission_deadline']),
            'Airing': ('airing', 'in_progress', True, ['playdate', 'submission_deadline']),
            '🏋️ Standby': ('standby', 'on_hold', True, []),
            '📦 Archiving': ('archiving', 'on_hold', True, ['archival_deadline']),
            '🌱 Leads': ('leads', 'incoming', True, ['playdate']),
            'Lead': ('lead', 'incoming', True, ['playdate']),
            'Negotiations': ('negotiations', 'incoming', True, ['playdate']),
            'Proposal Made': ('proposal made', 'incoming', True, ['playdate']),
            'Archived': ('archived', None, False, []),
            'Lost': ('lost', None, False, []),
            'Something else': ('something else', None, True, []),
        }
        for status in expected:
            self.project_with('Status ' + status, status)
        result = self.client.project_list(ProjectListInput())
        assert isinstance(result, Done)
        for status, (key, group, snapshotted, dates) in expected.items():
            with self.subTest(status=status):
                project = next(p for p in result.result['projects'] if p['name'] == 'Status ' + status)
                self.assertEqual((project['status_key'], project['status_group'], project['snapshotted'], project['relevant_dates']),
                                 (key, group, snapshotted, dates))
                got = self.client.project_get(ProjectGetInput(project=project['page_id']))
                assert isinstance(got, Done)
                self.assertEqual(got.result['project']['status_group'], group)
        plain = next(p for p in result.result['projects'] if p['name'] == 'Example Show')
        self.assertEqual((plain['status_key'], plain['status_group'], plain['snapshotted'], plain['missing_roles'], plain['relevant_dates']), ('', None, True, [], []))

    def test_project_list_filters_on_status_group(self) -> None:
        from t6.verbs import ProjectListInput
        for status in ('In-Progress', 'Airing', 'Standby', 'Leads', 'Lost'):
            self.project_with('Group ' + status, status)
        for group, names in (('in_progress', {'Group In-Progress', 'Group Airing'}), ('on_hold', {'Group Standby'}), ('incoming', {'Group Leads'})):
            result = self.client.project_list(ProjectListInput(status_group=group))  # type: ignore[arg-type]
            assert isinstance(result, Done)
            self.assertEqual({p['name'] for p in result.result['projects']}, names)
        with self.assertRaises(APIError) as refused:
            self.client.call('project', 'list', ProjectListInput.model_construct(status_group='bogus'))
        self.assertEqual(refused.exception.status, 422)

    def test_project_missing_roles_are_listed_only_for_in_progress_tvc_and_airing(self) -> None:
        from t6.verbs import ProjectListInput
        for status in ('In-Progress', 'In-Progress TVC', 'Airing', 'Standby', 'Archiving', 'Leads'):
            self.project_with('Roles ' + status, status, editors=relation(self.person['id']), director=relation(self.person['id']))
        result = self.client.project_list(ProjectListInput())
        assert isinstance(result, Done)
        rest = ['assistant_editor_ids', 'online_editor_ids', 'colorist_ids', 'sound_tech_ids', 'post_coordinator_ids', 'cinematographer_ids']
        for status in ('In-Progress', 'In-Progress TVC', 'Airing', 'Standby', 'Archiving', 'Leads'):
            with self.subTest(status=status):
                project = next(p for p in result.result['projects'] if p['name'] == 'Roles ' + status)
                self.assertEqual(project['missing_roles'], rest if status in ('In-Progress', 'In-Progress TVC', 'Airing') else [])

    def test_project_health_rank_and_blank_health(self) -> None:
        from t6.verbs import ProjectListInput
        order = ['Critical', '🟡 At Risk', 'on track', 'Incoming', 'Weird', None]
        for number, health in enumerate(order):
            self.project_with(f'Health {number}', 'Standby', project_health=select(health))
        result = self.client.project_list(ProjectListInput())
        assert isinstance(result, Done)
        ranked = [next(p for p in result.result['projects'] if p['name'] == f'Health {n}') for n in range(len(order))]
        self.assertEqual([p['health_rank'] for p in ranked], [0, 1, 2, 3, 4, 5])
        self.assertEqual([p['health_missing'] for p in ranked], [False] * 5 + [True])

    def test_health_results_read_project_health_and_not_the_health_select(self) -> None:
        from t6.verbs import ProjectFlagsInput, ProjectGetInput
        page = self.project_with('Two selects', 'Standby', health=select('Critical'), playdate=date('2027-06-01'))
        got = self.client.project_get(ProjectGetInput(project=page['id']))
        assert isinstance(got, Done)
        self.assertEqual((got.result['project']['health_missing'], got.result['project']['health_rank']), (True, 5))
        flagged = self.client.project_flags(ProjectFlagsInput.model_validate({'project': page['id'], 'as_of': '2026-11-16'}))
        assert isinstance(flagged, Done)
        self.assertIn({'kind': 'data_gap', 'field': 'health', 'detail': 'missing health update'}, flagged.result['flags'])

    def test_project_flags_take_several_projects_each_with_its_stated_dates(self) -> None:
        from t6.verbs import ProjectFlagsInput
        late = self.project_with('Late show', 'In-Progress', playdate=date('2026-11-10'), submission_deadline=date('2026-12-20'), project_health=select('On track'))
        blank = self.project_with('Blank show', 'Leads', project_health=select('On track'))
        result = self.client.project_flags(ProjectFlagsInput.model_validate({'as_of': '2026-11-16', 'projects': [
            {'project': late['id']}, {'project': blank['id'], 'stated': {'playdate': '2026-12-01'}}, {'project': 'nobody'}]}))
        assert isinstance(result, Done), result
        first, second, third = result.result['projects']
        self.assertEqual((first['project'], first['found'], first['flags']), (late['id'], True, [{'kind': 'overdue', 'field': 'playdate', 'detail': 'overdue', 'days': 6}]))
        self.assertEqual(first['missing_roles'], self.client.project_flags(ProjectFlagsInput.model_validate({'project': late['id'], 'as_of': '2026-11-16'})).result['missing_roles'])  # type: ignore[union-attr]
        self.assertEqual(second['flags'], [{'kind': 'upcoming', 'field': 'playdate', 'detail': 'upcoming', 'days': 15}])
        self.assertEqual(third, {'project': 'nobody', 'found': False})
        for bad in ({}, {'project': late['id'], 'projects': [{'project': blank['id']}]}, {'projects': [{'project': blank['id']}], 'stated': {'playdate': None}}):
            with self.assertRaises(ValueError):
                ProjectFlagsInput.model_validate(bad)

    def test_project_flags_judge_on_as_of_and_lead_warning_days(self) -> None:
        from t6.verbs import ProjectFlagsInput
        late = self.project_with('Late show', 'In-Progress', playdate=date('2026-11-10'), submission_deadline=date('2026-12-20'), project_health=select('On track'))
        soon = self.project_with('Soon show', 'Leads', playdate=date('2026-12-01'), project_health=select('On track'))
        def flags(page: dict[str, Any], **more: Any) -> list[dict[str, Any]]:
            result = self.client.project_flags(ProjectFlagsInput.model_validate({'project': page['id'], **more}))
            assert isinstance(result, Done)
            return list(result.result['flags'])
        self.assertEqual(flags(late, as_of='2026-11-16'), [{'kind': 'overdue', 'field': 'playdate', 'detail': 'overdue', 'days': 6}])
        self.assertEqual(flags(late, as_of='2026-11-10'), [])
        self.assertEqual(flags(soon, as_of='2026-11-16'), [{'kind': 'upcoming', 'field': 'playdate', 'detail': 'upcoming', 'days': 15}])
        self.assertEqual(flags(soon, as_of='2026-11-16', lead_warning_days=10), [])

    def test_project_flags_judge_a_stated_date_like_the_propertys_own(self) -> None:
        from t6.verbs import ProjectFlagsInput
        page = self.project_with('Notes show', 'In-Progress', playdate=date('2026-12-20'), project_health=select('On track'))
        def flags(**stated: Any) -> list[tuple[str, str]]:
            result = self.client.project_flags(ProjectFlagsInput.model_validate({'project': page['id'], 'as_of': '2026-11-16', 'stated': stated}))
            assert isinstance(result, Done)
            return [(f['kind'], f['field']) for f in result.result['flags']]
        self.assertEqual(flags(), [('data_gap', 'submission_deadline')])
        self.assertEqual(flags(submission_deadline='2026-12-25'), [])
        self.assertEqual(flags(submission_deadline='2026-11-01'), [('overdue', 'submission_deadline')])
        self.assertEqual(flags(submission_deadline=None), [])
        self.assertEqual(flags(playdate='2026-11-01'), [('data_gap', 'submission_deadline')])

    def test_project_flags_find_both_date_sanity_problems(self) -> None:
        from t6.verbs import ProjectFlagsInput
        page = self.project_with('Sanity show', 'In-Progress', playdate=date('2026-11-20'), submission_deadline=date('2026-11-10'),
                                 archival_deadline=date('2026-11-01'), project_health=select('On track'))
        result = self.client.project_flags(ProjectFlagsInput.model_validate({'project': page['id'], 'as_of': '2026-11-01'}))
        assert isinstance(result, Done)
        self.assertEqual(result.result['sanity'], [
            {'kind': 'submission_before_playdate', 'first': 'submission_deadline', 'second': 'playdate', 'first_date': '2026-11-10', 'second_date': '2026-11-20', 'from_notes': False},
            {'kind': 'archival_before_submission', 'first': 'archival_deadline', 'second': 'submission_deadline', 'first_date': '2026-11-01', 'second_date': '2026-11-10', 'from_notes': False}])
        stated = self.project_with('Stated show', 'Standby', playdate=date('2026-11-20'), project_health=select('On track'))
        result = self.client.project_flags(ProjectFlagsInput.model_validate({'project': stated['id'], 'stated': {'submission_deadline': '2026-11-10', 'archival_deadline': None}}))
        assert isinstance(result, Done)
        self.assertEqual([(p['kind'], p['from_notes']) for p in result.result['sanity']], [('submission_before_playdate', True)])
        quiet = self.project_with('Quiet show', 'Standby', playdate=date('2026-11-20'), submission_deadline=date('2026-11-25'), project_health=select('On track'))
        result = self.client.project_flags(ProjectFlagsInput(project=quiet['id']))
        assert isinstance(result, Done)
        self.assertEqual(result.result['sanity'], [])

    def test_project_flags_return_the_missing_roles(self) -> None:
        from t6.verbs import ProjectFlagsInput
        page = self.project_with('Roles show', 'Airing', editors=relation(self.person['id']))
        result = self.client.project_flags(ProjectFlagsInput(project=page['id']))
        assert isinstance(result, Done)
        self.assertNotIn('editor_ids', result.result['missing_roles'])
        self.assertEqual(len(result.result['missing_roles']), 7)
        standby = self.project_with('Idle show', 'Standby')
        result = self.client.project_flags(ProjectFlagsInput(project=standby['id']))
        assert isinstance(result, Done)
        self.assertEqual(result.result['missing_roles'], [])

    def test_jo_reads_return_the_status_group_active_and_health_flags(self) -> None:
        from t6.verbs import JoListInput, JoGetInput
        expected = {'Needs Intake Review': ('not_started', True), 'Pending': ('not_started', True), 'In progress': ('started', True),
                    'Needs Completion Review': ('started', True), 'Done': ('started', False), 'Cancelled': ('cancelled', False)}
        for status in expected:
            self.notion.add_page('job_orders', {JobOrdersRow.notion_name('name'): title('JO ' + status),
                                               JobOrdersRow.notion_name('status'): {'type': 'status', 'status': {'name': status}}})
        result = self.client.jo_list(JoListInput())
        assert isinstance(result, Done)
        for status, (group, active) in expected.items():
            with self.subTest(status=status):
                order = next(o for o in result.result['job_orders'] if o['name'] == 'JO ' + status)
                self.assertEqual((order['status_group'], order['active'], order['health_flags']), (group, active, []))
                self.assertIs(order['done'], status == 'Done')
                got = self.client.jo_get(JoGetInput(ref=order['page_id']))
                assert isinstance(got, Done)
                self.assertEqual((got.result['job_order']['status_group'], got.result['job_order']['active']), (group, active))
                self.assertIs(got.result['job_order']['done'], status == 'Done')
        blank = next(o for o in result.result['job_orders'] if o['page_id'] == self.jo['id'])
        self.assertEqual((blank['status_group'], blank['active'], blank['done']), (None, True, False))

    def test_jo_health_flags_ignore_emoji(self) -> None:
        from t6.verbs import JoListInput
        orders = JobOrdersRow
        for name, health in (('Both', {'type': 'formula', 'formula': {'type': 'string', 'string': '🛑 Blocked, 🕘 Delayed'}}),
                             ('Urgent', {'type': 'formula', 'formula': {'type': 'string', 'string': '🔥 URGENT'}})):
            self.notion.add_page('job_orders', {orders.notion_name('name'): title('Health ' + name), orders.notion_name('health'): health})
        result = self.client.jo_list(JoListInput())
        assert isinstance(result, Done)
        flags = {o['name']: o['health_flags'] for o in result.result['job_orders']}
        self.assertEqual((flags['Health Both'], flags['Health Urgent'], flags['Work']), (['Blocked', 'Delayed'], ['Urgent'], []))

    def test_jo_get_includes_t6_state_for_t6_reference(self) -> None:
        from t6.job_order import JobOrderStore
        from t6.notion.job_orders import JobOrders
        from t6.verbs import JoGetInput
        store = JobOrderStore(self.registry, PreviewTokens('key'))
        with self.notion.client() as notion:
            orders = JobOrders(notion, 'job_orders').list()
        plan = store.migrate(orders, actor='operator')
        migrated = store.migrate(orders, actor='operator', apply=True, digest=plan['digest'])
        jo_id = migrated['entries'][0]['t6_id']
        result = self.client.jo_get(JoGetInput(ref=jo_id))
        assert isinstance(result, Done)
        order = result.result['job_order']
        self.assertEqual(order['t6_id'], jo_id)
        self.assertEqual(order['state']['status'], 'Needs Intake Review')
        self.assertEqual(order['history'], [])
        self.assertEqual(order['notes'], [])

    def test_jo_get_before_the_notion_page_exists_still_returns_the_rule_results(self) -> None:
        from t6.verbs import JoGetInput
        with self.registry._transaction() as db:
            db.execute('INSERT INTO job_orders VALUES (?, ?, ?, ?, ?)', ('t6j_unbound', 't6p_one', None, None, '{"name": "Cut", "status": "Needs Intake Review"}'))
        result = self.client.jo_get(JoGetInput(ref='t6j_unbound'))
        assert isinstance(result, Done), result
        order = result.result['job_order']
        self.assertEqual((order['status_group'], order['active'], order['done'], order['health_flags'], order['project_ids']),
                         ('not_started', True, False, [], ['t6p_one']))

    def test_camera_enrich_requires_preview_and_confirm(self) -> None:
        from t6.verbs import CameraEnrichInput, Preview
        operator = Client(self.client.base_url, 'operator')
        inputs = CameraEnrichInput(page_id=self.camera['id'], fields={'name': 'Updated'})
        with patch.dict(os.environ, {'T6_PREVIEW_SIGNING_KEY': 'key'}):
            preview = operator.camera_enrich(inputs)
            assert isinstance(preview, Preview)
            self.assertEqual(self.notion.page(self.camera['id'])['properties']['Name']['title'][0]['plain_text'], 'Camera One')
            inputs.confirm = preview.token
            inputs.idempotency_key = preview.plan['idempotency_key']
            result = operator.camera_enrich(inputs)
        self.assertIsInstance(result, Done)
        self.assertEqual(self.notion.page(self.camera['id'])['properties']['Name']['title'][0]['plain_text'], 'Updated')

    def person_create(self, **fields: object) -> tuple[Client, object]:
        from t6.verbs import PersonCreateInput
        operator = Client(self.client.base_url, 'operator')
        inputs = PersonCreateInput.model_validate({'name': 'Placeholder Person', 'kind': 'client', **fields})
        preview = operator.person_create(inputs)
        if not isinstance(preview, Preview):
            return operator, preview
        inputs.confirm, inputs.idempotency_key = preview.token, preview.plan['idempotency_key']
        return operator, operator.person_create(inputs)

    def contact_pages(self) -> int:
        with self.notion.client() as client:
            return len(client.query_data_source('contacts'))

    def test_person_create_makes_one_page_and_one_person(self) -> None:
        from t6.verbs import PersonCreateInput
        before = self.contact_pages()
        with patch.dict(os.environ, {'T6_PREVIEW_SIGNING_KEY': 'key'}):
            operator = Client(self.client.base_url, 'operator')
            inputs = PersonCreateInput(name='Placeholder Person', kind='client', telegram_handle='@placeholder', telegram_id=77, note='Added from onboarding.', idempotency_key='create-1')
            preview = operator.person_create(inputs)
            assert isinstance(preview, Preview)
            self.assertEqual(self.contact_pages(), before)
            inputs.confirm = preview.token
            created = operator.person_create(inputs)
            assert isinstance(created, Done)
            person = created.result['person']
            self.assertEqual((person['name'], person['telegram_user_id'], person['telegram_handle'], person['kind']), ('Placeholder Person', 77, 'placeholder', 'client'))
            self.assertEqual(self.registry.bound_entity('notion', person['page_id']), person['t6_id'])
            again = operator.person_create(PersonCreateInput(name='Placeholder Person', kind='client', telegram_handle='@placeholder', telegram_id=77, note='Added from onboarding.', idempotency_key='create-1'))
            assert isinstance(again, Preview)
            inputs.confirm = again.token
            replay = operator.person_create(inputs)
            assert isinstance(replay, Done)
            self.assertEqual(replay.result, created.result)
            self.assertEqual(self.contact_pages(), before + 1)
            with self.registry._transaction() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM persons WHERE telegram_id = 77').fetchone()[0], 1)
            _, taken = self.person_create(name='Another Placeholder', telegram_id=77)
            assert isinstance(taken, Refused)
            self.assertEqual(taken.rule, 'telegram_id')
            _, reader = Client(self.client.base_url, 'secret'), Client(self.client.base_url, 'secret').person_create(PersonCreateInput(name='Placeholder', kind='client'))
            self.assertIsInstance(reader, Refused)

    def test_person_create_takes_freelancer_and_a_bad_kind_is_a_422(self) -> None:
        from urllib.error import HTTPError
        from urllib.request import Request, urlopen
        import json
        with patch.dict(os.environ, {'T6_PREVIEW_SIGNING_KEY': 'key'}):
            _, created = self.person_create(kind='freelancer', idempotency_key='create-freelancer')
            assert isinstance(created, Done)
            self.assertEqual(created.result['person']['kind'], 'freelancer')
        request = Request(f'http://127.0.0.1:{self.server.server_port}/v1/person/create', headers={'Authorization': 'Bearer operator'},
                          data=json.dumps({'name': 'Placeholder Person', 'kind': 'contractor'}).encode())
        with self.assertRaises(HTTPError) as error:
            urlopen(request)
        self.assertEqual(error.exception.code, 422)
        error.exception.close()

    def test_person_create_after_an_unclear_notion_failure_is_ambiguous_and_makes_no_second_page(self) -> None:
        from t6.verbs import Ambiguous, PersonCreateInput
        before = self.contact_pages()
        with patch.dict(os.environ, {'T6_PREVIEW_SIGNING_KEY': 'key'}):
            operator = Client(self.client.base_url, 'operator')
            inputs = PersonCreateInput(name='Placeholder Person', kind='client', note='Added from onboarding.', idempotency_key='create-2')
            preview = operator.person_create(inputs)
            assert isinstance(preview, Preview)
            inputs.confirm = preview.token
            self.notion.fail_next(500, code='internal_server_error', after=2)
            self.assertIsInstance(operator.person_create(inputs), Ambiguous)
            self.assertEqual(self.contact_pages(), before + 1)
            again = operator.person_create(PersonCreateInput(name='Placeholder Person', kind='client', note='Added from onboarding.', idempotency_key='create-2'))
            assert isinstance(again, Preview)
            inputs.confirm = again.token
            self.assertIsInstance(operator.person_create(inputs), Ambiguous)
            self.assertEqual(self.contact_pages(), before + 1)

    def test_person_create_refused_by_notion_frees_the_key(self) -> None:
        with patch.dict(os.environ, {'T6_PREVIEW_SIGNING_KEY': 'key'}):
            before = self.contact_pages()
            self.notion.fail_next(400, code='validation_error')
            _, refused = self.person_create(idempotency_key='create-3')
            assert isinstance(refused, Refused)
            self.assertEqual(refused.rule, 'notion')
            _, created = self.person_create(idempotency_key='create-3')
            self.assertIsInstance(created, Done)
            self.assertEqual(self.contact_pages(), before + 1)

    def bound_person(self, page: dict[str, Any], telegram_id: int | None = None) -> str:
        person_id = 't6n_' + ('%032x' % (len(page['id']) * 7 + (telegram_id or 0) + 1))
        with self.registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES (?, 'staff', '[]', '[]', ?, 0, 1)", (person_id, telegram_id))
            db.execute("INSERT INTO bindings (system, page_id, project_id, bound_at, active) VALUES ('notion', ?, ?, 'now', 1)", (page['id'].replace('-', ''), person_id))
        return person_id

    def set_telegram(self, person_id: str, telegram_id: int, key: str, *, fail: bool = False) -> object:
        from t6.verbs import PersonTelegramInput
        operator = Client(self.client.base_url, 'operator')
        inputs = PersonTelegramInput(person_id=person_id, telegram_id=telegram_id, idempotency_key=key)
        preview = operator.person_set_telegram(inputs)
        if not isinstance(preview, Preview):
            return preview
        inputs.confirm = preview.token
        if fail:
            self.notion.fail_next(500, code='internal_server_error')
        return operator.person_set_telegram(inputs)

    def registry_telegram(self, person_id: str) -> int | None:
        with self.registry._transaction() as db:
            telegram_id: int | None = db.execute('SELECT telegram_id FROM persons WHERE id = ?', (person_id,)).fetchone()[0]
            return telegram_id

    def audits(self, person_id: str) -> int:
        with self.registry._transaction() as db:
            count: int = db.execute("SELECT COUNT(*) FROM audit WHERE action = 'person_set_telegram' AND project_id = ?", (person_id,)).fetchone()[0]
            return count

    def test_person_set_telegram_writes_the_contact_and_reads_back(self) -> None:
        from t6.verbs import PersonGetInput, PersonListInput
        person_id = self.bound_person(self.person)
        with patch.dict(os.environ, {'T6_PREVIEW_SIGNING_KEY': 'key'}):
            done = self.set_telegram(person_id, 4242, 'tg-1')
            assert isinstance(done, Done)
            self.assertEqual(self.registry_telegram(person_id), 4242)
            got = self.client.person_get(PersonGetInput(person_id=person_id))
            assert isinstance(got, Done)
            self.assertEqual(got.result['person']['telegram_user_id'], 4242)
            listed = self.client.person_list(PersonListInput())
            assert isinstance(listed, Done)
            self.assertEqual({p['page_id']: p['telegram_user_id'] for p in listed.result['people']}[self.person['id']], 4242)
            self.assertIn('4242', str(self.notion.page(self.person['id'])['properties']))

    def test_person_set_telegram_replaces_an_id_the_same_person_holds(self) -> None:
        person_id = self.bound_person(self.person, 4242)
        with patch.dict(os.environ, {'T6_PREVIEW_SIGNING_KEY': 'key'}):
            self.assertIsInstance(self.set_telegram(person_id, 4242, 'tg-same'), Done)
            self.assertIsInstance(self.set_telegram(person_id, 4343, 'tg-new'), Done)
        self.assertEqual(self.registry_telegram(person_id), 4343)

    def test_person_set_telegram_refused_when_another_person_holds_the_id_and_writes_no_page(self) -> None:
        other = self.notion.add_page('contacts', {'Name': title('Ben Reyes')})
        self.bound_person(other, 5151)
        person_id = self.bound_person(self.person)
        with patch.dict(os.environ, {'T6_PREVIEW_SIGNING_KEY': 'key'}):
            refused = self.set_telegram(person_id, 5151, 'tg-held')
        assert isinstance(refused, Refused)
        self.assertEqual(refused.rule, 'telegram_id')
        self.assertNotIn('5151', str(self.notion.page(self.person['id'])['properties']))

    def test_person_set_telegram_notion_failure_is_ambiguous_and_a_retry_completes_once(self) -> None:
        from t6.verbs import Ambiguous
        person_id = self.bound_person(self.person)
        with patch.dict(os.environ, {'T6_PREVIEW_SIGNING_KEY': 'key'}):
            self.assertIsInstance(self.set_telegram(person_id, 4242, 'tg-2', fail=True), Ambiguous)
            self.assertIsNone(self.registry_telegram(person_id))
            self.assertEqual(self.audits(person_id), 0)
            done = self.set_telegram(person_id, 4242, 'tg-2')
            self.assertIsInstance(done, Done)
            self.assertEqual(self.registry_telegram(person_id), 4242)
            self.assertEqual(self.audits(person_id), 1)
            again = self.set_telegram(person_id, 4242, 'tg-2')
            self.assertIsInstance(again, Done)
            self.assertEqual(self.audits(person_id), 1)

    def test_person_set_telegram_without_a_contact_stays_registry_only(self) -> None:
        person_id = 't6n_' + '2' * 32
        with self.registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES (?, 'staff', '[]', '[]', NULL, 0, 1)", (person_id,))
        with patch.dict(os.environ, {'T6_PREVIEW_SIGNING_KEY': 'key'}):
            self.assertIsInstance(self.set_telegram(person_id, 4242, 'tg-3'), Done)
        self.assertEqual(self.registry_telegram(person_id), 4242)

    def test_project_related_reads_and_telegram_key(self) -> None:
        from t6.verbs import DeliverableListInput, JoFindInput, JoListInput, MeetingNotesListInput
        project = self.project['id']
        for result, key, name in (
            (self.client.jo_list(JoListInput(project=project)), 'job_orders', 'Work'),
            (self.client.jo_find(JoFindInput(telegram_key='-100:3')), 'job_orders', 'Work'),
            (self.client.meeting_notes_list(MeetingNotesListInput(project=project, since='2026-11-01')), 'meeting_notes', 'Notes'),
        ):
            self.assertIsInstance(result, Done)
            assert isinstance(result, Done)
            self.assertEqual(result.result[key][0]['name'], name)
        result = self.client.meeting_notes_list(MeetingNotesListInput(project=project, since='2026-12-01'))
        assert isinstance(result, Done)
        self.assertEqual(result.result['meeting_notes'], [])
        result = self.client.meeting_notes_list(MeetingNotesListInput(project=project))
        assert isinstance(result, Done)
        self.assertEqual(result.result['meeting_notes'][0]['body'][0]['text'], 'Meeting details')
        self.assertTrue(result.result['meeting_notes'][0]['body'][0]['spans'][0]['bold'])

    def test_deliverable_reads_use_t6_identity(self) -> None:
        from t6.deliverable import DeliverableStore
        from t6.registry import Registered
        from t6.verbs import DeliverableGetInput, DeliverableListInput, Preview
        project = self.registry.register(actor='setup', idempotency_key='deliverable-project', notion_page_id=self.project['id'])
        assert isinstance(project, Registered)
        store = DeliverableStore(self.registry, PreviewTokens('key'))
        values = {'template': 'film', 'project': project.project.id, 'code': 'TLR', 'episode': None, 'due': None, 'air': None}
        preview = store.define(values, actor='setup', idempotency_key='deliverable')
        assert isinstance(preview, Preview)
        done = store.define(values, actor='setup', idempotency_key='deliverable', confirm=preview.token)
        assert isinstance(done, Done)
        identifier = done.result['deliverable_id']
        listed = self.client.deliverable_list(DeliverableListInput(project=project.project.id))
        found = self.client.deliverable_get(DeliverableGetInput(ref=identifier))
        assert isinstance(listed, Done) and isinstance(found, Done)
        self.assertEqual(listed.result['deliverables'][0]['t6_id'], identifier)
        self.assertEqual(found.result['deliverable']['id'], identifier)

    def add_contacts(self, *rows: tuple[str, str | None]) -> dict[str, str]:
        from t6.notion.generated import ContactsRow
        return {name + str(n): self.notion.add_page('contacts', {ContactsRow.notion_name('name'): title(name),
                ContactsRow.notion_name('telegram_user_id'): rich_text(telegram)})['id'] for n, (name, telegram) in enumerate(rows)}

    def contact_queries(self) -> int:
        return sum(1 for r in self.notion.requests if r.method == 'POST' and r.url.path.endswith('/query') and 'contacts' in r.url.path)

    def find(self, **fields: Any) -> dict[str, Any]:
        from t6.verbs import PersonFindInput
        result = self.client.person_find(PersonFindInput(**fields))
        assert isinstance(result, Done), result
        found: dict[str, Any] = result.result
        return found

    def test_person_find_suggest_gives_up_to_3_candidates_after_an_exact_match(self) -> None:
        self.add_contacts(('Tomas Reyes', None), ('Tomas Reyez', None), ('Tomasa Reyes', None), ('Tomas Reyess', None), ('Quinn Okafor', None))
        plain = self.find(mention='Tomas Reyes')
        self.assertEqual((plain['match']['name'], plain['method'], [c['name'] for c in plain['candidates']]), ('Tomas Reyes', 'normalized', ['Tomas Reyes']))
        suggested = self.find(mention='Tomas Reyes', suggest=True)
        self.assertEqual({k: v for k, v in suggested.items() if k != 'candidates'}, {k: v for k, v in plain.items() if k != 'candidates'})
        self.assertEqual(len(suggested['candidates']), 3)
        self.assertEqual(suggested['candidates'][0]['name'], 'Tomas Reyes')
        self.assertTrue(all(c['score'] >= 0.60 for c in suggested['candidates']))

    def test_person_find_without_the_new_inputs_keeps_its_reply(self) -> None:
        self.assertEqual(self.find(mention='Ana Cruz'), {
            'match': {**self.find(mention='Ana Cruz')['match']}, 'method': 'normalized', 'score': 1.0, 'needs_review': False, 'reason': None,
            'candidates': [{**self.find(mention='Ana Cruz')['match'], 'score': 1.0}]})
        self.assertEqual(self.find(mention='Ana Cruz', suggest=False, prefer_telegram=False), self.find(mention='Ana Cruz'))

    def test_person_find_prefer_telegram_picks_the_namesake_that_has_a_telegram_id(self) -> None:
        ids = self.add_contacts(('Lena Moreau', None), ('Lena Moreau', '7001'))
        self.assertTrue(self.find(mention='Lena Moreau')['needs_review'])
        preferred = self.find(mention='Lena Moreau', prefer_telegram=True)
        self.assertEqual((preferred['match']['page_id'], preferred['needs_review']), (ids['Lena Moreau1'], False))
        self.assertEqual(self.find(mention='Lena Moreau', prefer_telegram=True, suggest=True)['candidates'][0]['page_id'], ids['Lena Moreau1'])

    def test_person_find_prefer_telegram_falls_through_a_tie_in_the_preferred_set(self) -> None:
        self.add_contacts(('Lena Moreau', '7001'), ('Lena Moreau', '7002'), ('Lena Moreau', None))
        found = self.find(mention='Lena Moreau', prefer_telegram=True)
        self.assertEqual((found['match'], found['needs_review'], len(found['candidates'])), (None, True, 3))

    def test_person_find_mentions_answers_each_name_in_order_with_one_contact_read(self) -> None:
        self.add_contacts(('Tomas Reyes', None), ('Lena Moreau', '7001'))
        before = self.contact_queries()
        found = self.find(mentions=['Lena Moreau', '  ', 'Tomas Reyes', 'Zzzzz Qqqqq'], suggest=True)
        self.assertEqual(self.contact_queries() - before, 1)
        results = found['results']
        self.assertEqual([r['match']['name'] if r['match'] else None for r in results], ['Lena Moreau', None, 'Tomas Reyes', None])
        self.assertEqual((results[1]['method'], results[1]['candidates']), ('none', []))
        self.assertEqual(results[0], self.find(mention='Lena Moreau', suggest=True))

    def test_person_find_refuses_a_bad_mention_list(self) -> None:
        from t6.verbs import PersonFindInput
        for fields in ({}, {'mention': 'Ana Cruz', 'mentions': ['Ana Cruz']}, {'mentions': ['Ana Cruz'] * 51}):
            with self.subTest(fields=fields):
                result = self.client.person_find(PersonFindInput(**fields))
                self.assertIsInstance(result, Refused)
                assert isinstance(result, Refused)
                self.assertEqual(result.rule, 'input')
        self.assertEqual(len(self.find(mentions=['Ana Cruz'] * 50)['results']), 50)

    def test_person_list_registered_filters_on_the_t6_id(self) -> None:
        from t6.verbs import PersonListInput
        ids = self.add_contacts(('Registered One', None), ('Registered Two', None), ('Unregistered Three', None))
        for n, name in enumerate(('Registered One0', 'Registered Two1'), start=1):
            self.bound_person({'id': ids[name]}, n)
        def names(**fields: Any) -> set[str]:
            result = self.client.person_list(PersonListInput(**fields))
            assert isinstance(result, Done)
            return {p['name'] for p in result.result['people'] if p['name'] != 'Ana Cruz'}
        self.assertEqual(names(registered=True), {'Registered One', 'Registered Two'})
        self.assertEqual(names(registered=False), {'Unregistered Three'})
        self.assertEqual(names(), {'Registered One', 'Registered Two', 'Unregistered Three'})

    def test_person_list_kind_filters_on_the_registered_kind(self) -> None:
        from t6.verbs import PersonListInput
        ids = self.add_contacts(('Staff One', None), ('Freelancer Two', None), ('Client Three', None), ('Unregistered Four', None))
        for n, (name, kind) in enumerate((('Staff One0', 'staff'), ('Freelancer Two1', 'freelancer'), ('Client Three2', 'client')), start=1):
            person_id = self.bound_person({'id': ids[name]}, n)
            with self.registry._transaction() as db:
                db.execute('UPDATE persons SET kind = ? WHERE id = ?', (kind, person_id))
        def names(**fields: Any) -> set[str]:
            result = self.client.person_list(PersonListInput(**fields))
            assert isinstance(result, Done)
            return {p['name'] for p in result.result['people'] if p['name'] != 'Ana Cruz'}
        self.assertEqual(names(kind='freelancer'), {'Freelancer Two'})
        self.assertEqual(names(kind='client'), {'Client Three'})
        self.assertEqual(names(kind='vendor'), set())
        self.assertEqual(names(kind='staff', registered=True), {'Staff One'})
        self.assertEqual(names(), {'Staff One', 'Freelancer Two', 'Client Three', 'Unregistered Four'})
        with self.assertRaises(APIError) as refused:
            self.client.call('person', 'list', PersonListInput.model_construct(kind='contractor'))
        self.assertEqual(refused.exception.status, 422)

    def test_get_find_and_studio_reads(self) -> None:
        from t6.verbs import (ProjectGetInput, ProjectResolveInput, JoGetInput,
                              PersonFindInput, PersonGetInput, PersonListInput,
                              CameraGetInput, DepartmentsInput, StandingCcInput)
        calls = (
            (self.client.project_get(ProjectGetInput(project=self.project['id'])), 'project', 'Example Show'),
            (self.client.project_resolve(ProjectResolveInput(text='Example Show')), 'candidates', 'Example Show'),
            (self.client.jo_get(JoGetInput(ref=self.jo['id'])), 'job_order', 'Work'),
            (self.client.person_find(PersonFindInput(mention='Ana Cruz')), 'match', 'Ana Cruz'),
            (self.client.person_get(PersonGetInput(person_id=self.person['id'])), 'person', 'Ana Cruz'),
            (self.client.camera_get(CameraGetInput(page_id=self.camera['id'])), 'cameras', 'Camera One'),
        )
        for result, key, name in calls:
            self.assertIsInstance(result, Done, result)
            assert isinstance(result, Done)
            item = result.result[key]
            if isinstance(item, list):
                item = item[0]
            self.assertIsNotNone(item, (key, result))
            self.assertEqual(item['name'], name)
        for result in (self.client.departments(DepartmentsInput()), self.client.standing_cc(StandingCcInput()),
                       self.client.person_list(PersonListInput())):
            self.assertIsInstance(result, Done, result)

    def test_text_withhold_replaces_labelled_credentials_and_keeps_the_order(self) -> None:
        """The watchdog shows message text in its ops post; the rule for a credential lives in the API (#486)."""
        from t6.privacy.withhold import WITHHELD
        from t6.verbs import TextWithholdInput
        result = self.client.text_withhold(TextWithholdInput(texts=['JO 12 password: hunter2 for the drive', 'no secret here', '']))
        assert isinstance(result, Done), result
        self.assertEqual(result.result, {'texts': [f'JO 12 password: {WITHHELD}', 'no secret here', ''], 'withheld': 1})
        self.assertEqual(self.client.text_withhold(TextWithholdInput(texts=[])), Done(result={'texts': [], 'withheld': 0}))
        from t6.client import APIError
        with self.assertRaises(APIError) as refused:
            self.client.call('text', 'withhold', TextWithholdInput.model_construct(texts=['x'] * 201))
        self.assertEqual(refused.exception.status, 422)
        self.assertNotIn('hunter2', str(result.result))

    def test_client_names_masked_for_readers_and_visible_to_operators(self) -> None:
        from t6.verbs import ProjectListInput
        masked = self.client.project_list(ProjectListInput())
        operator = Client(f'http://127.0.0.1:{self.server.server_port}', 'operator')
        visible = operator.project_list(ProjectListInput())
        assert isinstance(masked, Done) and isinstance(visible, Done)
        self.assertNotIn('Client Corp', str(masked.result))
        self.assertIn('Client Corp campaign', str(visible.result))

    def test_a_caller_that_asks_for_real_names_is_refused_rather_than_masked(self) -> None:
        """The shift report goes to a group: a lost `unmasked` grant must stop it, not send masked names (#488)."""
        from t6.verbs import DepartmentsInput, ProjectListInput, Refused
        url = f'http://127.0.0.1:{self.server.server_port}'
        strict = Client(url, 'secret', require_unmasked=True)
        for refused in (strict.project_list(ProjectListInput()), strict.departments(DepartmentsInput())):
            assert isinstance(refused, Refused), refused
            self.assertEqual(refused.rule, 'unmasked')
        visible = Client(url, 'operator', require_unmasked=True).project_list(ProjectListInput())
        assert isinstance(visible, Done), visible
        self.assertIn('Client Corp campaign', str(visible.result))
        self.assertIsInstance(self.client.project_list(ProjectListInput()), Done)  # without the flag: masked, as before

    def test_read_rejects_source_ids_and_filters(self) -> None:
        from urllib.error import HTTPError
        from urllib.request import Request, urlopen
        import json
        for payload in ({'data_sources': {'projects': 'other'}}, {'filter': {'property': 'x'}}):
            request = Request(f'http://127.0.0.1:{self.server.server_port}/v1/project/list',
                              data=json.dumps(payload).encode(), headers={'Authorization': 'Bearer secret'})
            with self.assertRaises(HTTPError) as error:
                urlopen(request)
            self.assertEqual(error.exception.code, 422)
            error.exception.close()

    def test_t6_ids_and_rotation_filters(self) -> None:
        from t6.registry import Registered, Rotation
        from t6.verbs import ProjectGetInput, ProjectListInput, JoListInput
        registered = self.registry.register(actor='setup', idempotency_key='p',
            notion_page_id=self.project['id'], code='EX', rotation=Rotation.IN)
        assert isinstance(registered, Registered)
        project_id = registered.project.id
        result = self.client.project_get(ProjectGetInput(project=project_id))
        assert isinstance(result, Done)
        self.assertEqual(result.result['project']['t6_id'], project_id)
        result = self.client.project_list(ProjectListInput(rotation='in'))
        assert isinstance(result, Done)
        self.assertEqual(len(result.result['projects']), 1)
        result = self.client.jo_list(JoListInput(project=project_id))
        assert isinstance(result, Done)
        self.assertEqual(result.result['job_orders'][0]['project_id'], project_id)

    def test_project_resolve_in_text_scans_the_registry_codes(self) -> None:
        from t6.registry import Rotation
        from t6.verbs import ProjectResolveInput
        self.registry.register(actor='setup', idempotency_key='p', notion_page_id=self.project['id'], code='EXS', rotation=Rotation.IN)
        found = self.client.project_resolve(ProjectResolveInput(text='exs pilot cut', in_text=True))
        assert isinstance(found, Done)
        self.assertEqual((found.result['match']['page_id'], found.result['reason']), (self.project['id'], None))
        none = self.client.project_resolve(ProjectResolveInput(text='Example Show', in_text=True))
        assert isinstance(none, Done)
        self.assertEqual((none.result['match'], none.result['reason'], none.result['candidates']), (None, 'no_code_in_text', []))
        next_to_word = self.client.project_resolve(ProjectResolveInput(text='EXSX', in_text=True))
        assert isinstance(next_to_word, Done)
        self.assertEqual(next_to_word.result['reason'], 'no_code_in_text')

    def test_project_resolve_without_in_text_is_unchanged(self) -> None:
        from t6.registry import Rotation
        from t6.verbs import ProjectResolveInput
        self.registry.register(actor='setup', idempotency_key='p', notion_page_id=self.project['id'], code='EXS', rotation=Rotation.IN)
        plain = self.client.project_resolve(ProjectResolveInput(text='EXS pilot cut'))
        assert isinstance(plain, Done)
        self.assertIsNone(plain.result['match'])
        self.assertEqual(plain.result['candidates'], [])
        named = self.client.project_resolve(ProjectResolveInput(text='Example Show'))
        assert isinstance(named, Done)
        self.assertEqual(named.result['candidates'][0]['name'], 'Example Show')

    def test_registered_output_schemas_describe_read_results(self) -> None:
        from t6.verbs import (VERBS, ProjectListInput, ProjectGetInput, ProjectResolveInput,
                              JoListInput, JoGetInput, JoFindInput, PersonListInput, PersonGetInput,
                              PersonFindInput, DeliverableListInput, MeetingNotesListInput,
                              CameraGetInput, DepartmentsInput, StandingCcInput, StudioNoticesInput, ExternalLinkFindInput)
        calls = {
            ('project', 'list'): ProjectListInput(),
            ('project', 'get'): ProjectGetInput(project=self.project['id']),
            ('project', 'resolve'): ProjectResolveInput(text='Example Show'),
            ('jo', 'list'): JoListInput(), ('jo', 'get'): JoGetInput(ref=self.jo['id']),
            ('jo', 'find'): JoFindInput(telegram_key='-100:3'),
            ('person', 'list'): PersonListInput(), ('person', 'get'): PersonGetInput(person_id=self.person['id']),
            ('person', 'find'): PersonFindInput(mention='Ana Cruz'),
            ('deliverable', 'list'): DeliverableListInput(project=self.project['id']),
            ('meeting-notes', 'list'): MeetingNotesListInput(project=self.project['id']),
            ('camera', 'get'): CameraGetInput(), ('departments', ''): DepartmentsInput(),
            ('standing-cc', ''): StandingCcInput(), ('studio', 'notices'): StudioNoticesInput(),
            ('external-link', 'find'): ExternalLinkFindInput(system='Instagantt', kind='timeline', external_id='missing'),
        }
        for entry in VERBS:
            if (entry.noun, entry.verb) not in calls:
                continue
            with self.subTest(verb=(entry.noun, entry.verb)):
                outcome = self.client.call(entry.noun, entry.verb, calls[(entry.noun, entry.verb)])
                assert isinstance(outcome, Done), outcome
                entry.output_model.model_validate(outcome.result)

    def test_get_refuses_pages_from_the_wrong_source(self) -> None:
        from t6.verbs import JoGetInput, PersonGetInput, Refused
        jo = self.client.jo_get(JoGetInput(ref=self.person['id']))
        person = self.client.person_get(PersonGetInput(person_id=self.project['id']))
        self.assertIsInstance(jo, Refused)
        self.assertIsInstance(person, Refused)

    def test_unmasked_grant_reads_real_names_and_nothing_more(self) -> None:
        from t6.verbs import DeliverableListInput, JoGetInput, PersonListInput, ProjectListInput, DeliverableEditInput, DeliverableMissInput
        granted = Client(self.client.base_url, 'unmasked-secret')
        for result in (granted.project_list(ProjectListInput()), granted.jo_get(JoGetInput(ref=self.jo['id'])),
                       granted.deliverable_list(DeliverableListInput(project=self.project['id'])),
                       granted.person_list(PersonListInput())):
            self.assertIsInstance(result, Done)
        self.assertIn('Client Corp', str(granted.project_list(ProjectListInput())))
        self.assertNotIn('Client Corp', str(self.client.project_list(ProjectListInput())))
        for refused in (granted.call('deliverable', 'edit', DeliverableEditInput(deliverable='x', code='A')),
                        granted.call('deliverable', 'miss', DeliverableMissInput(deliverable='x', reason='late'))):
            self.assertIsInstance(refused, Refused)
            assert isinstance(refused, Refused)
            self.assertEqual(refused.rule, 'permission')
        self.api.privacy_registry = None
        self.assertIsInstance(granted.project_list(ProjectListInput()), Done)
        self.assertIsInstance(self.client.project_list(ProjectListInput()), Refused)

    def test_missing_privacy_registry_fails_closed(self) -> None:
        from t6.verbs import ProjectListInput, Refused
        self.api.privacy_registry = None
        self.assertIsInstance(self.client.project_list(ProjectListInput()), Refused)

    def test_job_order_and_person_ids_are_present_in_reads(self) -> None:
        from t6.job_order import JobOrderStore
        from t6.notion.job_orders import JobOrders
        from t6.notion.reads import ContactReader
        from t6.verbs import JoListInput, PersonGetInput
        people = self.api.people
        with self.notion.client() as client:
            contacts = ContactReader(client, 'contacts').list()
            preview = people.migrate(contacts, actor='setup')
            migrated = people.migrate(contacts, actor='setup', apply=True, digest=preview['digest'])
            person_id = migrated['entries'][0]['t6_id']
            orders = JobOrders(client, 'job_orders').list()
            store = JobOrderStore(self.registry, PreviewTokens('key'))
            preview_jo = store.migrate(orders, actor='setup')
            migrated_jo = store.migrate(orders, actor='setup', apply=True, digest=preview_jo['digest'])
        jo_id = migrated_jo['entries'][0]['t6_id']
        result = self.client.jo_list(JoListInput(assignee=person_id))
        assert isinstance(result, Done)
        self.assertEqual(result.result['job_orders'][0]['t6_id'], jo_id)
        self.assertEqual(result.result['job_orders'][0]['assigned_ids'], [person_id])
        result = self.client.person_get(PersonGetInput(person_id=person_id))
        assert isinstance(result, Done)
        self.assertEqual(result.result['person']['t6_id'], person_id)

    def test_jo_list_no_project_returns_only_job_orders_with_no_project_oldest_first(self) -> None:
        import json
        from t6.verbs import JoListInput
        names = ('Orphan A', 'Orphan B')
        for name in names:
            self.notion.add_page('job_orders', {JobOrdersRow.notion_name('name'): title(name)})
        result = self.client.jo_list(JoListInput(no_project=True))
        assert isinstance(result, Done)
        self.assertEqual([o['name'] for o in result.result['job_orders']], list(names))
        self.assertEqual([o['project_id'] for o in result.result['job_orders']], [None, None])
        queries = [json.loads(r.content) for r in self.notion.requests if r.url.path.endswith('/query')]
        self.assertEqual(queries[-1]['sorts'], [{'timestamp': 'created_time', 'direction': 'ascending'}])
        everything = self.client.jo_list(JoListInput())
        assert isinstance(everything, Done)
        self.assertEqual(len(everything.result['job_orders']), 3)
        both = self.client.jo_list(JoListInput(project=self.project['id'], no_project=True))
        assert isinstance(both, Refused)
        self.assertEqual(both.rule, 'input')

    def test_external_link_find_for_reports_instagantt_reads_media_locations(self) -> None:
        from t6.ids import EntityKind, T6Id
        from t6.media import MediaStore
        from t6.registry import Registered
        from t6.verbs import ExternalLinkFindInput, Preview
        registered = self.registry.register(actor='setup', idempotency_key='timeline-project', notion_page_id=self.project['id'])
        assert isinstance(registered, Registered)
        project = registered.project.id
        timeline = 'pr_8Kd2mXq41ZtYw'
        store = MediaStore(self.registry, PreviewTokens('key'))
        values = {'owner': project, 'kind': 'instagantt-project', 'ref': timeline, 'role': 'working'}
        preview = store.write('link', values, actor='person', idempotency_key='link-timeline')
        assert isinstance(preview, Preview)
        store.write('link', values, actor='person', idempotency_key='link-timeline', confirm=preview.token)
        result = self.client.external_link_find(ExternalLinkFindInput(system='Instagantt', kind='timeline', external_id=timeline))
        assert isinstance(result, Done)
        self.assertEqual(result.result['link']['project_ids'], [project])
        self.assertEqual(result.result['link']['ref']['external_id'], timeline)
        missing = self.client.external_link_find(ExternalLinkFindInput(system='Instagantt', kind='timeline', external_id='missing'))
        assert isinstance(missing, Done)
        self.assertIsNone(missing.result['link'])

    def test_studio_notices_and_global_meeting_notes(self) -> None:
        from t6.verbs import StudioNoticesInput, MeetingNotesListInput
        notices = self.client.studio_notices(StudioNoticesInput())
        assert isinstance(notices, Done)
        self.assertEqual(notices.result['description'], '')
        notes = self.client.meeting_notes_list(MeetingNotesListInput())
        assert isinstance(notes, Done)
        self.assertEqual(notes.result['meeting_notes'][0]['name'], 'Notes')

    def test_server_configuration_loads_owned_files(self) -> None:
        from pathlib import Path
        from t6.server.verbs import read_config_from_env
        sources = Path(self.tmp.name) / 'sources.json'
        names = Path(self.tmp.name) / 'names.json'
        sources.write_text('{"projects":"owned"}', encoding='utf-8')
        names.write_text('["Client Corp"]', encoding='utf-8')
        loaded, privacy = read_config_from_env({'T6_READ_DATA_SOURCES': str(sources),
                                                'T6_READ_PRIVACY_REGISTRY': str(names)})
        self.assertEqual(loaded, {'projects': 'owned'})
        self.assertIsNotNone(privacy)
        names.write_text('{"bad":"shape"}', encoding='utf-8')
        with self.assertRaises(ValueError):
            read_config_from_env({'T6_READ_PRIVACY_REGISTRY': str(names)})

    def test_trashed_records_are_not_listed(self) -> None:
        from t6.verbs import JoFindInput, DeliverableListInput, MeetingNotesListInput
        with self.notion.client() as notion:
            notion.update_page(self.jo['id'], {'in_trash': True})
            for page in notion.query_data_source('deliverables'):
                notion.update_page(page['id'], {'in_trash': True})
            notion.update_page(self.note['id'], {'in_trash': True})
        for outcome, key in ((self.client.jo_find(JoFindInput(telegram_key='-100:3')), 'job_orders'),
                             (self.client.deliverable_list(DeliverableListInput(project=self.project['id'])), 'deliverables'),
                             (self.client.meeting_notes_list(MeetingNotesListInput(project=self.project['id'])), 'meeting_notes')):
            assert isinstance(outcome, Done)
            self.assertEqual(outcome.result[key], [])

    def test_status_and_readiness_filters(self) -> None:
        from t6.verbs import JoListInput, ProjectListInput, MeetingNotesListInput
        self.notion.add_page('projects', {'Name': title('Active Show'), 'Status': {'type': 'select', 'select': {'name': 'In Progress'}}})
        self.notion.add_page('job_orders', {JobOrdersRow.notion_name('name'): title('Pending work'),
                                           JobOrdersRow.notion_name('status'): {'type': 'status', 'status': {'name': 'Pending'}}})
        result = self.client.project_list(ProjectListInput(status='In Progress'))
        assert isinstance(result, Done)
        self.assertEqual([p['name'] for p in result.result['projects']], ['Active Show'])
        result = self.client.jo_list(JoListInput(status_group='not_started'))
        assert isinstance(result, Done)
        self.assertEqual([o['name'] for o in result.result['job_orders']], ['Pending work'])
        result = self.client.jo_list(JoListInput(status_group='active'))
        assert isinstance(result, Done)
        self.assertEqual({o['name'] for o in result.result['job_orders']}, {'Work', 'Pending work'})
        result = self.client.meeting_notes_list(MeetingNotesListInput(title_prefix='Notes', before='2026-11-13'))
        assert isinstance(result, Done)
        self.assertEqual(len(result.result['meeting_notes']), 1)
        result = self.client.meeting_notes_list(MeetingNotesListInput(ready_to_send=True))
        assert isinstance(result, Done)
        self.assertEqual(result.result['meeting_notes'], [])

    def test_masking_does_not_rewrite_identifiers(self) -> None:
        from t6.verbs import ProjectListInput, Refused
        self.api.privacy_registry = Registry(people=[Person(name=self.project['id'])])
        self.assertIsInstance(self.client.project_list(ProjectListInput()), Refused)

    def test_cli_reads_need_no_client_side_source_file(self) -> None:
        import contextlib
        import io
        from t6.cli import main
        env = {'T6_API_URL': f'http://127.0.0.1:{self.server.server_port}', 'T6_API_SECRET': 'secret'}
        for args in (['project', 'list'], ['person', 'get', '--person-id', self.person['id']],
                     ['jo', 'get', self.jo['id']], ['departments'], ['project', 'list', '--status-group', 'on_hold'],
                     ['project', 'flags', '--project', self.project['id'], '--as-of', '2026-11-16', '--stated', '{"playdate": null}']):
            with self.subTest(args=args), patch.dict(os.environ, env), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(args), 0)

    def test_jo_get_accepts_telegram_key_before_migration(self) -> None:
        from t6.verbs import JoGetInput
        result = self.client.jo_get(JoGetInput(ref='-100:3'))
        assert isinstance(result, Done)
        self.assertEqual(result.result['job_order']['page_id'], self.jo['id'])

    def test_watchdog_verbs_read_only_through_notion_source_and_jo_get_passes_live(self) -> None:
        from t6.notion import Freshness, NotionSource
        from t6.verbs import (
            DeliverableCandidatesInput,
            JoFindInput,
            JoGetInput,
            PersonFindInput,
            ProjectResolveInput,
        )

        calls: list[tuple[str, str, Freshness]] = []

        class SpySource(NotionSource):
            def query_data_source(
                self,
                data_source_id: str,
                *,
                filter: dict[str, Any] | None = None,
                sorts: list[dict[str, Any]] | None = None,
                page_size: int = 100,
                freshness: Freshness = 'mirror',
            ) -> list[dict[str, Any]]:
                calls.append(('query_data_source', data_source_id, freshness))
                return super().query_data_source(
                    data_source_id, filter=filter, sorts=sorts, page_size=page_size, freshness=freshness
                )

            def retrieve_page(self, page_id: str, *, freshness: Freshness = 'mirror') -> dict[str, Any]:
                calls.append(('retrieve_page', page_id, freshness))
                return super().retrieve_page(page_id, freshness=freshness)

            def list_block_children(
                self, block_id: str, *, page_size: int = 100, freshness: Freshness = 'live'
            ) -> list[dict[str, Any]]:
                calls.append(('list_block_children', block_id, freshness))
                return super().list_block_children(block_id, page_size=page_size, freshness=freshness)

        spy = SpySource(self.notion.client)
        self.api.notion_source = spy
        # Prove none of the 5 watchdog verbs open their own Notion client directly.
        self.api.open_notion = lambda _token: (_ for _ in ()).throw(AssertionError('direct open_notion call'))

        unmasked = Client(f'http://127.0.0.1:{self.server.server_port}', 'unmasked-secret')

        resolved = unmasked.project_resolve(ProjectResolveInput(text='Example Show'))
        self.assertIsInstance(resolved, Done)
        self.assertEqual(calls, [('query_data_source', 'projects', 'mirror')])

        calls.clear()
        person = unmasked.person_find(PersonFindInput(mention='Ana Cruz'))
        self.assertIsInstance(person, Done)
        self.assertEqual(calls, [('query_data_source', 'contacts', 'mirror')])

        calls.clear()
        candidates = unmasked.deliverable_candidates(DeliverableCandidatesInput(project=self.project['id']))
        self.assertIsInstance(candidates, Done)
        self.assertEqual(calls, [('query_data_source', 'deliverables', 'mirror')])

        calls.clear()
        found_jo = unmasked.jo_find(JoFindInput(telegram_key='-100:3'))
        self.assertIsInstance(found_jo, Done)
        self.assertEqual(calls, [('query_data_source', 'job_orders', 'mirror')])

        calls.clear()
        got_by_id = unmasked.jo_get(JoGetInput(ref=self.jo['id']))
        self.assertIsInstance(got_by_id, Done)
        self.assertEqual(
            calls,
            [
                ('retrieve_page', self.jo['id'], 'live'),
                ('retrieve_page', self.jo['id'], 'live'),
                ('list_block_children', self.jo['id'], 'live'),
            ],
        )

        calls.clear()
        got_by_key = unmasked.jo_get(JoGetInput(ref='-100:3'))
        self.assertIsInstance(got_by_key, Done)
        self.assertEqual(
            calls,
            [
                ('query_data_source', 'job_orders', 'live'),
                ('retrieve_page', self.jo['id'], 'live'),
                ('retrieve_page', self.jo['id'], 'live'),
                ('list_block_children', self.jo['id'], 'live'),
            ],
        )

    def test_verb_api_uses_single_lazy_notion_source_and_supports_injection(self) -> None:
        from t6.notion import NotionSource
        from t6.verbs import DeliverableCandidatesInput, JoFindInput, JoGetInput, PersonFindInput, ProjectResolveInput

        # Default server-owned NotionSource reads T6_NOTION_TOKEN lazily and is reused across calls.
        source = self.api.notion_source
        self.assertIsInstance(source, NotionSource)
        tokens_seen: list[str] = []
        original_open = self.api.open_notion

        def recording_open(token: str) -> Any:
            tokens_seen.append(token)
            return original_open(token)

        self.api.open_notion = recording_open
        with patch.dict(os.environ, {'T6_NOTION_TOKEN': 'rotated-token'}):
            self.assertIsInstance(self.client.project_resolve(ProjectResolveInput(text='Example Show')), Done)
            self.assertIsInstance(self.client.person_find(PersonFindInput(mention='Ana Cruz')), Done)
            self.assertIsInstance(self.client.jo_find(JoFindInput(telegram_key='-100:3')), Done)
            self.assertIsInstance(self.client.jo_get(JoGetInput(ref=self.jo['id'])), Done)
            self.assertIsInstance(self.client.deliverable_candidates(DeliverableCandidatesInput(project=self.project['id'])), Done)
        self.assertIs(self.api.notion_source, source)
        self.assertTrue(tokens_seen)
        self.assertEqual(set(tokens_seen), {'rotated-token'})

        # Injected NotionSource works even without T6_NOTION_TOKEN in the environment.
        injected = NotionSource(self.notion.client)
        injected_api = VerbAPI(
            self.api.people,
            self.api.queue,
            {'secret': 'reports'},
            lambda _: (_ for _ in ()).throw(AssertionError('must use injected NotionSource')),
            privacy_registry=self.api.privacy_registry,
            data_sources=self.api.data_sources,
            notion_source=injected,
        )
        server = make_server(('127.0.0.1', 0), injected_api)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.dict(os.environ, {}, clear=True):
                res = Client(f'http://127.0.0.1:{server.server_port}', 'secret').project_resolve(
                    ProjectResolveInput(text='Example Show')
                )
            self.assertIsInstance(res, Done)
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

    def test_remaining_verb_api_routes_use_notion_source_with_expected_freshness(self) -> None:
        from t6.notion import Freshness, NotionSource
        from t6.notion.testing import external_link_properties, package_spec_properties, project_properties
        from t6.registry import Registered
        from t6.verbs import (
            CameraGetInput,
            DeliverableDefineInput,
            DeliverableDeliverInput,
            DepartmentsInput,
            MachineListInput,
            MediaMigrateInput,
            MeetingNotesCreateInput,
            MeetingNotesListInput,
            PersonGetInput,
            PersonListInput,
            StudioNoticesInput,
        )

        self.notion.add_snapshot_data_source('bays', 'bays')
        self.notion.add_snapshot_data_source('package_specs', 'package_specs')
        self.notion.add_data_source('external_links')
        self.notion.add_option('job_orders', 'Department', 'AE')
        self.notion.add_page('bays', {'Name': title('Bay 1')})
        self.notion.add_page('package_specs', package_spec_properties(package_kind='delivery', version='1'))
        self.notion.add_page('external_links', external_link_properties(system='resolve', kind='project', external_id='db/show', projects=[self.project['id']], active=True))
        typed_proj = self.notion.add_page('projects', project_properties(name='Typed Show', project_type='Feature Film'))
        reg_proj = self.registry.register(actor='setup', idempotency_key='typed-proj', notion_page_id=typed_proj['id'])
        assert isinstance(reg_proj, Registered)
        with self.registry._transaction() as db:
            db.execute("INSERT INTO deliverables VALUES ('t6v_del', ?, 'FTR', NULL, NULL, NULL)", (reg_proj.project.id,))
            db.execute("INSERT INTO persons VALUES ('t6n_1', 'staff', '[]', '[]', NULL, 0, 1)")
            db.execute("INSERT INTO bindings (system, page_id, project_id, bound_at, active) VALUES ('notion', ?, 't6n_1', 'now', 1)", (self.person['id'].replace('-', ''),))
            db.execute("INSERT INTO person_permissions VALUES (?, 'minutes')", ('t6n_' + '1' * 32,))
            db.execute("INSERT INTO person_permissions VALUES (?, 'admin')", ('t6n_' + '1' * 32,))
        self.api.data_sources.update({'bays': 'bays', 'package_specs': 'package_specs', 'external_links': 'external_links'})

        calls: list[tuple[str, str, Freshness]] = []

        class SpySource(NotionSource):
            def query_data_source(self, data_source_id: str, *, filter: dict[str, Any] | None = None, sorts: list[dict[str, Any]] | None = None, page_size: int = 100, freshness: Freshness = 'mirror') -> list[dict[str, Any]]:
                calls.append(('query_data_source', data_source_id, freshness))
                return super().query_data_source(data_source_id, filter=filter, sorts=sorts, page_size=page_size, freshness=freshness)

            def retrieve_page(self, page_id: str, *, freshness: Freshness = 'mirror') -> dict[str, Any]:
                calls.append(('retrieve_page', page_id, freshness))
                return super().retrieve_page(page_id, freshness=freshness)

            def retrieve_data_source(self, data_source_id: str, *, freshness: Freshness = 'mirror') -> dict[str, Any]:
                calls.append(('retrieve_data_source', data_source_id, freshness))
                return super().retrieve_data_source(data_source_id, freshness=freshness)

            def list_block_children(self, block_id: str, *, page_size: int = 100, freshness: Freshness = 'live') -> list[dict[str, Any]]:
                calls.append(('list_block_children', block_id, freshness))
                return super().list_block_children(block_id, page_size=page_size, freshness=freshness)

        self.api.notion_source = SpySource(self.notion.client)
        self.api.open_notion = lambda _token: (_ for _ in ()).throw(AssertionError('direct open_notion call'))

        operator = Client(f'http://127.0.0.1:{self.server.server_port}', 'operator')

        self.assertIsInstance(operator.studio_notices(StudioNoticesInput()), Done)
        self.assertEqual(calls, [('retrieve_data_source', 'projects', 'mirror')])

        calls.clear()
        self.assertIsInstance(operator.departments(DepartmentsInput()), Done)
        self.assertEqual(calls, [('retrieve_data_source', 'job_orders', 'mirror')])

        calls.clear()
        self.assertIsInstance(operator.camera_get(CameraGetInput()), Done)
        self.assertEqual(calls, [('query_data_source', 'cameras', 'mirror')])

        calls.clear()
        self.assertIsInstance(operator.person_list(PersonListInput()), Done)
        self.assertEqual(calls, [('query_data_source', 'contacts', 'mirror')])

        calls.clear()
        self.assertIsInstance(operator.person_get(PersonGetInput(person_id='t6n_1')), Done)
        self.assertEqual(calls, [('retrieve_page', self.person['id'], 'mirror'), ('retrieve_page', self.person['id'], 'mirror')])

        calls.clear()
        self.assertIsInstance(operator.meeting_notes_list(MeetingNotesListInput(include_body=False)), Done)
        self.assertEqual(calls, [('query_data_source', 'meeting_notes', 'mirror')])

        calls.clear()
        self.assertIsInstance(operator.meeting_notes_list(MeetingNotesListInput(include_body=True)), Done)
        self.assertEqual(calls, [('query_data_source', 'meeting_notes', 'mirror'), ('list_block_children', self.note['id'], 'live')])

        calls.clear()
        self.assertIsInstance(operator.meeting_notes_create(MeetingNotesCreateInput(title='New note', meeting_date='2026-11-18', project_ids=[], attendee_ids=[], blocks=[])), Done)
        self.assertTrue(calls)
        self.assertTrue(all(f == 'live' for _, _, f in calls))

        calls.clear()
        self.assertIsInstance(operator.machine_list(MachineListInput()), Done)
        self.assertEqual(calls, [('query_data_source', 'bays', 'mirror')])

        calls.clear()
        with patch.dict(os.environ, {'T6_PREVIEW_SIGNING_KEY': 'key'}):
            self.assertIsInstance(operator.deliverable_deliver(DeliverableDeliverInput(deliverable='t6v_del', carrier='drive', idempotency_key='del-1')), Preview)
        self.assertEqual(calls, [('query_data_source', 'package_specs', 'live')])

        calls.clear()
        with patch.dict(os.environ, {'T6_PREVIEW_SIGNING_KEY': 'key'}):
            self.assertIsInstance(operator.deliverable_define(DeliverableDefineInput(project=reg_proj.project.id, code='Ep01', idempotency_key='def-1')), Preview)
        self.assertEqual(calls, [('retrieve_page', typed_proj['id'], 'live')])

        calls.clear()
        with patch.dict(os.environ, {'T6_PREVIEW_SIGNING_KEY': 'key'}):
            preview = operator.admin_media_migrate(MediaMigrateInput(idempotency_key='mig-1'))
        self.assertIsInstance(preview, Preview)
        self.assertEqual(calls, [('query_data_source', 'external_links', 'live')])

        calls.clear()
        health = operator.health()
        assert isinstance(health, dict)
        self.assertIn('data_gaps', health)
        self.assertEqual(calls, [('query_data_source', 'projects', 'mirror')])
