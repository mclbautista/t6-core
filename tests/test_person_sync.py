"""A Person's T6 ID is shown on its Contacts page and an edit of it is put back."""
import uuid
from unittest.mock import patch

from test_registry_sync import SyncTestCase
from test_registry_service import WORKER_SECRET
from test_server_app import InProcess
from t6.notion import codec
from t6.notion.generated import ContactsRow
from t6.notion.person_projection import PersonProjection
from t6.notion.testing import title
from t6.person import People
from t6.preview import PreviewTokens
from t6.verbs import Done, Preview
from t6.worker import QueueClient, Worker
from t6.worker.registry import RegistryClient


class PersonSyncTests(SyncTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.notion.add_snapshot_data_source('contacts', 'contacts')
        self.notion.add_property('contacts', 'T6 ID', 'rich_text')
        self.property_id = self.notion._data_sources['contacts']['properties']['T6 ID']['id']
        patcher = patch('t6.notion.person_projection.projection_id', return_value=self.property_id)
        patcher.start()
        self.addCleanup(patcher.stop)
        with self.registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES ('operator-1', 'staff', '[]', '[]', NULL, 0, 1)")
            db.execute("INSERT INTO person_permissions VALUES ('operator-1', 'operator')")
        self.page_id = str(self.notion.add_page('contacts', {ContactsRow.notion_name('name'): title('Maria')}, page_id=uuid.uuid4().hex)['id'])
        transport = InProcess(self.app)
        self.projection = PersonProjection(self.client, RegistryClient(transport, WORKER_SECRET), 'contacts')
        self.runner = Worker(QueueClient(transport, WORKER_SECRET), 'main', {'person.sync': self.projection.sync})

    def add_person(self, page: str) -> str:
        people = People(self.registry, PreviewTokens('secret'))
        values = {'page_id': page, 'kind': 'staff'}
        preview = people.write('add', values, actor='operator-1', idempotency_key='add-' + page)
        assert isinstance(preview, Preview)
        done = people.write('add', values, actor='operator-1', idempotency_key='add-' + page, confirm=preview.token)
        assert isinstance(done, Done)
        return str(done.result['person_id'])

    def contact_id(self, page: str) -> str | None:
        return ContactsRow.from_page(self.notion.page(page)).t6_id

    def test_a_new_person_shows_its_id_on_the_contacts_page(self) -> None:
        person_id = self.add_person(self.page_id)
        self.assertIsNone(self.contact_id(self.page_id))
        self.assertTrue(self.runner.run_once())
        self.assertEqual(self.contact_id(self.page_id), person_id)
        self.assertFalse(self.runner.run_once())

    def test_an_edit_of_the_id_in_notion_is_put_back(self) -> None:
        person_id = self.add_person(self.page_id)
        self.runner.run_once()
        self.notion.update_page(self.page_id, {self.property_id: {'rich_text': [{'text': {'content': 'wrong'}}]}})
        self.assertEqual(self.contact_id(self.page_id), 'wrong')
        job = type('Job', (), {'payload': {'page_id': self.page_id, 'updated_properties': [self.property_id]}, 'dedupe_key': 'k'})()
        with patch('t6.notion.person_projection.notion_editor', return_value=('Someone', None)):
            result = self.projection.reconcile(job)
        assert isinstance(result, dict)
        self.assertEqual(result['restore'], {'t6_id': person_id})
        self.assertEqual(self.contact_id(self.page_id), person_id)
        with self.registry._transaction() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit WHERE action='notion_identity_edit' AND project_id=?", (person_id,)).fetchone()[0], 1)

    def test_a_page_that_is_not_a_contact_is_left_alone(self) -> None:
        other = self.page()
        job = type('Job', (), {'payload': {'page_id': other}, 'dedupe_key': 'k'})()
        self.assertEqual(self.projection.reconcile(job), {'outcome': 'not_a_contacts_page'})
