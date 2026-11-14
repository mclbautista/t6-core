"""JO outbox reaches fake Notion through the main worker."""
from test_registry_sync import SyncTestCase
from test_registry_service import WORKER_SECRET
from test_server_app import InProcess

from t6.job_order import JobOrderStore
from t6.notion import codec
from t6.preview import PreviewTokens
from t6.verbs import Done, Preview
from t6.worker import QueueClient, Worker
from t6.worker.registry import RegistryClient


class JoSyncTests(SyncTestCase):
    def test_raise_outbox_creates_one_page_and_worker_retry_does_not_duplicate(self) -> None:
        from t6.notion.jo_projection import JobOrderProjection
        project_page = self.page()
        project = self.new('SHOW', page=project_page)
        self.drain()
        source = 'job-orders'
        self.notion.add_snapshot_data_source(source, 'job_orders')
        self.notion.add_property(source, 'T6 ID', 'rich_text')
        self.notion.add_property(source, 'T6 Notes', 'rich_text')
        store = JobOrderStore(self.registry, PreviewTokens('secret'))
        values = {'fields': {'project_id': project_page, 'name': 'SHOW - AE - JO #1', 'number': '1', 'department': 'TOC', 'date': '2026-11-14', 'deadline': '2026-11-14T17:00:00+08:00', 'deliverable_ids': ['episode-page'], 'watermark': 'none'}}
        preview = store.write('raise', values, actor='alice', idempotency_key='raise')
        assert isinstance(preview, Preview)
        result = store.write('raise', values, actor='alice', idempotency_key='raise', confirm=preview.token)
        assert isinstance(result, Done)
        identifier = result.result['jo_id']
        transport = InProcess(self.app)
        projection = JobOrderProjection(self.client, RegistryClient(transport, WORKER_SECRET), source)
        worker = Worker(QueueClient(transport, WORKER_SECRET), 'main', projection.handlers())
        self.assertTrue(worker.run_once())
        record = store.get(identifier)
        assert record is not None
        page = record['bindings'][0]['page_id']
        replay = store.write('raise', values, actor='alice', idempotency_key='raise')
        assert isinstance(replay, Preview)
        recovered = store.write('raise', values, actor='alice', idempotency_key='raise', confirm=replay.token)
        assert isinstance(recovered, Done)
        self.assertEqual(recovered.result['page_id'], page)
        self.assertEqual(codec.rich_text(self.notion.page(page)['properties']['T6 ID']), identifier)
        from t6.notion.job_orders import JobOrders
        saved = JobOrders(self.client, source).get(page)
        self.assertEqual((saved.department, saved.number, saved.deliverable_ids, saved.watermark), ('TOC', '1', ('episode-page',), 'none'))
        assert saved.deadline is not None
        self.assertEqual(saved.deadline.isoformat(), '2026-11-14T17:00:00+08:00')
        self.assertEqual(len([r for r in self.notion.requests if r.method == 'POST' and r.url.path == '/v1/pages']), 1)
        self.start()
        self.assertFalse(worker.run_once())
        found = store.get(page)
        assert found is not None
        self.assertEqual(found['id'], identifier)
        self.assertEqual(project['id'], found['project_id'])

    def test_assignment_and_notes_are_projected_to_the_existing_page(self) -> None:
        from t6.notion.jo_projection import JobOrderProjection
        from t6.person import People
        from t6.notion.generated import JobOrdersRow
        project_page = self.page()
        project = self.new('SHOW', page=project_page)
        self.drain()
        self.notion.add_snapshot_data_source('job-orders', 'job_orders')
        self.notion.add_property('job-orders', 'T6 ID', 'rich_text')
        self.notion.add_property('job-orders', 'T6 Notes', 'rich_text')
        store = JobOrderStore(self.registry, PreviewTokens('secret'))
        people = People(self.registry, PreviewTokens('secret'))
        person_values = {'page_id': 'contact-page', 'kind': 'staff'}
        p = people.write('add', person_values, actor='alice', idempotency_key='person')
        assert isinstance(p, Preview)
        person = people.write('add', person_values, actor='alice', idempotency_key='person', confirm=p.token)
        assert isinstance(person, Done)
        values = {'fields': {'project_id': project_page, 'name': 'SHOW - AE - JO #1', 'notes': 'original'}}
        p = store.write('raise', values, actor='alice', idempotency_key='raise')
        assert isinstance(p, Preview)
        made = store.write('raise', values, actor='alice', idempotency_key='raise', confirm=p.token)
        assert isinstance(made, Done)
        jo_id = made.result['jo_id']
        for verb, data in [('assign', {'jo': jo_id, 'person': person.result['person_id']}), ('note', {'jo': jo_id, 'text': 'New note'})]:
            p = store.write(verb, data, actor='alice', idempotency_key=verb)
            assert isinstance(p, Preview)
            self.assertIsInstance(store.write(verb, data, actor='alice', idempotency_key=verb, confirm=p.token), Done)
        transport = InProcess(self.app)
        worker = Worker(QueueClient(transport, WORKER_SECRET), 'main', JobOrderProjection(self.client, RegistryClient(transport, WORKER_SECRET), 'job-orders').handlers())
        while worker.run_once():
            pass
        record = store.get(jo_id)
        assert record is not None
        page = self.notion.page(record['bindings'][0]['page_id'])
        row = JobOrdersRow.from_page(page)
        self.assertEqual(row.assigned, ('contact-page',))
        self.assertEqual(row.jo_notes, 'original')
        self.assertEqual(codec.rich_text(page['properties']['T6 Notes']), 'New note')
        self.assertEqual([e.action for e in self.registry.activity(project['id'])[:3]], ['jo_note', 'jo_assign', 'jo_raise'])

    def test_migration_projects_identity_and_preserves_existing_notes(self) -> None:
        from t6.notion.jo_projection import JobOrderProjection
        from t6.notion.job_orders import JobOrders
        from t6.notion.generated import JobOrdersRow
        from t6.notion.testing import rich_text, title
        project_page = self.page()
        self.new('SHOW', page=project_page)
        self.drain()
        self.notion.add_snapshot_data_source('job-orders', 'job_orders')
        self.notion.add_property('job-orders', 'T6 ID', 'rich_text')
        self.notion.add_property('job-orders', 'T6 Notes', 'rich_text')
        raw = self.notion.add_page('job-orders', {
            JobOrdersRow.notion_name('name'): title('SHOW - AE - JO #1'),
            JobOrdersRow.notion_name('jo_notes'): rich_text('Original note'),
            JobOrdersRow.notion_name('projects'): {'type': 'relation', 'relation': [{'id': project_page}]},
        })
        orders = JobOrders(self.client, 'job-orders').list()
        store = JobOrderStore(self.registry, PreviewTokens('secret'))
        preview = store.migrate(orders, actor='alice')
        applied = store.migrate(orders, actor='alice', apply=True, digest=preview['digest'])
        jo_id = applied['entries'][0]['t6_id']
        note = {'jo': jo_id, 'text': 'Added'}
        token = store.write('note', note, actor='alice', idempotency_key='note')
        assert isinstance(token, Preview)
        self.assertIsInstance(store.write('note', note, actor='alice', idempotency_key='note', confirm=token.token), Done)
        transport = InProcess(self.app)
        worker = Worker(QueueClient(transport, WORKER_SECRET), 'main', JobOrderProjection(self.client, RegistryClient(transport, WORKER_SECRET), 'job-orders').handlers())
        while worker.run_once():
            pass
        row = JobOrdersRow.from_page(self.notion.page(raw['id']))
        self.assertEqual(row.jo_notes, 'Original note')
        self.assertEqual(codec.rich_text(self.notion.page(raw['id'])['properties']['T6 Notes']), 'Added')
        self.assertEqual(codec.rich_text(self.notion.page(raw['id'])['properties']['T6 ID']), jo_id)

    def test_create_response_lost_recovers_by_t6_id_without_another_create(self) -> None:
        from unittest.mock import patch
        import httpx
        from t6.notion.jo_projection import JobOrderProjection
        project_page = self.page()
        self.new('SHOW', page=project_page)
        self.drain()
        self.notion.add_snapshot_data_source('job-orders', 'job_orders')
        self.notion.add_property('job-orders', 'T6 ID', 'rich_text')
        self.notion.add_property('job-orders', 'T6 Notes', 'rich_text')
        store = JobOrderStore(self.registry, PreviewTokens('secret'))
        values = {'fields': {'project_id': project_page, 'name': 'SHOW - AE - JO #1', 'source_text': 'The body'}}
        p = store.write('raise', values, actor='alice', idempotency_key='raise')
        assert isinstance(p, Preview)
        result = store.write('raise', values, actor='alice', idempotency_key='raise', confirm=p.token)
        assert isinstance(result, Done)
        original = self.notion._create_page
        def lost(body: dict) -> httpx.Response:  # type: ignore[type-arg]
            original(body)
            raise httpx.ReadTimeout('response lost')
        transport = InProcess(self.app)
        worker = Worker(QueueClient(transport, WORKER_SECRET), 'main', JobOrderProjection(self.client, RegistryClient(transport, WORKER_SECRET), 'job-orders').handlers())
        with patch.object(self.notion, '_create_page', side_effect=lost):
            self.assertTrue(worker.run_once())
        record = store.get(result.result['jo_id'])
        assert record is not None
        self.assertEqual(len(record['bindings']), 1)
        self.assertEqual(self.client.list_block_children(record['bindings'][0]['page_id'])[0]['paragraph']['rich_text'][0]['plain_text'], 'The body')
        self.assertEqual(len([r for r in self.notion.requests if r.method == 'POST' and r.url.path == '/v1/pages']), 1)

    def test_worker_registration_and_sync_access_are_scoped(self) -> None:
        from test_registry_service import READER_SECRET
        from t6.worker.__main__ import WorkerSettings, configured_handlers
        transport = InProcess(self.app)
        settings = WorkerSettings('http://server.invalid', WORKER_SECRET, 'main')
        handlers = configured_handlers({'T6_NOTION_TOKEN': 'fake', 'T6_NOTION_JOB_ORDERS_DATA_SOURCE': 'job-orders'}, settings, transport)
        self.assertIn('jo.sync', handlers['main'])
        self.assertEqual(self.send('GET', '/registry/jo-sync/1', secret=READER_SECRET).status, 403)
        self.assertEqual(self.send('POST', '/registry/jo-sync/1', {'begin': True}, secret=READER_SECRET).status, 403)
