"""Template selection and Notion projection against the in-memory stores."""
import json
import os
import unittest
from typing import Any
from unittest.mock import patch

from test_person import MemoryRegistry
from t6.deliverable import DeliverableStore
from t6.deliverable_templates import PROJECT_TYPES, TEMPLATES
from t6.ids import EntityKind, T6Id
from t6.notion import codec
from t6.notion.deliverable_projection import DeliverableProjection
from t6.notion.generated import DeliverablesRow, ProjectsRow
from t6.notion.testing import FakeNotion, project_properties
from t6.notion_edits import NotionEdits
from t6.preview import PreviewTokens
from t6.registry import Registered
from t6.verbs import Done, Preview, Refused
from t6.worker.client import WorkerJob


class Bridge:
    def __init__(self, registry: MemoryRegistry) -> None:
        self.registry = registry

    def __getattr__(self, name: str) -> Any:
        return getattr(self.registry, name)

    def projection(self, page: str, values: dict[str, Any], *, record: bool = False) -> dict[str, Any]:
        return NotionEdits(self.registry).projection(page, values, record=record)

    def observe_deliverable(self, page: str, shown: dict[str, Any], changed: list[str], editor: str, event: str, *, editor_id: str | None = None) -> dict[str, Any]:
        return NotionEdits(self.registry).deliverable(page, shown, changed, editor, event, editor_id=editor_id)


class TemplateProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = MemoryRegistry()
        self.addCleanup(self.db.db.close)
        self.db._new_id = lambda: str(T6Id.mint(EntityKind.PROJECT))
        with self.db._transaction() as db:
            db.execute("INSERT INTO persons VALUES ('operator', 'staff', '[]', '[]', NULL, 0, 1)")
        self.fake = FakeNotion()
        self.fake.add_snapshot_data_source('projects', 'projects')
        self.fake.add_snapshot_data_source('deliverables', 'deliverables')
        self.fake.add_property('deliverables', 'T6 ID', 'rich_text')
        self.client = self.fake.client()
        self.addCleanup(self.client.close)
        self.project_page = str(self.fake.add_page('projects', project_properties(name='Show', project_type='Feature Film'))['id'])
        project = self.db.register(actor='service', idempotency_key='project', notion_page_id=self.project_page)
        assert isinstance(project, Registered)
        self.project = project.project.id
        self.store = DeliverableStore(self.db, PreviewTokens('secret'), lambda page: ProjectsRow.from_page(self.client.retrieve_page(page)).project_type)
        self.projection = DeliverableProjection(self.client, Bridge(self.db), 'deliverables')
        prop = self.fake._data_sources['deliverables']['properties']['T6 ID']['id']
        patcher = patch('t6.notion.deliverable_projection.projection_id', return_value=prop)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.seq = 0

    def define(self, key: str, template: str | None = None) -> str:
        values = {'project': self.project, 'code': key, 'template': template}
        preview = self.store.define(values, actor='operator', idempotency_key=key)
        assert isinstance(preview, Preview), preview
        done = self.store.define(values, actor='operator', idempotency_key=key, confirm=preview.token)
        assert isinstance(done, Done)
        return str(done.result['deliverable_id'])

    def drain(self) -> None:
        for intent in self.db.undispatched_deliverable_sync():
            job = WorkerJob(job_id=intent.id, queue='main', token='x', kind='deliverable.sync', dedupe_key=str(intent.id), payload={'intent_id': intent.id}, attempt=1)
            self.projection.sync(job)
            self.db.mark_dispatched([intent.id])

    def record(self, identifier: str, milestone: str) -> None:
        self.seq += 1
        values = {'deliverable': identifier, 'milestone': milestone}
        preview = self.store.record_milestone(values, actor='operator', idempotency_key=str(self.seq))
        assert isinstance(preview, Preview), preview
        self.assertIsInstance(self.store.record_milestone(values, actor='operator', idempotency_key=str(self.seq), confirm=preview.token), Done)

    def page(self, identifier: str) -> str:
        record = self.store.get(identifier)
        assert record is not None
        return str(record['bindings'][0]['page_id'])

    def status(self, identifier: str) -> str | None:
        return DeliverablesRow.from_page(self.client.retrieve_page(self.page(identifier))).status

    def test_registered_define_uses_notion_and_accepts_override(self) -> None:
        from t6.person import People
        from t6.verbs import DeliverableDefineInput, VERBS, invoke

        with self.db._transaction() as db:
            db.execute("INSERT INTO person_permissions VALUES ('operator', 'operator')")
        people = People(self.db, PreviewTokens('secret'))
        entry = next(v for v in VERBS if (v.noun, v.verb) == ('deliverable', 'define'))
        with patch.dict(os.environ, {'T6_NOTION_TOKEN': 'fake', 'T6_PREVIEW_SIGNING_KEY': 'secret'}):
            inputs = DeliverableDefineInput(project=self.project, code='feature', idempotency_key='registered')
            preview = invoke(entry, inputs, actor='operator', people=people, open_notion=lambda token: self.fake.client())
            assert isinstance(preview, Preview), preview
            self.assertEqual(preview.plan['template'], 'film')
            done = invoke(entry, inputs.model_copy(update={'confirm': preview.token}), actor='operator', people=people, open_notion=lambda token: self.fake.client())
            assert isinstance(done, Done), done
            record = self.store.get(str(done.result['deliverable_id']))
            assert record is not None
            self.assertEqual(record['stage']['template'], 'film')
            override = DeliverableDefineInput(project=self.project, code='sound', template='sound_only')
            result = invoke(entry, override, actor='operator', people=people, open_notion=lambda token: self.fake.client())
            assert isinstance(result, Preview), result
            self.assertEqual(result.plan['template'], 'sound_only')

    def test_project_type_selection_and_override(self) -> None:
        mappings = {
            'film': ('Feature Film', 'Theatrical Feature', 'Documentary Feature', 'Festival Feature', 'MMFF Feature', "Int'l Co-Prod Feature", 'Streamer Originals Film', 'Short Film', 'Limited Series', 'Limited Documentary Series', 'Streamer Originals Series'),
            'daily': ('Serial TV', 'Serial Reality'), 'color_only': ('Serial TV - Color',), 'tvc': ('TVC', 'Docu Ad'),
        }
        pairs = [(project_type, template) for template, types in mappings.items() for project_type in types]
        self.assertEqual(PROJECT_TYPES, dict(pairs))
        for index, (project_type, expected) in enumerate(pairs):
            self.client.update_page(self.project_page, {'properties': {ProjectsRow.notion_id('project_type'): {'select': {'name': project_type}}}})
            identifier = self.define('mapped-' + str(index))
            record = self.store.get(identifier)
            assert record is not None
            self.assertEqual(record['stage']['template'], expected)
        for unmapped_type in ('Music Video', 'Operations', 'Vertical Shorts', 'Special Format', 'Film Festival', None):
            self.client.update_page(self.project_page, {'properties': {ProjectsRow.notion_id('project_type'): {'select': {'name': unmapped_type} if unmapped_type else None}}})
            self.assertIsInstance(self.store.define({'project': self.project, 'code': 'unknown'}, actor='operator'), Refused)
        identifier = self.define('override', 'sound_only')
        record = self.store.get(identifier)
        assert record is not None
        self.assertEqual(record['stage']['template'], 'sound_only')

    def test_version_lock_and_delivery_project_once_per_status_change(self) -> None:
        from t6.frameio.model import Version
        from t6.notion.package_specs import PackageSpecReader
        from t6.notion.testing import package_spec_properties
        from t6.package import PackageStore
        from t6.version import VersionStore

        class Reader:
            def versions(self, file_id: str) -> tuple[Version, ...]:
                return (Version(file_id=file_id, name='cut', stack_id='stack'),)

        identifier = self.define('other-verbs', 'film')
        self.drain()
        page = self.page(identifier)
        status_id = DeliverablesRow.notion_id('status')
        def status_writes() -> int:
            return sum(1 for request in self.fake.requests if request.method == 'PATCH' and page in str(request.url)
                       and status_id in json.loads(request.content).get('properties', {}))

        self.record(identifier, 'ingested')
        self.drain()
        before = status_writes()
        self.record(identifier, 'synced')
        self.record(identifier, 'color delivered')
        self.drain()
        self.assertEqual(status_writes(), before)

        versions = VersionStore(self.db, PreviewTokens('secret'), Reader())
        add = {'deliverable': identifier, 'frameio_file': 'file'}
        preview = versions.write('add', add, actor='operator', idempotency_key='version')
        assert isinstance(preview, Preview)
        done = versions.write('add', add, actor='operator', idempotency_key='version', confirm=preview.token)
        assert isinstance(done, Done)
        lock = {'version': done.result['version_id']}
        preview = versions.write('lock', lock, actor='operator', idempotency_key='lock')
        assert isinstance(preview, Preview)
        self.assertIsInstance(versions.write('lock', lock, actor='operator', idempotency_key='lock', confirm=preview.token), Done)
        self.drain()
        self.assertEqual(self.status(identifier), 'In Progress')
        self.assertEqual(status_writes(), before + 1)

        self.fake.add_data_source('specs')
        self.fake.add_page('specs', package_spec_properties(package_kind='delivery', version='1'))
        packages = PackageStore(self.db, PreviewTokens('secret'), PackageSpecReader(self.client, 'specs'))
        deliver = {'deliverable': identifier, 'carrier': 'drive', 'ref': None}
        preview = packages.deliver(deliver, actor='operator', idempotency_key='deliver')
        assert isinstance(preview, Preview), preview
        self.assertIsInstance(packages.deliver(deliver, actor='operator', idempotency_key='deliver', confirm=preview.token), Done)
        self.drain()
        self.assertEqual(self.status(identifier), 'Delivered')
        self.assertEqual(status_writes(), before + 2)

    def test_sequences_projection_skips_and_manual_edit(self) -> None:
        expected = {
            'film': ['Waiting for Lock', 'Waiting for Lock', 'In Progress', 'In Progress', 'Mastering', 'QC Passed', 'Delivered', 'Archiving', 'Archived'],
            'color_only': ['In Progress'] * 6 + ['Archiving', 'Archived'],
            'sound_only': ['In Progress'] * 6 + ['Archiving', 'Archived'],
            'tvc': ['In Progress', 'In Progress', 'Mastering', 'Delivered', 'Archiving', 'Archived'],
            'daily': ['In Progress'] * 7 + ['Archiving', 'Archived'],
        }
        for template, milestones in TEMPLATES.items():
            identifier = self.define(template, template)
            self.drain()
            self.assertEqual(self.status(identifier), 'Staged')
            for milestone, want in zip(milestones, expected[template]):
                self.record(identifier, milestone)
                self.drain()
                self.assertEqual(self.status(identifier), want, (template, milestone))
            self.record(identifier, milestones[0])
            self.assertEqual(len(self.db.undispatched_deliverable_sync()), 0)
            self.assertEqual(self.status(identifier), 'Archived')
            skipped = self.define('skipped-' + template, template)
            self.drain()
            self.record(skipped, 'archiving')
            self.record(skipped, milestones[0])
            self.drain()
            self.assertEqual(self.status(skipped), 'Archiving')
            if template != 'film':
                refused = self.store.record_milestone({'deliverable': skipped, 'milestone': 'color delivered'}, actor='operator')
                self.assertIsInstance(refused, Refused)
        identifier = self.define('skipped', 'film')
        self.drain()
        self.record(identifier, 'delivered')
        self.drain()
        self.assertEqual(self.status(identifier), 'Delivered')
        page = self.page(identifier)
        status_id = DeliverablesRow.notion_id('status')
        assert status_id
        self.client.update_page(page, {'properties': {status_id: {'select': {'name': 'Blocked'}}}})
        job = WorkerJob(job_id=1, queue='main', token='x', kind='notion.page_changed', dedupe_key='event', payload={'page_id': page, 'updated_properties': [status_id], 'authors': [{'name': 'Editor'}]}, attempt=1)
        self.projection.reconcile(job)
        self.assertEqual(codec.select(self.client.retrieve_page(page)['properties']['Status']), 'Delivered')
        self.assertTrue(any('Editor' in entry.summary and 'Status is derived; record the milestone instead' in entry.summary for entry in self.db.activity(self.project)))
        count = len(self.db.activity(self.project))
        self.projection.reconcile(job)
        self.assertEqual(len(self.db.activity(self.project)), count)
