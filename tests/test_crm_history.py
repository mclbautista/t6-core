"""CRM Sheet History tab (t6-core #713): one row per Lead edit, with names, replay-safe, newest first."""
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from test_crm_sheet import FakeSheets, job
from test_crm_stage import STATUS_PROPERTY
from test_drive_sheets import account
from test_registry_sync import SyncTestCase
from test_server_app import notion_request

from t6.notion.testing import title
from t6.notion_edits import NotionEdits
from t6.registry.sqlite import SqliteProjectRegistry
from t6.worker.internal_queue import InternalJobQueue

OTHER_SOURCE = '22222222222222222222222222222222'
LEAD_OWNER = 'dzRn'


class FakeBook:
    """Takes both writes: rows for History, a column for Projects."""
    def __init__(self) -> None:
        self.writes: list[tuple[str, list[list[str]]]] = []

    def replace_column(self, tab: str, values: list[str]) -> None:
        self.writes.append((tab, list(values)))  # type: ignore[arg-type]

    def replace_rows(self, tab: str, rows: list[list[str]]) -> None:
        self.writes.append((tab, [list(row) for row in rows]))


class HistoryTests(SyncTestCase):
    def history(self) -> list[tuple[Any, ...]]:
        with self.registry._transaction() as db:
            return db.execute('SELECT property, from_value, to_value, actor FROM crm_edit_history ORDER BY id').fetchall()

    def lead(self, status: str = 'Leads') -> tuple[str, str]:
        page = self.page()
        project = self.new('SHOW', page=page)
        from t6.ids import EntityKind, T6Id
        person = str(T6Id.mint(EntityKind.PERSON))
        with self.registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES (?, 'staff', '[]', '[]', NULL, 0, 1)", (person,))
        self.registry.bind_entity('notion_user', 'user-1', T6Id.parse(person, EntityKind.PERSON), actor='setup')
        NotionEdits(self.registry).project_status(page, status, 'Ada', 'e0', 'user-1')
        return page, str(project['id'])

    def test_a_relation_edit_is_one_row_with_names_and_a_replay_adds_none(self) -> None:
        page, _ = self.lead()
        edits = NotionEdits(self.registry)
        self.assertEqual(edits.crm_edit(page, 'Lead Owner', ['Bea'], 'Ada', 'e1')['outcome'], 'recorded')
        self.assertEqual(edits.crm_edit(page, 'Lead Owner', ['Bea'], 'Ada', 'e1')['outcome'], 'unchanged')
        with self.registry._transaction() as db:
            db.execute("DELETE FROM notion_last_seen WHERE property = 'crm_Lead Owner'")
        edits.crm_edit(page, 'Lead Owner', ['Bea'], 'Ada', 'e1')  # last-seen lost: the unique key still holds
        self.assertEqual(self.history(), [('Status', '', 'Leads', 'Ada'), ('Lead Owner', '', 'Bea', 'Ada')])

    def test_a_second_edit_shows_the_previous_names_as_from(self) -> None:
        page, _ = self.lead('Negotiations')
        edits = NotionEdits(self.registry)
        edits.crm_edit(page, 'Studio', ['Acme'], 'Ada', 'e1')
        edits.crm_edit(page, 'Studio', ['Acme', 'Beta'], 'Ada', 'e2')
        self.assertEqual(self.history()[-1], ('Studio', 'Acme', 'Acme, Beta', 'Ada'))

    def test_a_project_that_is_not_a_lead_records_nothing(self) -> None:
        page = self.page()
        self.new('SHOW', page=page)
        self.assertEqual(NotionEdits(self.registry).crm_edit(page, 'Studio', ['Acme'], 'Ada', 'e1')['outcome'], 'not_a_lead')
        page, _ = self.lead()
        edits = NotionEdits(self.registry)
        edits.project_status(page, '🚨 In-Progress', 'Ada', 'e2', 'user-1')  # the CRM part ends
        self.assertEqual(edits.crm_edit(page, 'Studio', ['Acme'], 'Ada', 'e3')['outcome'], 'not_a_lead')
        self.assertEqual([row[0] for row in self.history()], ['Status', 'Status'])

    def test_a_status_edit_to_a_non_crm_status_is_a_history_row_from_the_current_stage(self) -> None:
        page, _ = self.lead('Negotiations')
        edits = NotionEdits(self.registry)
        edits.project_status(page, 'Standby', 'Ada', 'e2', 'user-1')
        edits.project_status(page, 'Standby', 'Ada', 'e2', 'user-1')  # a replay adds nothing
        self.assertEqual(self.history(), [('Status', '', 'Negotiations', 'Ada'), ('Status', 'Negotiations', 'Standby', 'Ada')])
        other = self.page()
        self.new('OTHER', page=other)  # never a Lead: no row
        edits.project_status(other, 'Standby', 'Ada', 'e3', 'user-1')
        self.assertEqual(len(self.history()), 2)

    def test_registration_records_the_relation_names_so_the_first_edit_has_a_from(self) -> None:
        page = '00000000000040008000000000000099'
        edits = NotionEdits(self.registry)
        edits.register_created_page(page, 'e0', 'Leads', None, 'main-worker', {'Lead Owner': ['Bea'], 'Studio': ['Acme']})
        edits.crm_edit(page, 'Lead Owner', ['Bea', 'Cy'], 'Ada', 'e1')
        self.assertEqual(self.history(), [('Lead Owner', 'Bea', 'Bea, Cy', 'Ada')])

    def test_a_stage_change_is_a_status_row_with_from_and_to(self) -> None:
        page, _ = self.lead()
        NotionEdits(self.registry).project_status(page, 'Onboarding', 'Ada', 'e1', 'user-1')
        self.assertEqual(self.history()[-1], ('Status', 'Leads', 'Onboarding', 'Ada'))

    def test_the_history_is_append_only(self) -> None:
        self.lead()
        with self.registry._transaction() as db:
            with self.assertRaises(sqlite3.DatabaseError):
                db.execute("UPDATE crm_edit_history SET to_value = 'x'")
            with self.assertRaises(sqlite3.DatabaseError):
                db.execute('DELETE FROM crm_edit_history')

    def test_a_notion_webhook_edit_shows_contact_names_and_a_replayed_event_adds_no_row(self) -> None:
        page, _ = self.lead()
        contact = str(self.notion.add_page(OTHER_SOURCE, {'Name': title('Bea Cruz')})['id'])
        self.client.update_page(page, {'properties': {'Lead Owner': {'relation': [{'id': contact}]}}})
        prop_id = self.notion.page(page)['properties']['Lead Owner']['id']
        event = {'id': 'hist-1', 'timestamp': datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(),
                 'type': 'page.properties_updated', 'entity': {'id': page, 'type': 'page'},
                 'authors': [{'id': 'user-1', 'name': 'Ada'}], 'data': {'updated_properties': [prop_id]}}
        for _ in range(2):
            self.assertEqual(self.app.public(notion_request(self.clock, event)).status, 200)
            self.drain()
        self.assertEqual(self.history()[-1], ('Lead Owner', '', 'Bea Cruz', 'Ada'))
        self.assertEqual(len(self.history()), 2)

    def test_another_property_is_ignored(self) -> None:
        page, _ = self.lead()
        self.client.update_page(page, {'properties': {'Name': {'title': [{'text': {'content': 'Renamed'}}]}}})
        prop_id = self.notion.page(page)['properties']['Name']['id']
        event = {'id': 'hist-2', 'timestamp': datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(),
                 'type': 'page.properties_updated', 'entity': {'id': page, 'type': 'page'},
                 'authors': [{'id': 'user-1', 'name': 'Ada'}], 'data': {'updated_properties': [prop_id]}}
        self.app.public(notion_request(self.clock, event))
        self.drain()
        self.assertEqual([row[0] for row in self.history()], ['Status'])


class SheetTests(unittest.TestCase):
    def setUp(self) -> None:
        self._folder = tempfile.TemporaryDirectory()
        self.addCleanup(self._folder.cleanup)
        self.registry = SqliteProjectRegistry(str(Path(self._folder.name, 'registry.db')))
        self.book = FakeBook()

    def register(self, number: int) -> tuple[str, str]:
        page = f'00000000000040008000{number:012d}'
        return page, str(self.registry.register(actor='setup', idempotency_key=f'k{number}', notion_page_id=page).project.id)  # type: ignore[union-attr]

    def edit(self, project: str, at: float, label: str, before: str, after: str, event: str) -> None:
        with self.registry._transaction() as db:
            db.execute('INSERT INTO crm_edit_history (project_id, at, property, from_value, to_value, actor, event) VALUES (?, ?, ?, ?, ?, ?, ?)',
                       (project, at, label, before, after, 'Ada', event))

    def test_the_tab_is_rewritten_newest_first_with_a_header_and_manila_dates(self) -> None:
        from t6.crm.sheet_sync import EditHistory
        page, project = self.register(1)
        self.edit(project, 1_700_000_000.0, 'Studio', '', 'Acme', 'a')  # 2023-11-14 22:13 UTC, 06:13 next day in Manila
        self.edit(project, 1_700_100_000.0, 'Status', 'Leads', 'Lost', 'b')
        EditHistory(self.registry, lambda p: ('Big Show', 'Leads'), self.book).handle(job('crm.history_sync'))
        self.assertEqual(self.book.writes, [('History', [
            ['Date', 'Project', 'What changed', 'From', 'To', 'By'],
            ['2023-11-16 10:00', 'Big Show', 'Status', 'Leads', 'Lost', 'Ada'],
            ['2023-11-15 06:13', 'Big Show', 'Studio', '', 'Acme', 'Ada']])])

    def test_an_unreadable_page_falls_back_to_the_project_id_and_no_edits_gives_only_the_header(self) -> None:
        from t6.crm.sheet_sync import EditHistory
        history = EditHistory(self.registry, lambda p: None, self.book)
        history.handle(job('crm.history_sync'))
        _, project = self.register(1)
        self.edit(project, 1_700_000_000.0, 'Studio', '', 'Acme', 'a')
        history.handle(job('crm.history_sync'))
        self.assertEqual(len(self.book.writes[0][1]), 1)
        self.assertEqual(self.book.writes[1][1][1][1], project)

    def test_the_writer_puts_rows_in_a_range_then_clears_below(self) -> None:
        from t6.crm.sheet_writer import GoogleSheetWriter
        http = FakeSheets()
        GoogleSheetWriter('synthetic-sheet', account(), http).replace_rows('History', [['Date', 'By'], ['d', "=A1"]])
        base = 'https://sheets.googleapis.com/v4/spreadsheets/synthetic-sheet/values/'
        self.assertEqual(http.calls, [
            ('PUT', base + "'History'!A1:B2?valueInputOption=RAW", {'majorDimension': 'ROWS', 'values': [['Date', 'By'], ['d', '=A1']]}),
            ('POST', base + "'History'!A3:B:clear", {})])

    def test_the_dropdown_uses_the_stage_table_for_open_leads(self) -> None:
        from t6.crm.sheet_sync import ProjectDropdown
        pages = {}
        ids = {}
        for number, name, status, stage in ((1, 'Moved Lost', 'Negotiations', 'Lost'), (2, 'Moved On', 'Leads', 'Onboarding'),
                                            (3, 'Plain Lead', 'Leads', None), (4, 'Ended', 'In-Progress', 'In-Progress')):
            page, project = self.register(number)
            pages[page], ids[name] = (name, status), project
            if stage:
                with self.registry._transaction() as db:
                    db.execute('INSERT INTO project_stage_history (project_id, at, actor, field, from_value, to_value, event) VALUES (?, 1, ?, ?, NULL, ?, ?)',
                               (project, 'x', 'crm_stage', stage, f'e{number}'))
        ProjectDropdown(self.registry, lambda p: pages.get(p), self.book).handle(job())
        self.assertEqual(self.book.writes, [('Projects', sorted(
            [f'{name} · {ids[name]}' for name in ('Moved On', 'Plain Lead', 'Ended')], key=str.casefold))])

    def test_a_project_past_the_crm_is_not_an_open_lead_whatever_a_stale_status_says(self) -> None:
        from t6.crm.sheet_sync import ProjectDropdown
        page, project = self.register(1)
        with self.registry._transaction() as db:
            db.execute("INSERT INTO project_stage_history (project_id, at, actor, field, from_value, to_value, event) VALUES (?, 1, 'x', 'crm_stage', 'Leads', 'In-Progress', 'e1')", (project,))
        ProjectDropdown(self.registry, lambda p: ('Stale', 'Leads'), self.book).handle(job())
        self.assertEqual(self.book.writes, [('Projects', [])])


class TriggerTests(unittest.TestCase):
    def test_a_status_or_lead_relation_edit_queues_a_history_rewrite_and_a_title_edit_does_not(self) -> None:
        from t6.crm.sheet_sync import SheetSyncTrigger
        with tempfile.TemporaryDirectory() as folder:
            registry = SqliteProjectRegistry(str(Path(folder, 'registry.db')))
            queue = InternalJobQueue(registry, clock=lambda: 1_000_000.0)
            handler = SheetSyncTrigger(queue, status_property=STATUS_PROPERTY, history_properties=[STATUS_PROPERTY, LEAD_OWNER],
                                       clock=lambda: 1_000_000.0).wrap(lambda j: {'outcome': 'ok'}, 'notion.page_changed')

            def queued() -> int:
                with registry._transaction() as db:
                    return int(db.execute("SELECT COUNT(*) FROM internal_jobs WHERE kind = 'crm.history_sync'").fetchone()[0])
            handler(job('notion.page_changed', {'updated_properties': ['title']}))
            self.assertEqual(queued(), 0)
            handler(job('notion.page_changed', {'updated_properties': [LEAD_OWNER]}))
            handler(job('notion.page_changed', {'updated_properties': [STATUS_PROPERTY]}))
            self.assertEqual(queued(), 1)


if __name__ == '__main__':
    unittest.main()
