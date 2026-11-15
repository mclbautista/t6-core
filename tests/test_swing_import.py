"""Swing Drives import from the Notion database: preview, apply, safe twice, by page ID and serial only."""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import patch

from t6.drives import Drive, Ref, Role
from t6.drives.testing import InMemoryDriveStore
from t6.ids import EntityKind, T6Id
from t6.notion.swing_drives import SwingDrivesProperties as P, read_swing_drives
from t6.notion.testing import FakeNotion, checkbox, date, relation, rich_text, title
from t6.swing_import import SWING_SOURCE, import_swing_drives

SHOW_PAGE = 'a' * 32
UNKNOWN_PAGE = 'b' * 32
OTHER_PAGE = 'c' * 32
SHOW = str(T6Id.mint(EntityKind.PROJECT))


class SwingImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeNotion()
        self.fake.add_snapshot_data_source('swing', 'swing_drives')
        self.fake.add_property('swing', P.SERIAL, 'rich_text')
        self.fake.add_property('swing', P.PROJECTS, 'relation')
        self.store = InMemoryDriveStore(project_bindings={SHOW_PAGE: SHOW})

    def row(self, name: str, serial: str | None, project: str | list[str] | None = SHOW_PAGE, in_use: bool = True, moved: str | None = '2026-10-01') -> str:
        properties: dict[str, Any] = {P.NAME: title(name), P.SERIAL: rich_text(serial), P.IN_USE: checkbox(in_use),
                                      P.LAST_MOVED: date(moved), P.PROJECTS: relation(*(project if isinstance(project, list) else [project] if project else []))}
        return str(self.fake.add_page('swing', properties)['id']).replace('-', '')

    def run_import(self, *, apply: bool = False, digest: str | None = None) -> dict[str, Any]:
        with self.fake.client() as client:
            rows = read_swing_drives(client, 'swing')
        return import_swing_drives(rows, self.store, apply=apply, digest=digest)

    def test_preview_writes_nothing_and_apply_binds_rows_once(self) -> None:
        existing = self.store.add_drive(Drive(serial='SYNTH-1', label='Invented drive'))
        first, second = self.row('Swing A', 'synth-1'), self.row('Swing B', 'SYNTH-2')
        preview = self.run_import()
        self.assertEqual({e['page_id']: e['action'] for e in preview['entries']}, {first: 'bind', second: 'create'})
        self.assertEqual(self.store.list_assignments(existing.id), ())
        self.assertIsNone(self.store.find_by_serial('SYNTH-2'))
        applied = self.run_import(apply=True, digest=preview['digest'])
        self.assertEqual(applied['changed'], 2)
        bound = self.store.list_assignments(existing.id)
        self.assertEqual([(a.label, a.role, a.project, str(a.start), a.end, a.return_date) for a in bound],
                         [('Swing A', Role.SWING, Ref(page_id=SHOW), '2026-10-01', None, None)])
        created = self.store.find_by_serial('SYNTH-2')
        assert created is not None
        self.assertFalse(created.ownership_known)
        self.assertEqual([s.sheet for s in self.store.sources(created.id)], [second])
        self.assertEqual(self.store.sources(created.id)[0].workbook, SWING_SOURCE)

    def test_second_apply_changes_nothing(self) -> None:
        self.row('Swing A', 'SYNTH-1')
        self.row('Swing B', None)
        preview = self.run_import()
        self.run_import(apply=True, digest=preview['digest'])
        drive = self.store.find_by_serial('SYNTH-1')
        assert drive is not None
        before = (self.store.list_assignments(drive.id), self.store.exceptions())
        again = self.run_import(apply=True, digest=preview['digest'])
        self.assertEqual(again['changed'], 0)
        self.assertEqual((self.store.list_assignments(drive.id), self.store.exceptions()), before)
        self.assertEqual([e['action'] for e in again['entries'] if e['serial'] == 'SYNTH-1'], ['already'])

    def test_apply_needs_the_digest_of_the_preview_and_refuses_changed_notion_data(self) -> None:
        self.row('Swing A', 'SYNTH-1')
        preview = self.run_import()
        with self.assertRaises(ValueError):
            self.run_import(apply=True)
        self.row('Swing B', 'SYNTH-2')
        with self.assertRaisesRegex(ValueError, 'changed since preview'):
            self.run_import(apply=True, digest=preview['digest'])
        self.assertIsNone(self.store.find_by_serial('SYNTH-1'))

    def test_a_row_is_never_matched_by_name_or_label(self) -> None:
        existing = self.store.add_drive(Drive(serial='SYNTH-1', label='Swing A'))
        self.row('Swing A', None)
        preview = self.run_import()
        self.run_import(apply=True, digest=preview['digest'])
        self.assertEqual(self.store.list_assignments(existing.id), ())
        self.assertEqual([i.reason for i in self.store.exceptions()], ['no usable serial'])

    def test_unplaceable_rows_land_in_the_exception_queue_with_the_page_id(self) -> None:
        no_serial = self.row('No serial', None)
        noted = self.row('Noted', 'SYNTH-3 (maybe)')
        one, two = self.row('Dup one', 'SYNTH-4'), self.row('Dup two', 'synth-4')
        no_project = self.row('No project', 'SYNTH-5', project=None)
        unregistered = self.row('Unregistered', 'SYNTH-6', project=UNKNOWN_PAGE)
        several = self.row('Two projects', 'SYNTH-7', project=[SHOW_PAGE, OTHER_PAGE])
        suspect = self.store.add_drive(Drive(suspected_serial='SYNTH-8', label='Maybe'))
        suspected = self.row('Suspected', 'SYNTH-8')
        preview = self.run_import()
        self.assertEqual({e['page_id']: e['action'] for e in preview['entries'] if e['action'] == 'exception'},
                         {p: 'exception' for p in (no_serial, noted, one, two, no_project, unregistered, several, suspected)})
        self.assertEqual(self.store.exceptions(), ())
        self.run_import(apply=True, digest=preview['digest'])
        queue = {item.sheet: item for item in self.store.exceptions()}
        self.assertEqual(set(queue), {no_serial, noted, one, two, no_project, unregistered, several, suspected})
        self.assertTrue(all(item.workbook == SWING_SOURCE for item in queue.values()))
        self.assertIn('Project', queue[unregistered].reason)
        self.assertIn('ambiguous', queue[one].reason)
        self.assertIsNone(self.store.find_by_serial('SYNTH-4'))
        self.assertEqual(self.store.list_assignments(suspect.id), ())

    def test_a_row_not_in_use_is_reported_and_skipped(self) -> None:
        page = self.row('Idle', 'SYNTH-9', in_use=False)
        preview = self.run_import()
        self.assertEqual([(e['page_id'], e['action']) for e in preview['entries']], [(page, 'skip')])
        self.run_import(apply=True, digest=preview['digest'])
        self.assertIsNone(self.store.find_by_serial('SYNTH-9'))
        self.assertEqual(self.store.exceptions(), ())

    def test_a_swing_already_assigned_to_the_project_is_not_assigned_twice(self) -> None:
        from datetime import date as day
        from t6.drives import Assignment
        drive = self.store.add_drive(Drive(serial='SYNTH-1'))
        self.store.add_assignment(drive.id, Assignment(label='old', project=Ref(page_id=SHOW), role=Role.SWING, start=day(2026, 9, 1)))
        self.row('Swing A', 'SYNTH-1')
        preview = self.run_import()
        self.assertEqual([e['action'] for e in preview['entries']], ['already'])
        self.run_import(apply=True, digest=preview['digest'])
        self.assertEqual(len(self.store.list_assignments(drive.id)), 1)

    def test_cli_preview_apply_twice(self) -> None:
        from t6.cli import main
        from t6.registry import SqliteProjectRegistry
        self.row('Swing A', 'SYNTH-1')
        self.row('Swing B', None)
        with tempfile.TemporaryDirectory() as tmp:
            sources = Path(tmp) / 'sources.json'
            sources.write_text(json.dumps({'swing_drives': 'swing'}))
            database = Path(tmp) / 'registry.sqlite'
            registry = SqliteProjectRegistry(database, new_id=lambda: SHOW)
            registry.register(actor='setup', idempotency_key='show', notion_page_id=SHOW_PAGE)

            def run(*args: str) -> Any:
                out = io.StringIO()
                with patch.dict(os.environ, {'T6_NOTION_TOKEN': 'fake'}, clear=True), redirect_stdout(out), redirect_stderr(io.StringIO()):
                    self.assertEqual(main(['admin', 'drive', 'import', '--swing', '--data-sources', str(sources),
                                           '--registry-db', str(database), *args], open_notion=lambda _: self.fake.client()), 0)
                return json.loads(out.getvalue())
            preview = run()
            self.assertEqual(preview['changed'], 0)
            path = Path(tmp) / 'preview.json'
            path.write_text(json.dumps(preview))
            first = run('--apply', '--preview', str(path))
            second = run('--apply', '--preview', str(path))
            self.assertGreater(first['changed'], 0)
            self.assertEqual(second['changed'], 0)
            with patch.dict(os.environ, {'T6_NOTION_TOKEN': 'fake'}, clear=True), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(main(['admin', 'drive', 'import', '--swing', '--data-sources', str(sources), '--registry-db', str(database), '--apply'],
                                      open_notion=lambda _: self.fake.client()), 2)


if __name__ == '__main__':
    unittest.main()
