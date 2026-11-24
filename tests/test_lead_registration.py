"""A new Projects page is registered as a Lead when it is created (t6-core #710)."""
import unittest
from typing import Any

from test_registry_sync import PROJECTS, SyncTestCase
from t6.notion import codec
from t6.notion.testing import project_properties
from t6.notion_edits import NotionEdits
from t6.registry import Registered


class LeadRegistrationTests(SyncTestCase):
    def setUp(self) -> None:
        super().setUp()
        for name in ('📝 Proposal Made', '🚨 In-Progress'):
            self.notion.add_option(PROJECTS, 'Status', name)

    def created(self, status: str | None = None) -> str:
        self.notion.add_option(PROJECTS, 'Status', '🧲 Leads')
        page = str(self.notion.add_page(PROJECTS, project_properties(name='New Show', status=status), page_id='0a' * 16)['id'])
        self.webhook(page, kind='page.created')
        self.drain()
        return page

    def projects(self) -> list[Any]:
        with self.registry._transaction() as db:
            return db.execute('SELECT id, code, rotation FROM projects').fetchall()

    def stages(self) -> list[Any]:
        with self.registry._transaction() as db:
            return db.execute('SELECT to_value FROM project_stage_history ORDER BY id').fetchall()

    def status(self, page: str) -> str | None:
        return codec.select(self.notion.page(page)['properties']['Status'])

    def test_a_new_page_gets_an_id_and_status_leads_without_a_card(self) -> None:
        page = self.created()
        ((project, code, rotation),) = self.projects()
        self.assertTrue(project.startswith('t6p_'))
        self.assertEqual((code, rotation), (None, 'out'))
        self.assertEqual(self.status(page), '🧲 Leads')
        self.assertEqual(self.shown(page)[0], project)
        self.assertEqual(self.stages(), [('Leads',)])
        self.assertEqual(self.items(), [])

    def test_a_page_created_with_another_status_keeps_it_and_is_registered(self) -> None:
        page = self.created('📝 Proposal Made')
        self.assertEqual(len(self.projects()), 1)
        self.assertEqual(self.status(page), '📝 Proposal Made')
        self.assertEqual(self.stages(), [('Proposal Made',)])

    def test_a_page_created_in_progress_is_registered_with_no_stage(self) -> None:
        self.created('🚨 In-Progress')
        self.assertEqual(len(self.projects()), 1)
        self.assertEqual(self.stages(), [])

    def test_a_replayed_event_registers_once(self) -> None:
        page = self.created()
        edits = NotionEdits(self.registry)
        again = edits.register_created_page(page, 'event-1', 'Leads', None, 'main-worker')
        other = edits.register_created_page(page, 'event-9', 'Leads', None, 'main-worker')
        self.assertEqual((again['outcome'], other['outcome']), ('replayed', 'already_bound'))
        self.assertEqual(len(self.projects()), 1)
        self.assertEqual(self.stages(), [('Leads',)])

    def test_the_relations_a_page_is_created_with_are_the_baseline_for_its_first_edit(self) -> None:
        self.notion.add_option(PROJECTS, 'Status', '🧲 Leads')
        studio = str(self.notion.add_page(PROJECTS, project_properties(name='Acme'), page_id='0d' * 16)['id'])
        page = str(self.notion.add_page(PROJECTS, project_properties(name='New Show', studio_id=studio), page_id='0e' * 16)['id'])
        self.webhook(page, kind='page.created')
        self.drain()
        edits = NotionEdits(self.registry)
        self.assertEqual(edits.crm_edit(page, 'Studio', ['Acme', 'Beta'], 'Ada', 'e1')['outcome'], 'recorded')
        with self.registry._transaction() as db:
            self.assertEqual(db.execute('SELECT from_value, to_value FROM crm_edit_history').fetchall(), [('Acme', 'Acme, Beta')])

    def test_an_edit_of_an_unregistered_page_does_not_register_it(self) -> None:
        page = str(self.notion.add_page(PROJECTS, project_properties(name='Old Show'), page_id='0b' * 16)['id'])
        self.webhook(page)
        self.drain()
        self.assertEqual(self.projects(), [])
        self.assertEqual([item['kind'] for item in self.items()], ['unbound_page'])

    def test_registration_still_happens_when_the_leads_option_is_missing(self) -> None:
        page = str(self.notion.add_page(PROJECTS, project_properties(name='New Show'), page_id='0c' * 16)['id'])
        self.webhook(page, kind='page.created')
        self.drain()
        self.assertEqual(len(self.projects()), 1)
        self.assertIsNone(self.status(page))
        self.assertEqual(self.stages(), [])


if __name__ == '__main__':
    unittest.main()
