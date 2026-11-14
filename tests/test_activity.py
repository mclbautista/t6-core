"""Project activity and its audit record are committed together."""
import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from cli_api_fake import client_patch
from t6.cli import main
from t6.registry import Registered, Rotation, SqliteProjectRegistry


class ActivityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'registry.sqlite'
        self.now = 1000.0
        patcher = client_patch(lambda _token: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.registry = SqliteProjectRegistry(self.path, lambda: self.now)
        with self.registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES ('test-actor', 'staff', '[]', '[]', NULL, 0, 1)")
            db.execute("INSERT INTO person_permissions VALUES ('test-actor', 'operator')")
        result = self.registry.register(actor='person', idempotency_key='register', code='ABC')
        assert isinstance(result, Registered)
        self.project = result.project

    def test_changes_are_paired_ordered_filtered_and_replayed_once(self) -> None:
        self.now = 2000
        self.registry.set_rotation(self.project.id, Rotation.IN, actor='service', idempotency_key='in', expected_revision=1)
        self.registry.set_rotation(self.project.id, Rotation.IN, actor='service', idempotency_key='in', expected_revision=1)
        self.now = 3000
        self.registry.bind_page(self.project.id, 'page', actor='person', idempotency_key='bind', expected_revision=2)
        entries = self.registry.activity(self.project.id)
        self.assertEqual([e.action for e in entries], ['page_bound', 'rotation_changed', 'registered'])
        self.assertEqual(len(self.registry.audit(self.project.id)), len(entries))
        self.assertEqual([e.action for e in self.registry.activity(self.project.id, since=2000, limit=1)], ['page_bound'])
        self.assertEqual(len({e.audit_id for e in entries}), 3)

    def test_both_tables_reject_updates_and_deletes(self) -> None:
        with contextlib.closing(sqlite3.connect(self.path)) as db:
            for table in ('activity', 'audit'):
                for sql in (f'DELETE FROM {table}', f'UPDATE {table} SET actor = "changed"'):
                    with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                        db.execute(sql)

    def test_activity_failure_rolls_back_registry_and_audit(self) -> None:
        with contextlib.closing(sqlite3.connect(self.path)) as db:
            db.execute("CREATE TRIGGER fail_activity BEFORE INSERT ON activity BEGIN SELECT RAISE(ABORT, 'fail'); END")
            db.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.registry.set_code(self.project.id, 'NEW', actor='person', idempotency_key='code', expected_revision=1)
        project = self.registry.get(self.project.id)
        assert project is not None
        self.assertEqual(project.code, 'ABC')
        self.assertEqual(len(self.registry.audit(self.project.id)), 1)
        self.assertEqual(len(self.registry.activity(self.project.id)), 1)

    def test_cli_json_and_text(self) -> None:
        with patch.dict(os.environ, {'T6_PERSON_ID': 'test-actor', 'T6_REGISTRY_DB': str(self.path)}):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(['project', 'activity', self.project.id, '--json', '--limit', '1']), 0)
            self.assertEqual(json.loads(output.getvalue())['result']['entries'][0]['action'], 'registered')
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(['project', 'activity', self.project.id, '--since', '1970-01-01']), 0)
            self.assertIn('person  registered', output.getvalue())


if __name__ == '__main__':
    unittest.main()
