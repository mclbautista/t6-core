"""CRM stages (t6-core #709): Notion Status edits to Leads..Lost are accepted and kept as stage history."""
import copy
import unittest
from typing import Any

from test_registry_sync import SyncTestCase
from t6.notion.schema import load_snapshot
from t6.notion_edits import NotionEdits

STATUS_PROPERTY = '%3DR%7DW'


def snapshot_with_stages(**ids: str) -> dict[str, Any]:
    snapshot = copy.deepcopy(load_snapshot())
    snapshot['databases']['projects']['named_options'] = {'status': {'property': STATUS_PROPERTY, 'values': ids}}
    return snapshot


class CrmStageTests(SyncTestCase):
    def known_person(self, user: str = 'user-1') -> str:
        from t6.ids import EntityKind, T6Id
        person = str(T6Id.mint(EntityKind.PERSON))
        with self.registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES (?, 'staff', '[]', '[]', NULL, 0, 1)", (person,))
        self.registry.bind_entity('notion_user', user, T6Id.parse(person, EntityKind.PERSON), actor='setup')
        return person

    def history(self, project: str) -> list[tuple[Any, ...]]:
        with self.registry._transaction() as db:
            return db.execute('SELECT actor, field, from_value, to_value FROM project_stage_history WHERE project_id = ? ORDER BY id', (project,)).fetchall()

    def test_a_known_person_moves_a_project_through_the_stages_and_it_is_audited(self) -> None:
        page = self.page()
        project = self.new('SHOW', page=page)
        person = self.known_person()
        edits = NotionEdits(self.registry)
        self.assertEqual(edits.project_status(page, 'Leads', 'Ada', 'e1', 'user-1')['outcome'], 'accepted')
        self.assertEqual(edits.project_status(page, 'Onboarding', 'Ada', 'e2', 'user-1')['outcome'], 'accepted')
        self.assertEqual(edits.project_status(page, 'Lost', 'Ada', 'e3', 'user-1')['outcome'], 'accepted')
        self.assertEqual(edits.project_status(page, 'Leads', 'Ada', 'e4', 'user-1')['outcome'], 'accepted')
        self.assertEqual(self.history(project['id']), [(person, 'crm_stage', None, 'Leads'), (person, 'crm_stage', 'Leads', 'Onboarding'),
                                                      (person, 'crm_stage', 'Onboarding', 'Lost'), (person, 'crm_stage', 'Lost', 'Leads')])
        entry = next(e for e in self.registry.activity(project['id']) if 'Lost' in e.summary and 'Leads' in e.summary)
        self.assertEqual(entry.actor, person)
        self.assertIn('Ada', entry.summary)
        self.assertEqual(self.registry.audit(project['id'])[-1].action, 'crm_stage_changed')
        self.assertEqual(self.registry.review_items(), ())

    def test_a_notion_webhook_edit_is_accepted_end_to_end(self) -> None:
        from datetime import datetime, timezone
        from test_server_app import notion_request
        page = self.page()
        project = self.new('SHOW', page=page)
        self.drain()
        self.known_person()
        self.client.update_page(page, {'properties': {'Status': {'select': {'name': '🤝 Negotiations'}}}})
        prop_id = self.notion.page(page)['properties']['Status']['id']
        event = {'id': 'crm-1', 'timestamp': datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(),
                 'type': 'page.properties_updated', 'entity': {'id': page, 'type': 'page'},
                 'authors': [{'id': 'user-1', 'name': 'Ada'}], 'data': {'updated_properties': [prop_id]}}
        self.assertEqual(self.app.public(notion_request(self.clock, event)).status, 200)
        self.drain()
        self.assertEqual([row[3] for row in self.history(project['id'])], ['Negotiations'])
        self.assertEqual(self.registry.review_items(), ())

    def test_an_unknown_editor_is_still_flagged_and_records_nothing(self) -> None:
        page = self.page()
        project = self.new('SHOW', page=page)
        self.assertEqual(NotionEdits(self.registry).project_status(page, 'Negotiations', 'Mallory', 'e1', 'nobody')['outcome'], 'flagged')
        self.assertEqual(self.history(project['id']), [])
        self.assertEqual({item.kind for item in self.registry.review_items()}, {'notion_project_status'})

    def test_a_replayed_event_records_once(self) -> None:
        page = self.page()
        project = self.new('SHOW', page=page)
        self.known_person()
        edits = NotionEdits(self.registry)
        edits.project_status(page, 'Leads', 'Ada', 'e1', 'user-1')
        with self.registry._transaction() as db:
            db.execute("DELETE FROM notion_last_seen WHERE property = 'project_status'")
        edits.project_status(page, 'Leads', 'Ada', 'e1', 'user-1')
        self.assertEqual(len(self.history(project['id'])), 1)

    def test_a_stage_is_found_by_its_option_id_after_a_rename(self) -> None:
        page = self.page()
        project = self.new('SHOW', page=page)
        self.known_person()
        edits = NotionEdits(self.registry, snapshot_with_stages(NEGOTIATIONS='opt-neg'))
        outcome = edits.project_status(page, 'In Talks', 'Ada', 'e1', 'user-1', option_id='opt-neg')
        self.assertEqual(outcome['outcome'], 'accepted')
        self.assertEqual(self.history(project['id'])[0][3], 'Negotiations')

    def test_a_stage_with_an_emoji_name_matches(self) -> None:
        page = self.page()
        project = self.new('SHOW', page=page)
        self.known_person()
        self.assertEqual(NotionEdits(self.registry).project_status(page, '🤝 Proposal Made', 'Ada', 'e1', 'user-1')['outcome'], 'accepted')
        self.assertEqual(self.history(project['id'])[0][3], 'Proposal Made')

    def test_in_progress_after_a_stage_ends_the_crm_part_without_a_hold_or_flag(self) -> None:
        page = self.page()
        project = self.new('SHOW', page=page)
        self.known_person()
        edits = NotionEdits(self.registry)
        edits.project_status(page, 'Onboarding', 'Ada', 'e1', 'user-1')
        self.drain()
        self.assertEqual(edits.project_status(page, '🚨 In-Progress', 'Ada', 'e2', 'user-1')['outcome'], 'accepted')
        self.assertEqual(self.history(project['id'])[-1][2:], ('Onboarding', 'In-Progress'))
        self.assertEqual(self.registry.review_items(), ())
        with self.registry._transaction() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit WHERE action IN ('project_resume', 'notion_state_flagged')").fetchone()[0], 0)

    def test_in_progress_with_no_stage_keeps_todays_path(self) -> None:
        page = self.page()
        self.new('SHOW', page=page)
        self.known_person()
        self.assertEqual(NotionEdits(self.registry).project_status(page, 'In-Progress', 'Ada', 'e1', 'user-1')['outcome'], 'flagged')

    def test_stage_history_is_append_only(self) -> None:
        import sqlite3
        page = self.page()
        project = self.new('SHOW', page=page)
        self.known_person()
        NotionEdits(self.registry).project_status(page, 'Leads', 'Ada', 'e1', 'user-1')
        with self.registry._transaction() as db:
            with self.assertRaises(sqlite3.DatabaseError):
                db.execute('UPDATE project_stage_history SET to_value = ?', ('Lost',))
            with self.assertRaises(sqlite3.DatabaseError):
                db.execute('DELETE FROM project_stage_history WHERE project_id = ?', (project['id'],))


class NamedStageTests(unittest.TestCase):
    PROPERTIES = {'Status': {'id': STATUS_PROPERTY, 'type': 'select', 'select': {'options': [
        {'id': 'o1', 'name': '🌱 Leads'}, {'id': 'o2', 'name': 'Negotiations'}, {'id': 'o3', 'name': '🚨 In-Progress'}]}}}

    def test_fill_records_stage_ids_whatever_the_emoji(self) -> None:
        from t6.notion import named
        database: dict[str, Any] = {}
        named.fill(database, 'projects', self.PROPERTIES)
        self.assertEqual(database['named_options']['status']['values'], {'LEADS': 'o1', 'NEGOTIATIONS': 'o2'})
        missing = {m['value'] for m in named.missing_values(database, 'projects', self.PROPERTIES)}
        self.assertEqual(missing, {'PROPOSAL_MADE', 'ONBOARDING', 'LOST'})
