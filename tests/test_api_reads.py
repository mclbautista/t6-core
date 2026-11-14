"""Read verbs through the HTTP client and fake Notion."""
import os
import tempfile
import threading
import unittest
from unittest.mock import patch

from t6.client import Client
from t6.notion.testing import FakeNotion, title, relation, rich_text, date
from t6.notion.generated import JobOrdersRow, DeliverablesRow, MeetingNotesRow
from t6.person import People
from t6.preview import PreviewTokens
from t6.registry.sqlite import SqliteProjectRegistry
from t6.server.http import make_server
from t6.server.queue import JobQueue
from t6.server.verbs import VerbAPI
from t6.verbs import Done
from t6.privacy import Person, Registry


class ReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.notion = FakeNotion()
        for source in ('projects', 'job_orders', 'deliverables', 'meeting_notes', 'contacts', 'cameras'):
            self.notion.add_snapshot_data_source(source, source)
        self.notion.add_data_source('external_links')
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
        self.api = VerbAPI(People(registry, PreviewTokens('key')), JobQueue(self.tmp.name + '/queue'),
                           {'secret': 'reports', 'operator': operator}, lambda _: self.notion.client(),
                           privacy_registry=Registry(people=[Person(name='Client Corp')]),
                           data_sources={name: name for name in ('projects', 'job_orders', 'deliverables', 'meeting_notes', 'contacts', 'cameras', 'external_links')})
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

    def test_project_related_reads_and_telegram_key(self) -> None:
        from t6.verbs import DeliverableListInput, JoFindInput, JoListInput, MeetingNotesListInput
        project = self.project['id']
        for result, key, name in (
            (self.client.jo_list(JoListInput(project=project)), 'job_orders', 'Work'),
            (self.client.jo_find(JoFindInput(telegram_key='-100:3')), 'job_orders', 'Work'),
            (self.client.deliverable_list(DeliverableListInput(project=project)), 'deliverables', 'Trailer'),
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

    def test_client_names_masked_for_readers_and_visible_to_operators(self) -> None:
        from t6.verbs import ProjectListInput
        masked = self.client.project_list(ProjectListInput())
        operator = Client(f'http://127.0.0.1:{self.server.server_port}', 'operator')
        visible = operator.project_list(ProjectListInput())
        assert isinstance(masked, Done) and isinstance(visible, Done)
        self.assertNotIn('Client Corp', str(masked.result))
        self.assertIn('Client Corp campaign', str(visible.result))

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

    def test_external_link_find_for_reports_instagantt(self) -> None:
        from t6.notion.testing import external_link_properties
        from t6.verbs import ExternalLinkFindInput
        self.notion.add_page('external_links', external_link_properties(
            system='Instagantt', kind='timeline', external_id='timeline-1', projects=[self.project['id']]))
        result = self.client.external_link_find(ExternalLinkFindInput(system='Instagantt', kind='timeline', external_id='timeline-1'))
        assert isinstance(result, Done)
        self.assertEqual(result.result['link']['project_ids'], [self.project['id']])

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
                     ['jo', 'get', self.jo['id']], ['departments']):
            with self.subTest(args=args), patch.dict(os.environ, env), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(args), 0)

    def test_jo_get_accepts_telegram_key_before_migration(self) -> None:
        from t6.verbs import JoGetInput
        result = self.client.jo_get(JoGetInput(ref='-100:3'))
        assert isinstance(result, Done)
        self.assertEqual(result.result['job_order']['page_id'], self.jo['id'])
