"""Drive persistence through the shared DriveStore interface."""
import unittest

from t6.drives import Drive
from t6.drives.sqlite import SqliteDriveStore
from test_person import MemoryRegistry


class DriveDatabaseTests(unittest.TestCase):
    def test_drive_survives_store_reconstruction(self) -> None:
        registry = MemoryRegistry()
        self.addCleanup(registry.db.close)
        drive = Drive(serial='SYNTHETIC-ONE')
        SqliteDriveStore(registry).add_drive(drive)
        self.assertEqual(SqliteDriveStore(registry).find_by_serial('synthetic-one'), drive)

    def test_store_contract(self) -> None:
        from unittest.mock import patch
        import test_drives
        import test_drive_identity
        registries: list[MemoryRegistry] = []
        def make_store() -> SqliteDriveStore:
            registry = MemoryRegistry()
            registries.append(registry)
            with registry._transaction() as db:
                for identifier in ('t6p_aaaaaaaaaaaaaaaaaaaaaaaa', 't6p_bbbbbbbbbbbbbbbbbbbbbbbb'):
                    db.execute("INSERT INTO projects VALUES (?, 1, NULL, NULL, 'out', 0)", (identifier,))
            return SqliteDriveStore(registry)
        suite = unittest.TestSuite([unittest.TestLoader().loadTestsFromModule(test_drives),
                                    unittest.TestLoader().loadTestsFromModule(test_drive_identity)])
        result = unittest.TestResult()
        try:
            with patch.object(test_drives, 'make_store', make_store), patch.object(test_drive_identity, 'InMemoryDriveStore', make_store):
                suite.run(result)
        finally:
            for registry in registries:
                registry.db.close()
        self.assertEqual(result.errors, [])
        self.assertEqual(result.failures, [])

    def test_unreadable_row_is_durable_and_idempotent(self) -> None:
        import tempfile
        from t6.drives.importer import import_per_project_workbook
        from test_drives_importer import write_workbook, make_lookup, LOGGED_BY
        registry = MemoryRegistry()
        self.addCleanup(registry.db.close)
        with tempfile.TemporaryDirectory() as folder:
            path = write_workbook(folder, {'Unresolved': [['HARD DRIVE LABEL', 'SERIAL NUMBER'], [None, '(unreadable)']]})
            store = SqliteDriveStore(registry)
            for _ in range(2):
                import_per_project_workbook(path, store, lookup=make_lookup(), logged_by=LOGGED_BY)
            items = SqliteDriveStore(registry).exceptions()
            unreadable = [item for item in items if item.reason == 'unreadable']
            self.assertEqual(len(unreadable), 1)
            self.assertEqual((unreadable[0].workbook, unreadable[0].sheet, unreadable[0].row), (str(path.resolve()), 'Unresolved', 2))
            self.assertIn('(unreadable)', unreadable[0].detail)

    def test_import_resolves_assignment_to_t6_project(self) -> None:
        import tempfile
        from t6.drives import Ref
        from t6.drives.importer import import_per_project_workbook
        from t6.registry import Registered
        from test_drives_importer import write_workbook, make_lookup, LOGGED_BY
        registry = MemoryRegistry()
        self.addCleanup(registry.db.close)
        registry._new_id = lambda: 't6p_0123456789abcdef01234567'
        project = registry.register(actor='test', idempotency_key='project', notion_page_id='project-paper-moon')
        assert isinstance(project, Registered)
        with tempfile.TemporaryDirectory() as folder:
            path = write_workbook(folder, {'PAPER MOON': [['HARD DRIVE LABEL', 'SERIAL NUMBER'], ['synthetic', 'SYNTH-1']]})
            store = SqliteDriveStore(registry)
            import_per_project_workbook(path, store, lookup=make_lookup(), logged_by=LOGGED_BY)
            drive = store.find_by_serial('SYNTH-1')
            assert drive is not None
            self.assertEqual(store.assignment_history(drive.id)[0].project, Ref(page_id=project.project.id))

    def test_unregistered_project_cannot_be_written_as_assignment(self) -> None:
        from t6.drives import Assignment, Ref
        registry = MemoryRegistry()
        self.addCleanup(registry.db.close)
        store = SqliteDriveStore(registry)
        drive = store.add_drive(Drive(serial='SYNTH-1'))
        with self.assertRaises(ValueError):
            store.add_assignment(drive.id, Assignment(label='x', project=Ref(page_id='not-a-project'), role=None, start=None))

    def test_import_retains_source_and_is_unchanged_on_rerun(self) -> None:
        import tempfile
        from dataclasses import asdict
        from t6.drives import Ref
        from t6.drives.importer import import_per_project_workbook
        from test_drives_importer import write_workbook, make_lookup, LOGGED_BY
        registry = MemoryRegistry()
        self.addCleanup(registry.db.close)
        with tempfile.TemporaryDirectory() as folder:
            path = write_workbook(folder, {'Unknown': [
                ['HARD DRIVE LABEL', 'SERIAL NUMBER', 'HD DESCRIPTION'],
                ['one', None, 'synthetic case'],
                ['two', 'SYNTH-SHARED', 'synthetic case'],
                ['three', 'SYNTH-SHARED', 'synthetic case'],
                [None, '(unreadable)', None],
            ]})
            store = SqliteDriveStore(registry)
            def snapshot() -> tuple[list[tuple[object, object, object, object]], object]:
                pairs = store.open_assignments(Ref(text='Unknown'))
                return ([(asdict(drive), asdict(assignment), store.list_events(drive.id), store.sources(drive.id)) for drive, assignment in pairs], store.exceptions())
            import_per_project_workbook(path, store, lookup=make_lookup(), logged_by=LOGGED_BY)
            first = snapshot()
            self.assertEqual(len(first[0]), 3)
            self.assertIsNone(store.find_by_serial('SYNTH-SHARED'))
            for drive, _ in store.open_assignments(Ref(text="Unknown")):
                self.assertEqual(store.sources(drive.id)[0].workbook, str(path.resolve()))
            import_per_project_workbook(path, store, lookup=make_lookup(), logged_by=LOGGED_BY)
            self.assertEqual(snapshot(), first)

    def test_unbound_lookup_project_remains_text_and_is_reported(self) -> None:
        import tempfile
        from t6.drives import Ref
        from t6.drives.importer import import_per_project_workbook
        from test_drives_importer import write_workbook, make_lookup, LOGGED_BY
        registry = MemoryRegistry()
        self.addCleanup(registry.db.close)
        with tempfile.TemporaryDirectory() as folder:
            path = write_workbook(folder, {'PAPER MOON': [['HARD DRIVE LABEL', 'SERIAL NUMBER'], ['invented', 'SYNTH-2']]})
            store = SqliteDriveStore(registry)
            result = import_per_project_workbook(path, store, lookup=make_lookup(), logged_by=LOGGED_BY)
            drive = store.find_by_serial('SYNTH-2')
            assert drive is not None
            self.assertEqual(store.assignment_history(drive.id)[0].project, Ref(text='PAPER MOON'))
            self.assertTrue(any(item.reason == 'unresolved project' for item in result.review))
            self.assertTrue(any(item.reason == 'unresolved project' for item in store.exceptions()))

    def test_exception_preserves_all_source_cells(self) -> None:
        import tempfile
        from t6.drives.importer import import_per_project_workbook
        from test_drives_importer import write_workbook, make_lookup, LOGGED_BY
        registry = MemoryRegistry()
        self.addCleanup(registry.db.close)
        with tempfile.TemporaryDirectory() as folder:
            path = write_workbook(folder, {'Unknown': [
                ['HARD DRIVE LABEL', 'SERIAL NUMBER', 'HD DESCRIPTION'],
                ['synthetic label', 'SYNTH-3 [unclear]', 'synthetic detail'],
            ]})
            store = SqliteDriveStore(registry)
            import_per_project_workbook(path, store, lookup=make_lookup(), logged_by=LOGGED_BY)
            item = next(item for item in store.exceptions() if item.reason == 'note in serial cell')
            self.assertIn('synthetic detail', item.detail)
            self.assertIn('SYNTH-3 [unclear]', item.detail)

    def test_log_import_and_exceptions_survive_database_reopen(self) -> None:
        import tempfile
        from pathlib import Path
        from t6.drives import Ref
        from t6.drives.importer import import_log_sheet_workbook
        from t6.registry.sqlite import SqliteProjectRegistry
        from test_drives_importer import write_workbook, make_lookup, LOGGED_BY
        with tempfile.TemporaryDirectory() as folder:
            path = write_workbook(folder, {'Movements': [
                ['HARD DRIVE LABEL', 'IN/OUT', 'PROJECT NAME', 'REMARKS'],
                ['synthetic one', 'IN', 'Unknown', 'S/N: SYNTH-4'],
                ['synthetic two', 'OUT', 'Unknown', 'unreadable serial'],
            ]})
            db_path = Path(folder) / 'test.sqlite'
            store = SqliteDriveStore(SqliteProjectRegistry(db_path))
            import_log_sheet_workbook(path, store, lookup=make_lookup(), logged_by=LOGGED_BY)
            drive = store.find_by_serial('SYNTH-4')
            assert drive is not None
            first = (store.assignment_history(drive.id), store.list_events(drive.id), store.sources(drive.id), store.exceptions())
            reopened = SqliteDriveStore(SqliteProjectRegistry(db_path))
            self.assertEqual(reopened.assignment_history(drive.id)[0].project, Ref(text='Unknown'))
            self.assertTrue(any(item.reason == 'movement with no serial' and item.row == 3 for item in reopened.exceptions()))
            import_log_sheet_workbook(path, reopened, lookup=make_lookup(), logged_by=LOGGED_BY)
            self.assertEqual((reopened.assignment_history(drive.id), reopened.list_events(drive.id), reopened.sources(drive.id), reopened.exceptions()), first)

    def test_unrecognized_log_rows_are_preserved(self) -> None:
        import tempfile
        from t6.drives.importer import import_log_sheet_workbook
        from test_drives_importer import write_workbook, make_lookup, LOGGED_BY
        registry = MemoryRegistry()
        self.addCleanup(registry.db.close)
        with tempfile.TemporaryDirectory() as folder:
            path = write_workbook(folder, {'Unknown': [['UNRECOGNIZED', 'COLUMN'], ['synthetic value', 'synthetic serial']]})
            store = SqliteDriveStore(registry)
            import_log_sheet_workbook(path, store, lookup=make_lookup(), logged_by=LOGGED_BY)
            self.assertTrue(any(item.row == 2 and 'synthetic serial' in item.detail for item in store.exceptions()))

    def test_exception_preserves_duplicate_source_columns(self) -> None:
        import tempfile
        from t6.drives.importer import import_per_project_workbook
        from test_drives_importer import write_workbook, make_lookup, LOGGED_BY
        registry = MemoryRegistry()
        self.addCleanup(registry.db.close)
        with tempfile.TemporaryDirectory() as folder:
            path = write_workbook(folder, {'Unknown': [
                ['HARD DRIVE LABEL', 'SERIAL NUMBER', 'HD DESCRIPTION', 'HD DESCRIPTION'],
                ['synthetic', 'SYNTH-5 [unclear]', 'first detail', 'second detail'],
            ]})
            store = SqliteDriveStore(registry)
            import_per_project_workbook(path, store, lookup=make_lookup(), logged_by=LOGGED_BY)
            item = next(item for item in store.exceptions() if item.reason == 'note in serial cell')
            self.assertIn('first detail', item.detail)
            self.assertIn('second detail', item.detail)

    def test_receipt_prevents_new_exceptions_after_serial_change(self) -> None:
        import tempfile
        from t6.drives.importer import import_per_project_workbook
        from test_drives_importer import write_workbook, make_lookup, LOGGED_BY
        registry = MemoryRegistry()
        self.addCleanup(registry.db.close)
        with tempfile.TemporaryDirectory() as folder:
            path = write_workbook(folder, {'Unknown': [['HARD DRIVE LABEL', 'SERIAL NUMBER'], ['original', 'SYNTH-6']]})
            store = SqliteDriveStore(registry)
            import_per_project_workbook(path, store, lookup=make_lookup(), logged_by=LOGGED_BY)
            drive = store.find_by_serial('SYNTH-6')
            assert drive is not None
            store.set_serial(drive.id, 'SYNTH-7')
            store.add_drive(Drive(serial='SYNTH-6', label='different'))
            before = store.exceptions()
            import_per_project_workbook(path, store, lookup=make_lookup(), logged_by=LOGGED_BY)
            self.assertEqual(store.exceptions(), before)

    def test_project_queries_follow_registry_binding(self) -> None:
        from t6.drives import Assignment, Ref
        from t6.registry import Registered
        registry = MemoryRegistry()
        self.addCleanup(registry.db.close)
        registry._new_id = lambda: 't6p_0123456789abcdef01234567'
        project = registry.register(actor='test', idempotency_key='project', notion_page_id='invented-page')
        assert isinstance(project, Registered)
        store = SqliteDriveStore(registry)
        drive = store.add_drive(Drive(serial='SYNTH-8'))
        store.add_assignment(drive.id, Assignment(label='synthetic', project=Ref(page_id=project.project.id), role=None, start=None))
        self.assertEqual(store.resolve_project(Ref(page_id=project.project.id), 'fallback'), Ref(page_id=project.project.id))
        self.assertEqual([d.id for d, _ in store.open_assignments(Ref(page_id='invented-page'))], [drive.id])

    def test_fake_project_bindings_preserve_ids_and_unresolved_text(self) -> None:
        from t6.drives import Ref
        from t6.drives.testing import InMemoryDriveStore
        store = InMemoryDriveStore(project_bindings={'invented-page': 't6p_0123456789abcdef01234567'})
        self.assertEqual(store.resolve_project(Ref(page_id='invented-page'), 'invented'), Ref(page_id='t6p_0123456789abcdef01234567'))
        self.assertEqual(store.resolve_project(Ref(page_id='unknown'), 'invented'), Ref(text='invented'))

    def test_fake_rejects_source_ids_in_assignments(self) -> None:
        from t6.drives import Assignment, Ref
        from t6.drives.testing import InMemoryDriveStore
        store = InMemoryDriveStore()
        drive = store.add_drive(Drive(serial='SYNTH-9'))
        with self.assertRaises(ValueError):
            store.add_assignment(drive.id, Assignment(label='synthetic', project=Ref(page_id='invented-page'), role=None, start=None))

    def test_fake_queries_follow_project_bindings(self) -> None:
        from t6.drives import Assignment, Ref
        from t6.drives.testing import InMemoryDriveStore
        project = 't6p_0123456789abcdef01234567'
        store = InMemoryDriveStore(project_bindings={'invented-page': project})
        drive = store.add_drive(Drive(serial='SYNTH-10'))
        store.add_assignment(drive.id, Assignment(label='synthetic', project=Ref(page_id=project), role=None, start=None))
        self.assertEqual([d.id for d, _ in store.open_assignments(Ref(page_id='invented-page'))], [drive.id])

    def test_receipt_prevents_new_exception_after_project_binding_changes(self) -> None:
        import tempfile
        from t6.drives.importer import import_per_project_workbook
        from t6.registry import Registered
        from test_drives_importer import write_workbook, make_lookup, LOGGED_BY
        registry = MemoryRegistry()
        self.addCleanup(registry.db.close)
        registry._new_id = lambda: 't6p_0123456789abcdef01234567'
        project = registry.register(actor='test', idempotency_key='project', notion_page_id='project-paper-moon')
        assert isinstance(project, Registered)
        with tempfile.TemporaryDirectory() as folder:
            path = write_workbook(folder, {'PAPER MOON': [['HARD DRIVE LABEL', 'SERIAL NUMBER'], ['synthetic', 'SYNTH-11']]})
            store = SqliteDriveStore(registry)
            import_per_project_workbook(path, store, lookup=make_lookup(), logged_by=LOGGED_BY)
            before = store.exceptions()
            with registry._transaction() as db:
                db.execute("DELETE FROM bindings WHERE page_id='project-paper-moon'")
            import_per_project_workbook(path, store, lookup=make_lookup(), logged_by=LOGGED_BY)
            self.assertEqual(store.exceptions(), before)
