"""Swing Drives import from the Notion database: every row is one Drive keyed by its page ID; preview, apply, safe twice."""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import patch

from t6.drives import Drive, Ownership, Role
from t6.drives.testing import InMemoryDriveStore
from t6.ids import EntityKind, T6Id
from t6.notion.swing_drives import SwingDrivesProperties as P, read_swing_drives
from t6.notion.testing import FakeNotion, checkbox, date, multi_select, relation, rich_text, select, title
from t6.swing_import import SWING_SOURCE, _row_key, import_swing_drives

CONTACT_A = 'a' * 32
CONTACT_B = 'b' * 32
STRANGER = 'c' * 32
PERSON_A = str(T6Id.mint(EntityKind.PERSON))
PERSON_B = str(T6Id.mint(EntityKind.PERSON))


class SwingImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeNotion()
        self.fake.add_snapshot_data_source('swing', 'swing_drives')
        self.store = InMemoryDriveStore(person_bindings={CONTACT_A: PERSON_A, CONTACT_B: PERSON_B})

    def row(self, name: str | None, *, collaborators: list[str] | None = None, in_use: bool = True, moved: str | None = '2026-10-01',
            fmt: str | None = None, hd: tuple[str, ...] = (), notes: str | None = None) -> str:
        properties: dict[str, Any] = {P.NAME: title(name or ''), P.IN_USE: checkbox(in_use), P.LAST_MOVED: date(moved),
                                      P.COLLABORATOR: relation(*(collaborators or [])), P.FORMAT: select(fmt),
                                      P.HD_TYPE: multi_select(*hd), P.SHORT_NOTES: rich_text(notes)}
        return str(self.fake.add_page('swing', properties)['id']).replace('-', '')

    def run_import(self, *, apply: bool = False, digest: str | None = None) -> dict[str, Any]:
        with self.fake.client() as client:
            rows = read_swing_drives(client, 'swing')
        return import_swing_drives(rows, self.store, apply=apply, digest=digest)

    def drive_of(self, page: str) -> Drive:
        (found,) = [d for d in (self.store.get_drive(i) for i in self.store._drives) if d and any(s.sheet == page for s in self.store.sources(d.id))]
        return found

    def test_the_reader_reads_the_real_properties_by_name(self) -> None:
        page = self.row('SYNTH01', collaborators=[CONTACT_A], fmt='exFAT', hd=('SSD', 'Portable'), notes='invented note')
        with self.fake.client() as client:
            (read,) = read_swing_drives(client, 'swing')
        self.assertEqual(read['page_id'].replace('-', ''), page)
        self.assertEqual(read, {'page_id': read['page_id'], 'name': 'SYNTH01', 'in_use': True, 'last_moved': '2026-10-01', 'collaborators': [CONTACT_A],
                                'format': 'exFAT', 'hd_types': ['SSD', 'Portable'], 'notes': 'invented note'})

    def test_preview_writes_nothing_and_apply_creates_one_drive_per_row_by_page_id(self) -> None:
        in_use = self.row('SYNTH01', collaborators=[CONTACT_A], fmt='exFAT', hd=('SSD', 'Portable'), notes='invented note')
        idle = self.row('SYNTH02', in_use=False)
        preview = self.run_import()
        self.assertEqual({e['page_id']: e['action'] for e in preview['entries']}, {in_use: 'create', idle: 'create'})
        self.assertEqual(self.store._drives, {})
        applied = self.run_import(apply=True, digest=preview['digest'])
        self.assertEqual(applied['changed'], 2)
        first, second = self.drive_of(in_use), self.drive_of(idle)
        self.assertNotEqual(first.id, second.id)
        for drive in (first, second):
            self.assertIsNone(drive.serial)
            self.assertIs(drive.ownership, Ownership.UNKNOWN)
            self.assertEqual(self.store.sources(drive.id)[0].workbook, SWING_SOURCE)
        self.assertEqual((first.label, first.format, first.description, first.remarks), ('SYNTH01', 'exFAT', 'SSD, Portable', 'invented note'))
        self.assertEqual(self.store.list_assignments(second.id), ())  # not in use: a Drive with no Assignment

    def test_one_registered_collaborator_holds_an_open_swing_with_no_project(self) -> None:
        page = self.row('SYNTH01', collaborators=[CONTACT_B], moved='2026-10-03')
        preview = self.run_import()
        self.run_import(apply=True, digest=preview['digest'])
        (held,) = self.store.list_assignments(self.drive_of(page).id)
        self.assertEqual((held.label, held.role, held.project, held.holder, str(held.start), held.end), ('SYNTH01', Role.SWING, None, PERSON_B, '2026-10-03', None))
        self.assertEqual(self.store.exceptions(), ())

    def test_holder_questions_go_to_the_exception_queue_and_the_drive_is_still_created(self) -> None:
        nobody = self.row('SYNTH03', collaborators=[])
        several = self.row('SYNTH04', collaborators=[CONTACT_A, CONTACT_B])
        stranger = self.row('SYNTH05', collaborators=[STRANGER])
        undated = self.row('SYNTH06', collaborators=[CONTACT_A], moved=None)
        preview = self.run_import()
        self.run_import(apply=True, digest=preview['digest'])
        queue = {item.sheet: item for item in self.store.exceptions()}
        self.assertEqual(set(queue), {nobody, several, stranger, undated})
        self.assertTrue(all(item.workbook == SWING_SOURCE for item in queue.values()))
        self.assertIn('no Collaborator', queue[nobody].reason)
        self.assertIn('several Collaborators', queue[several].reason)
        self.assertIn('not a registered Person', queue[stranger].reason)
        self.assertIn('Last Moved', queue[undated].reason)
        for page in queue:
            self.assertEqual(self.store.list_assignments(self.drive_of(page).id), ())

    def test_a_row_is_never_matched_by_name_or_label(self) -> None:
        existing = self.store.add_drive(Drive(serial='SYNTH-1', label='SYNTH08'))
        page = self.row('SYNTH08', collaborators=[CONTACT_A])
        preview = self.run_import()
        self.run_import(apply=True, digest=preview['digest'])
        created = self.drive_of(page)
        self.assertNotEqual(created.id, existing.id)
        self.assertEqual(self.store.list_assignments(existing.id), ())
        self.assertEqual(len(self.store.list_assignments(created.id)), 1)

    def test_second_apply_changes_nothing(self) -> None:
        self.row('SYNTH01', collaborators=[CONTACT_A])
        self.row('SYNTH02', in_use=True, collaborators=[])
        self.row('SYNTH03', in_use=False)
        preview = self.run_import()
        self.run_import(apply=True, digest=preview['digest'])
        before = (dict(self.store._drives), {k: list(v) for k, v in self.store._assignments.items()}, self.store.exceptions())
        again = self.run_import(apply=True, digest=preview['digest'])
        self.assertEqual(again['changed'], 0)
        self.assertEqual(before, (dict(self.store._drives), {k: list(v) for k, v in self.store._assignments.items()}, self.store.exceptions()))
        self.assertEqual({e['action'] for e in again['entries']}, {'already'})

    def test_apply_needs_the_digest_of_the_preview_and_refuses_changed_notion_data(self) -> None:
        self.row('SYNTH01')
        preview = self.run_import()
        with self.assertRaises(ValueError):
            self.run_import(apply=True)
        self.row('SYNTH02')
        with self.assertRaisesRegex(ValueError, 'changed since preview'):
            self.run_import(apply=True, digest=preview['digest'])
        self.assertEqual(self.store._drives, {})

    def test_a_row_with_no_name_has_nothing_to_identify_the_drive_and_is_queued(self) -> None:
        page = self.row(None, collaborators=[CONTACT_A])
        preview = self.run_import()
        self.assertEqual([e['action'] for e in preview['entries']], ['exception'])
        self.run_import(apply=True, digest=preview['digest'])
        self.assertEqual(self.store._drives, {})
        self.assertEqual([(i.sheet, i.reason) for i in self.store.exceptions()], [(page, 'no Name to label the Drive')])

    def test_cli_preview_apply_twice_on_the_t6_database(self) -> None:
        from t6.cli import main
        from t6.registry import SqliteProjectRegistry
        from t6.drives.sqlite import SqliteDriveStore
        held = self.row('SYNTH01', collaborators=[CONTACT_A], hd=('SSD',))
        self.row('SYNTH02', in_use=False)
        with tempfile.TemporaryDirectory() as tmp:
            sources = Path(tmp) / 'sources.json'
            sources.write_text(json.dumps({'swing_drives': 'swing'}))
            database = Path(tmp) / 'registry.sqlite'
            registry = SqliteProjectRegistry(database)
            with registry._transaction() as db:
                db.execute("INSERT INTO persons VALUES (?, 'staff', '[]', '[]', NULL, 0, 1)", (PERSON_A,))
            registry.bind_entity('notion', CONTACT_A, T6Id.parse(PERSON_A, EntityKind.PERSON), actor='setup')

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
            self.assertEqual((first['changed'], second['changed']), (2, 0))
            store = SqliteDriveStore(registry)
            drive_id = store.imported_drive(_row_key(held))
            assert drive_id is not None
            (assignment,) = store.list_assignments(drive_id)
            self.assertEqual((assignment.holder, assignment.project, assignment.role), (PERSON_A, None, Role.SWING))
            with patch.dict(os.environ, {'T6_NOTION_TOKEN': 'fake'}, clear=True), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(main(['admin', 'drive', 'import', '--swing', '--data-sources', str(sources), '--registry-db', str(database), '--apply'],
                                      open_notion=lambda _: self.fake.client()), 2)


if __name__ == '__main__':
    unittest.main()
