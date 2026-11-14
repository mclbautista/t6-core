import sqlite3
import tempfile
import unittest
from pathlib import Path

from t6.ids import EntityKind, T6Id
from t6.registry.sqlite import SqliteProjectRegistry


class IdTests(unittest.TestCase):
    def test_every_kind_round_trips_and_rejects_wrong_kind(self):
        for kind in EntityKind:
            with self.subTest(kind=kind):
                identifier = T6Id.mint(kind)
                self.assertEqual(T6Id.parse(str(identifier), kind), identifier)
                other = next(k for k in EntityKind if k != kind)
                with self.assertRaises(ValueError):
                    T6Id.parse(str(identifier), other)
                with self.assertRaises(ValueError):
                    T6Id.parse(str(identifier) + '!', kind)


class MigrationTests(unittest.TestCase):
    def test_old_database_upgrades_without_rewriting_bindings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'db'
            with sqlite3.connect(path) as db:
                db.executescript((Path(__file__).parent / 'fixtures/registry_schema_v0.sql').read_text())
                db.execute("INSERT INTO projects VALUES ('t6p_0123456789abcdef01234567', 1, NULL, NULL, 'out', 1)")
                db.execute("INSERT INTO audit (project_id, action, actor, at, before, after) VALUES ('t6p_0123456789abcdef01234567', 'registered', 'legacy', 1, NULL, '{}')")
                db.execute("INSERT INTO bindings VALUES ('notion', 'old', 't6p_0123456789abcdef01234567', 1, 0, 1)")
            registry = SqliteProjectRegistry(path)
            self.assertEqual(registry.get_by_page('old').id, 't6p_0123456789abcdef01234567')
            self.assertEqual(registry.activity('t6p_0123456789abcdef01234567')[0].actor, 'legacy')
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute('SELECT version, forward_only FROM schema_version ORDER BY version').fetchall(), [(1, 0), (2, 1), (3, 1), (4, 1)])
            SqliteProjectRegistry(path)

    def test_version_one_upgrades(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'db'
            with sqlite3.connect(path) as db:
                db.executescript((Path(__file__).parent / 'fixtures/registry_schema_v0.sql').read_text())
                db.execute('CREATE TABLE schema_version (version INTEGER PRIMARY KEY, forward_only INTEGER NOT NULL)')
                db.execute('INSERT INTO schema_version VALUES (1, 0)')
            SqliteProjectRegistry(path)
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute('SELECT version FROM schema_version ORDER BY version').fetchall(), [(1,), (2,), (3,), (4,)])
                self.assertIn('entity_id', [row[1] for row in db.execute('PRAGMA table_info(bindings)')])

    def test_non_project_bindings_keep_legacy_pages(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = SqliteProjectRegistry(Path(directory) / 'db')
            identifier = T6Id.mint(EntityKind.DELIVERABLE)
            registry.bind_entity('notion', 'first', identifier)
            registry.bind_entity('notion', 'second', identifier)
            self.assertEqual(registry.bound_entity('notion', 'first'), str(identifier))
            self.assertEqual(registry.bound_entity('notion', 'second'), str(identifier))
            with sqlite3.connect(Path(directory) / 'db') as db:
                self.assertEqual(db.execute('SELECT page_id, active FROM bindings ORDER BY page_id').fetchall(), [('first', 0), ('second', 1)])
            with self.assertRaises(ValueError):
                registry.bind_entity('notion', 'first', T6Id.mint(EntityKind.PERSON))
