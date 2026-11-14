"""Notion webhook edits reconcile through the real worker and fake Notion."""
from test_registry_sync import SyncTestCase
from t6.notion import codec


class NotionEditTests(SyncTestCase):
    def webhook(self, page: str, kind: str = 'page.properties_updated') -> None:
        from datetime import datetime, timezone
        from test_server_app import notion_request
        self.events += 1
        properties = self.notion.page(page)['properties']
        ids = [properties[name]['id'] for name in ('T6 ID', 'Project Code', 'Rotation', 'Status') if name in properties]
        event = {'id': f'event-{self.events}', 'timestamp': datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(),
                 'type': kind, 'entity': {'id': page, 'type': 'page'}, 'authors': getattr(self, 'authors', []), 'data': {'updated_properties': ids}}
        self.assertEqual(self.app.public(notion_request(self.clock, event)).status, 200)

    def test_project_code_is_restored_and_attributed(self) -> None:
        page = self.page()
        project = self.new('SHOW', page=page)
        self.drain()
        self.client.update_page(page, {'properties': {'Project Code': {'select': {'name': 'OTHER'}}}})
        self.webhook(page)
        self.drain()
        self.assertEqual(codec.select(self.notion.page(page)['properties']['Project Code']), 'SHOW')
        self.assertTrue(any('edited in Notion' in entry.summary and 'put back' in entry.summary
                            for entry in self.registry.activity(project['id'])))

    def test_job_order_status_edit_runs_submit(self) -> None:
        from t6.job_order import JobOrderStore
        from t6.preview import PreviewTokens
        from t6.verbs import Preview, Done
        from t6.notion.jo_projection import JobOrderProjection
        from t6.worker import QueueClient, Worker
        from t6.worker.registry import RegistryClient
        from test_registry_service import WORKER_SECRET
        from test_server_app import InProcess
        page = self.page()
        project = self.new('SHOW', page=page)
        self.drain()
        from t6.ids import EntityKind, T6Id
        from t6.person import People
        people = People(self.registry, PreviewTokens('secret'))
        person_preview = people.write('add', {'page_id': 'contact', 'kind': 'staff'}, actor='setup', idempotency_key='person')
        assert isinstance(person_preview, Preview)
        person = people.write('add', {'page_id': 'contact', 'kind': 'staff'}, actor='setup', idempotency_key='person', confirm=person_preview.token)
        assert isinstance(person, Done)
        self.registry.bind_entity('notion_user', 'user-1', T6Id.parse(person.result['person_id'], EntityKind.PERSON), actor='setup')
        with self.registry._transaction() as db:
            db.execute("INSERT INTO person_permissions VALUES (?, 'coordinator')", (person.result['person_id'],))
        self.authors = [{'id': 'user-1', 'name': 'Ada'}]
        self.notion.add_snapshot_data_source('job-orders', 'job_orders')
        self.notion.add_property('job-orders', 'T6 ID', 'rich_text')
        store = JobOrderStore(self.registry, PreviewTokens('secret'))
        values = {'fields': {'project_id': page, 'name': 'JO'}}
        preview = store.write('raise', values, actor='setup', idempotency_key='raise')
        assert isinstance(preview, Preview)
        result = store.write('raise', values, actor='setup', idempotency_key='raise', confirm=preview.token)
        assert isinstance(result, Done)
        transport = InProcess(self.app)
        projection = JobOrderProjection(self.client, RegistryClient(transport, WORKER_SECRET), 'job-orders')
        worker = Worker(QueueClient(transport, WORKER_SECRET), 'main', projection.handlers())
        self.assertTrue(worker.run_once())
        jo = store.get(result.result['jo_id'])
        assert jo is not None
        jo_page = jo['bindings'][0]['page_id']
        self.client.update_page(jo_page, {'properties': {'Status': {'status': {'name': 'In progress'}}}})
        self.webhook(jo_page)
        self.assertTrue(worker.run_once())
        current = store.get(jo_page)
        assert current is not None
        self.assertEqual(current['state']['status'], 'In progress')
        self.assertTrue(any('set in Notion by Ada' in entry.summary and entry.actor == person.result['person_id'] for entry in self.registry.activity(project['id'])))
        while worker.run_once():
            pass
        before = len(self.registry.activity(project['id']))
        self.webhook(jo_page)
        self.assertTrue(worker.run_once())
        self.assertEqual(len(self.registry.activity(project['id'])), before)
        with self.registry._transaction() as db:
            db.execute("DELETE FROM person_permissions WHERE person_id = ? AND permission = 'coordinator'", (person.result['person_id'],))
        self.client.update_page(jo_page, {'properties': {'Status': {'status': {'name': 'Done'}}}})
        self.webhook(jo_page)
        self.assertTrue(worker.run_once())
        current = store.get(jo_page)
        assert current is not None
        self.assertEqual(current['state']['status'], 'In progress')
        self.assertTrue(any('coordinator' in str(item.detail.get('reason', '')) for item in self.registry.review_items()))
        with self.registry._transaction() as db:
            db.execute("INSERT INTO person_permissions VALUES (?, 'coordinator')", (person.result['person_id'],))
        self.client.update_page(jo_page, {'properties': {'Status': {'status': {'name': 'In progress'}}}})
        self.webhook(jo_page)
        self.assertTrue(worker.run_once())
        self.authors = [{'id': 'unbound-user', 'name': person.result['person_id']}]
        self.client.update_page(jo_page, {'properties': {'Status': {'status': {'name': 'Done'}}}})
        self.webhook(jo_page)
        self.assertTrue(worker.run_once())
        current = store.get(jo_page)
        assert current is not None
        self.assertEqual(current['state']['status'], 'In progress')
        self.authors = [{'id': 'user-1', 'name': 'Ada'}]
        self.client.update_page(jo_page, {'properties': {'Status': {'status': {'name': 'In progress'}}}})
        self.webhook(jo_page)
        self.assertTrue(worker.run_once())
        self.client.update_page(jo_page, {'properties': {'Status': {'status': {'name': 'Needs Intake Review'}}}})
        self.webhook(jo_page)
        self.assertTrue(worker.run_once())
        current = store.get(jo_page)
        assert current is not None
        self.assertEqual(current['state']['status'], 'In progress')
        self.assertEqual(codec.status(self.notion.page(jo_page)['properties']['Status']), 'Needs Intake Review')
        self.assertTrue(any(item.kind == 'notion_state_edit' for item in self.registry.review_items()))
        self.assertTrue(any('flagged' in entry.summary for entry in self.registry.activity(project['id'])))
        before = len(self.registry.activity(project['id']))
        self.start()
        transport = InProcess(self.app)
        projection = JobOrderProjection(self.client, RegistryClient(transport, WORKER_SECRET), 'job-orders')
        worker = Worker(QueueClient(transport, WORKER_SECRET), 'main', projection.handlers())
        self.webhook(jo_page)
        self.assertTrue(worker.run_once())
        self.assertEqual(len(self.registry.activity(project['id'])), before)
        block = store.write('block', {'jo': result.result['jo_id'], 'reason': 'waiting'}, actor='setup', idempotency_key='block')
        assert isinstance(block, Preview)
        self.assertIsInstance(store.write('block', {'jo': result.result['jo_id'], 'reason': 'waiting'}, actor='setup', idempotency_key='block', confirm=block.token), Done)
        self.start()
        self.assertTrue(worker.run_once())
        self.assertEqual(codec.status(self.notion.page(jo_page)['properties']['Status']), 'Needs Intake Review')

    def test_editor_is_resolved_to_person(self) -> None:
        from datetime import datetime, timezone
        from t6.ids import EntityKind, T6Id
        from t6.person import People
        from t6.preview import PreviewTokens
        from t6.verbs import Preview, Done
        from test_server_app import notion_request
        page = self.page()
        project = self.new('SHOW', page=page)
        self.drain()
        people = People(self.registry, PreviewTokens('secret'))
        preview = people.write('add', {'page_id': 'contact', 'kind': 'staff'}, actor='setup', idempotency_key='person')
        assert isinstance(preview, Preview)
        person = people.write('add', {'page_id': 'contact', 'kind': 'staff'}, actor='setup', idempotency_key='person', confirm=preview.token)
        assert isinstance(person, Done)
        self.registry.bind_entity('notion_user', 'user-1', T6Id.parse(person.result['person_id'], EntityKind.PERSON), actor='setup')
        self.client.update_page(page, {'properties': {'Project Code': {'select': {'name': 'OTHER'}}}})
        self.notion.add_user('user-1', 'Ada')
        event = {'id': 'editor-event', 'timestamp': datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(),
                 'type': 'page.properties_updated', 'entity': {'id': page, 'type': 'page'},
                 'authors': [{'id': 'user-1'}], 'data': {}}
        self.assertEqual(self.app.public(notion_request(self.clock, event)).status, 200)
        self.drain()
        entry = next(e for e in self.registry.activity(project['id']) if 'edited in Notion' in e.summary)
        self.assertEqual(entry.actor, person.result['person_id'])
        self.assertIn('Ada', entry.summary)

    def test_project_status_resume_uses_verb(self) -> None:
        from datetime import datetime, timezone
        from test_server_app import notion_request
        from t6.project_writes import ProjectWrites
        from t6.preview import PreviewTokens
        from t6.verbs import Preview, Done
        page = self.page()
        project = self.new('SHOW', page=page)
        self.drain()
        from t6.ids import EntityKind, T6Id
        from t6.person import People
        people = People(self.registry, PreviewTokens('secret'))
        person_preview = people.write('add', {'page_id': 'contact', 'kind': 'staff'}, actor='setup', idempotency_key='person')
        assert isinstance(person_preview, Preview)
        person = people.write('add', {'page_id': 'contact', 'kind': 'staff'}, actor='setup', idempotency_key='person', confirm=person_preview.token)
        assert isinstance(person, Done)
        self.registry.bind_entity('notion_user', 'user-1', T6Id.parse(person.result['person_id'], EntityKind.PERSON), actor='setup')
        with self.registry._transaction() as db:
            db.execute("INSERT INTO person_permissions VALUES (?, 'operator')", (person.result['person_id'],))
        writer = ProjectWrites(self.registry, PreviewTokens('secret'))
        preview = writer.run('hold', project['id'], 'waiting', actor='setup', idempotency_key='hold')
        assert isinstance(preview, Preview)
        self.assertIsInstance(writer.run('hold', project['id'], 'waiting', actor='setup', idempotency_key='hold', confirm=preview.token), Done)
        self.client.update_page(page, {'properties': {'Status': {'select': {'name': 'In-Progress'}}}})
        prop_id = self.notion.page(page)['properties']['Status']['id']
        event = {'id': 'project-status', 'timestamp': datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(),
                 'type': 'page.properties_updated', 'entity': {'id': page, 'type': 'page'},
                 'authors': [{'id': 'user-1', 'name': 'Ada'}], 'data': {'updated_properties': [prop_id]}}
        self.assertEqual(self.app.public(notion_request(self.clock, event)).status, 200)
        self.drain()
        resume = writer.run('resume', project['id'], None, actor='setup', idempotency_key='again')
        from t6.verbs import Refused
        assert isinstance(resume, Refused)
        self.assertEqual(resume.rule, 'project_not_held')
        self.assertTrue(any('Status set in Notion by Ada' in entry.summary for entry in self.registry.activity(project['id'])))

    def test_health_without_a_matching_verb_is_flagged(self) -> None:
        from datetime import datetime, timezone
        from test_server_app import notion_request
        page = self.page()
        project = self.new('SHOW', page=page)
        self.drain()
        self.client.update_page(page, {'properties': {'Health': {'select': {'name': 'At Risk'}}}})
        prop_id = self.notion.page(page)['properties']['Health']['id']
        event = {'id': 'project-health', 'timestamp': datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(),
                 'type': 'page.properties_updated', 'entity': {'id': page, 'type': 'page'},
                 'authors': [{'id': 'unknown', 'name': 'Ada'}], 'data': {'updated_properties': [prop_id]}}
        self.assertEqual(self.app.public(notion_request(self.clock, event)).status, 200)
        self.drain()
        self.assertEqual(codec.select(self.notion.page(page)['properties']['Health']), 'At Risk')
        self.assertTrue(any(item.kind == 'notion_project_health' for item in self.registry.review_items()))
        self.assertTrue(any('Health' in entry.summary and 'Ada' in entry.summary for entry in self.registry.activity(project['id'])))

    def test_old_jo_projection_does_not_overwrite_newer_status(self) -> None:
        from t6.job_order import JobOrderStore
        from t6.preview import PreviewTokens
        from t6.verbs import Preview, Done
        from t6.notion.jo_projection import JobOrderProjection
        from t6.worker import QueueClient, Worker
        from t6.worker.registry import RegistryClient
        from test_registry_service import WORKER_SECRET
        from test_server_app import InProcess
        page = self.page()
        self.new('SHOW', page=page)
        self.drain()
        self.notion.add_snapshot_data_source('job-orders', 'job_orders')
        self.notion.add_property('job-orders', 'T6 ID', 'rich_text')
        store = JobOrderStore(self.registry, PreviewTokens('secret'))
        fields = {'fields': {'project_id': page, 'name': 'JO'}}
        p = store.write('raise', fields, actor='setup', idempotency_key='raise')
        assert isinstance(p, Preview)
        result = store.write('raise', fields, actor='setup', idempotency_key='raise', confirm=p.token)
        assert isinstance(result, Done)
        transport = InProcess(self.app)
        worker = Worker(QueueClient(transport, WORKER_SECRET), 'main', JobOrderProjection(self.client, RegistryClient(transport, WORKER_SECRET), 'job-orders').handlers())
        self.assertTrue(worker.run_once())
        for verb in ('submit', 'close'):
            p = store.write(verb, {'jo': result.result['jo_id']}, actor='setup', idempotency_key=verb)
            assert isinstance(p, Preview)
            self.assertIsInstance(store.write(verb, {'jo': result.result['jo_id']}, actor='setup', idempotency_key=verb, confirm=p.token), Done)
        self.start()
        record = store.get(result.result['jo_id'])
        assert record is not None
        jo_page = record['bindings'][0]['page_id']
        self.client.update_page(jo_page, {'properties': {'Status': {'status': {'name': 'Done'}}}})
        transport = InProcess(self.app)
        RegistryClient(transport, WORKER_SECRET).projection(jo_page, {'jo_status': 'Done'}, record=True)
        worker = Worker(QueueClient(transport, WORKER_SECRET), 'main', JobOrderProjection(self.client, RegistryClient(transport, WORKER_SECRET), 'job-orders').handlers())
        self.assertTrue(worker.run_once())
        self.assertEqual(codec.status(self.notion.page(jo_page)['properties']['Status']), 'Done')
