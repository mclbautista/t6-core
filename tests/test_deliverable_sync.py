"""The Deliverable outbox creates a row in fake Notion."""
from unittest.mock import patch
from test_registry_sync import SyncTestCase
from test_registry_service import WORKER_SECRET
from test_server_app import InProcess
from t6.deliverable import DeliverableStore
from t6.notion.deliverable_projection import DeliverableProjection
from t6.notion.generated import DeliverablesRow
from t6.preview import PreviewTokens
from t6.verbs import Done, Preview
from t6.worker import QueueClient, Worker
from t6.worker.registry import RegistryClient


class DeliverableSyncTests(SyncTestCase):
    def test_create_and_replay(self) -> None:
        project_page = self.page()
        project = self.new('SHOW', page=project_page)
        self.drain()
        self.notion.add_snapshot_data_source('deliverables', 'deliverables')
        self.notion.add_property('deliverables', 'T6 ID', 'rich_text')
        store = DeliverableStore(self.registry, PreviewTokens('secret'))
        values = {'project': project['id'], 'code': 'Ep01', 'episode': None, 'due': '2026-12-01', 'air': None}
        preview = store.define(values, actor='alice', idempotency_key='define')
        assert isinstance(preview, Preview)
        result = store.define(values, actor='alice', idempotency_key='define', confirm=preview.token)
        assert isinstance(result, Done)
        transport = InProcess(self.app)
        projection = DeliverableProjection(self.client, RegistryClient(transport, WORKER_SECRET), 'deliverables')
        worker = Worker(QueueClient(transport, WORKER_SECRET), 'main', {'deliverable.sync': projection.sync})
        property_id = self.notion._data_sources['deliverables']['properties']['T6 ID']['id']
        with patch('t6.notion.deliverable_projection.projection_id', return_value=property_id):
            self.assertTrue(worker.run_once())
        record = store.get(result.result['deliverable_id'])
        assert record is not None
        page = self.notion.page(record['bindings'][0]['page_id'])
        self.assertEqual(DeliverablesRow.from_page(page).name, 'Ep01')
        self.assertEqual(len(self.client.query_data_source('deliverables')), 1)

    def test_missing_snapshot_id_refuses(self) -> None:
        from t6.worker.client import WorkerJob
        from t6.worker.runner import Refused
        job = WorkerJob(job_id=1, queue='main', token='token', kind='deliverable.sync', dedupe_key='test', payload={'intent_id': 1}, attempt=1)
        with self.assertRaisesRegex(Refused, 'schema-snapshot'):
            DeliverableProjection(self.client, RegistryClient(InProcess(self.app), WORKER_SECRET), 'deliverables').sync(job)

    def test_identity_edit_is_restored_by_property_id(self) -> None:
        from unittest.mock import patch
        from t6.notion import codec
        from t6.worker.client import WorkerJob
        page = self.page()
        project = self.new('SHOW', page=page)
        self.drain()
        self.notion.add_snapshot_data_source('deliverables', 'deliverables')
        self.notion.add_property('deliverables', 'T6 ID', 'rich_text')
        prop_id = self.notion._data_sources['deliverables']['properties']['T6 ID']['id']
        self.notion.rename_property('deliverables', 'T6 ID', 'Renamed identity')
        store = DeliverableStore(self.registry, PreviewTokens('secret'))
        values = {'project': project['id'], 'code': 'Ep01', 'episode': None, 'due': None, 'air': None}
        preview = store.define(values, actor='alice', idempotency_key='define')
        assert isinstance(preview, Preview)
        done = store.define(values, actor='alice', idempotency_key='define', confirm=preview.token)
        assert isinstance(done, Done)
        projection = DeliverableProjection(self.client, RegistryClient(InProcess(self.app), WORKER_SECRET), 'deliverables')
        worker = Worker(QueueClient(InProcess(self.app), WORKER_SECRET), 'main', {'deliverable.sync': projection.sync})
        with patch('t6.notion.deliverable_projection.projection_id', return_value=prop_id):
            self.assertTrue(worker.run_once())
            record = store.get(done.result['deliverable_id'])
            assert record is not None
            deliverable_page = record['bindings'][0]['page_id']
            self.client.update_page(deliverable_page, {'properties': {prop_id: {'rich_text': [{'text': {'content': 'wrong'}}]}}})
            job = WorkerJob(job_id=2, queue='main', token='token', kind='notion.page_changed', dedupe_key='edit', payload={'page_id': deliverable_page, 'updated_properties': [prop_id]}, attempt=1)
            projection.reconcile(job)
            self.assertEqual(codec.rich_text(self.notion.page(deliverable_page)['properties']['Renamed identity']), record['id'])
            self.assertTrue(any(e.subject_id == record['id'] and e.action == 'notion_identity_edit' for e in self.registry.activity(project['id'])))
            count = len(self.registry.activity(project['id']))
            projection.reconcile(job)
            self.assertEqual(len(self.registry.activity(project['id'])), count)

    def test_worker_registers_handler_and_restricts_observation(self) -> None:
        from t6.worker.__main__ import WorkerSettings, configured_handlers
        from test_registry_service import READER_SECRET
        transport = InProcess(self.app)
        settings = WorkerSettings('http://server.invalid', WORKER_SECRET, 'main')
        env = {'T6_NOTION_TOKEN': 'fake', 'T6_NOTION_PROJECTS_DATA_SOURCE': 'projects',
               'T6_NOTION_JOB_ORDERS_DATA_SOURCE': 'job-orders', 'T6_NOTION_DELIVERABLES_DATA_SOURCE': 'deliverables'}
        handlers = configured_handlers(env, settings, transport)
        self.assertIn('deliverable.sync', handlers['main'])
        self.assertIn('notion.page_changed', handlers['main'])
        self.assertEqual(self.send('GET', '/registry/deliverable-sync/1', secret=READER_SECRET).status, 403)
        self.assertEqual(self.send('POST', '/registry/notion-deliverable/page', {}, secret=READER_SECRET).status, 403)
