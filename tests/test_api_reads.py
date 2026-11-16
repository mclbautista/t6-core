"""Read verbs through the HTTP client and fake Notion."""
import os
import tempfile
import threading
import unittest
from typing import Any
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
        self.api = VerbAPI(People(registry, PreviewTokens('key')), JobQueue(self.tmp.name + '/queue'),
                           {'secret': 'reports', 'operator': operator, 'mcp-secret': 'mcp'}, lambda _: self.notion.client(),
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
                     ['jo', 'get', self.jo['id']], ['departments']):
            with self.subTest(args=args), patch.dict(os.environ, env), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(args), 0)

    def test_jo_get_accepts_telegram_key_before_migration(self) -> None:
        from t6.verbs import JoGetInput
        result = self.client.jo_get(JoGetInput(ref='-100:3'))
        assert isinstance(result, Done)
        self.assertEqual(result.result['job_order']['page_id'], self.jo['id'])
