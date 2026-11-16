import sqlite3
from contextlib import closing
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from t6.ids import EntityKind, T6Id
from t6.registry import Registered
from t6.registry.sqlite import SqliteProjectRegistry


class IdTests(unittest.TestCase):
    def test_every_kind_round_trips_and_rejects_wrong_kind(self) -> None:
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
    def test_old_database_upgrades_without_rewriting_bindings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'db'
            with closing(sqlite3.connect(path)) as db, db:
                db.executescript((Path(__file__).parent / 'fixtures/registry_schema_v0.sql').read_text())
                db.execute("INSERT INTO projects VALUES ('t6p_0123456789abcdef01234567', 1, NULL, NULL, 'out', 1)")
                db.execute("INSERT INTO audit (project_id, action, actor, at, before, after) VALUES ('t6p_0123456789abcdef01234567', 'registered', 'legacy', 1, NULL, '{}')")
                db.execute("INSERT INTO bindings VALUES ('notion', 'old', 't6p_0123456789abcdef01234567', 1, 0, 1)")
            registry = SqliteProjectRegistry(path)
            project = registry.get_by_page('old')
            assert project is not None
            self.assertEqual(project.id, 't6p_0123456789abcdef01234567')
            self.assertEqual(registry.activity('t6p_0123456789abcdef01234567')[0].actor, 'legacy')
            with closing(sqlite3.connect(path)) as db, db:
                self.assertEqual(db.execute('SELECT version, forward_only FROM schema_version ORDER BY version').fetchall(), [(1, 0), (2, 0), (3, 0), (4, 0), (5, 0), (6, 0), (7, 0), (8, 0), (9, 0), (10, 0), (11, 0), (12, 0), (13, 0), (14, 0), (15, 0), (16, 0), (17, 0), (18, 0), (19, 0), (20, 0), (21, 0), (22, 1), (23, 0), (24, 0), (25, 0), (26, 0), (27, 1), (28, 0), (29, 0), (30, 0), (31, 0), (32, 0), (33, 0), (34, 0), (35, 0), (36, 0), (37, 0), (38, 0), (39, 0), (40, 0), (41, 0), (42, 0)])
            SqliteProjectRegistry(path)

    def test_previous_release_write_is_reconciled_on_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "db"
            registry = SqliteProjectRegistry(path)
            project = registry.register(actor="new", idempotency_key="new")
            assert isinstance(project, Registered)
            project_id = project.project.id
            # The previous release writes only to the original audit table.
            with closing(sqlite3.connect(path)) as old_release, old_release:
                old_release.execute("INSERT INTO audit (project_id, action, actor, at, before, after) VALUES (?, 'legacy_change', 'old', 42, NULL, '{}')", (project_id,))
            reopened = SqliteProjectRegistry(path)
            self.assertEqual(sum(item.actor == "old" for item in reopened.activity(project_id)), 1)
            reopened = SqliteProjectRegistry(path)
            self.assertEqual(sum(item.actor == "old" for item in reopened.activity(project_id)), 1)

    def test_version_one_upgrades(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'db'
            with closing(sqlite3.connect(path)) as db, db:
                db.executescript((Path(__file__).parent / 'fixtures/registry_schema_v0.sql').read_text())
                db.execute('CREATE TABLE schema_version (version INTEGER PRIMARY KEY, forward_only INTEGER NOT NULL)')
                db.execute('INSERT INTO schema_version VALUES (1, 0)')
            SqliteProjectRegistry(path)
            with closing(sqlite3.connect(path)) as db, db:
                self.assertEqual(db.execute('SELECT version FROM schema_version ORDER BY version').fetchall(), [(1,), (2,), (3,), (4,), (5,), (6,), (7,), (8,), (9,), (10,), (11,), (12,), (13,), (14,), (15,), (16,), (17,), (18,), (19,), (20,), (21,), (22,), (23,), (24,), (25,), (26,), (27,), (28,), (29,), (30,), (31,), (32,), (33,), (34,), (35,), (36,), (37,), (38,), (39,), (40,), (41,), (42,)])
                self.assertIn('project_id', [row[1] for row in db.execute('PRAGMA table_info(bindings)')])

    def test_version_sixteen_upgrades_with_all_features(self) -> None:
        from t6.registry import sqlite as migrations
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'db'
            with patch.object(migrations, '_MIGRATIONS', migrations._MIGRATIONS[:-3]):
                SqliteProjectRegistry(path)
            SqliteProjectRegistry(path)
            with closing(sqlite3.connect(path)) as db, db:
                self.assertEqual(db.execute('SELECT MAX(version) FROM schema_version').fetchone(), (42,))
                self.assertIsNotNone(db.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'milestones'").fetchone())

    def test_version_nineteen_upgrades_packages(self) -> None:
        from t6.registry import sqlite as migrations
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'db'
            with patch.object(migrations, '_MIGRATIONS', migrations._MIGRATIONS[:-2]):
                SqliteProjectRegistry(path)
            SqliteProjectRegistry(path)
            with closing(sqlite3.connect(path)) as db, db:
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
                self.assertTrue({'concerns', 'concern_notes', 'project_archives', 'project_person_roles', 'approvals', 'deliverables', 'deliverable_sync_intents', 'deliverable_migration_receipts', 'milestones', 'packages', 'concern_brief_messages', 'chat_links', 'chat_link_history', 'chat_link_previews'} <= tables)
                self.assertEqual(db.execute('SELECT MAX(version) FROM schema_version').fetchone(), (42,))

    def test_database_at_main_schema_upgrades_through_the_phase_3_migrations(self) -> None:
        from t6.registry import sqlite as migrations
        main_latest = 32  # the last migration the 0.5.0 release carries; the phase 3 migrations follow it
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'db'
            with patch.object(migrations, '_MIGRATIONS', migrations._MIGRATIONS[:main_latest]):
                SqliteProjectRegistry(path)
            with closing(sqlite3.connect(path)) as db, db:
                self.assertEqual(db.execute('SELECT MAX(version) FROM schema_version').fetchone(), (main_latest,))
                self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name = 'bays'").fetchone())
                self.assertIsNotNone(db.execute("SELECT name FROM sqlite_master WHERE name = 'abang_requests'").fetchone())
            SqliteProjectRegistry(path)
            with closing(sqlite3.connect(path)) as db, db:
                self.assertEqual([row[0] for row in db.execute('SELECT version FROM schema_version ORDER BY version')], list(range(1, 43)))
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
                self.assertTrue({'abang_requests', 'concern_pm_attempts', 'bays', 'media_locations', 'drives', 'transcripts', 'transcript_speaker_maps',
                                 'timelines', 'calendar_channels', 'frameio_held_activity'} <= tables)
                triggers = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
                self.assertTrue({'media_history_no_update', 'drive_events_no_delete', 'transcript_history_no_update', 'transcript_speaker_maps_no_delete'} <= triggers)

    def test_non_project_bindings_keep_legacy_pages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = SqliteProjectRegistry(Path(directory) / 'db')
            identifier = T6Id.mint(EntityKind.DELIVERABLE)
            registry.bind_entity('notion', 'first', identifier, actor='editor')
            registry.bind_entity('notion', 'second', identifier, actor='editor')
            self.assertEqual(registry.bound_entity('notion', 'first'), str(identifier))
            self.assertEqual(registry.bound_entity('notion', 'second'), str(identifier))
            with closing(sqlite3.connect(Path(directory) / 'db')) as db, db:
                self.assertEqual(db.execute('SELECT page_id, active FROM bindings ORDER BY page_id').fetchall(), [('first', 0), ('second', 1)])
            with self.assertRaises(ValueError):
                registry.bind_entity('notion', 'first', T6Id.mint(EntityKind.PERSON), actor='editor')

    def test_binding_normalization_and_audit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = SqliteProjectRegistry(Path(directory) / 'db')
            identifier = T6Id.mint(EntityKind.DELIVERABLE)
            page = '12345678-1234-1234-1234-123456789abc'
            registry.bind_entity('notion', page, identifier, actor='editor')
            registry.bind_entity('notion', page.replace('-', ''), identifier, actor='editor')
            self.assertEqual(registry.bound_entity('notion', page.replace('-', '')), str(identifier))
            self.assertEqual(len(registry.audit(str(identifier))), 1)
            self.assertEqual(registry.audit(str(identifier))[0].actor, 'editor')
            with self.assertRaises(ValueError):
                registry.bind_entity('notion', page.replace('-', ''), T6Id.mint(EntityKind.PERSON), actor='other')

    def test_reopen_does_not_treat_media_audit_as_project_activity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'db'
            registry = SqliteProjectRegistry(path)
            project = registry.register(actor='setup', idempotency_key='setup')
            assert isinstance(project, Registered)
            from media_seed import link_media
            media = link_media(registry, project.project.id, '/media')
            with registry._transaction() as db:
                db.execute("INSERT INTO audit (project_id, action, actor, at, before, after) VALUES (?, 'media_link', 'person', 1, NULL, '{}')", (media,))
            reopened = SqliteProjectRegistry(path)
            self.assertEqual(reopened.activity(media), ())

    def test_newer_schema_compatibility(self) -> None:
        from t6.registry import RegistryError
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'db'
            SqliteProjectRegistry(path)
            with closing(sqlite3.connect(path)) as db, db:
                db.execute('INSERT INTO schema_version VALUES (43, 0)')
            SqliteProjectRegistry(path)
            with closing(sqlite3.connect(path)) as db, db:
                db.execute('INSERT INTO schema_version VALUES (44, 1)')
            with self.assertRaises(RegistryError):
                SqliteProjectRegistry(path)

    def test_forward_only_test_migration_blocks_older_release(self) -> None:
        from t6.registry import RegistryError
        from t6.registry import sqlite as migrations
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'db'
            SqliteProjectRegistry(path)
            with patch.object(migrations, '_MIGRATIONS', migrations._MIGRATIONS + ((43, True, "CREATE TABLE incompatible (id INTEGER)"),)):
                SqliteProjectRegistry(path)
            with self.assertRaises(RegistryError):
                SqliteProjectRegistry(path)

    def test_project_and_other_entity_cannot_share_notion_page(self) -> None:
        from t6.registry import PageAlreadyBound
        with tempfile.TemporaryDirectory() as directory:
            registry = SqliteProjectRegistry(Path(directory) / 'db')
            page = '12345678-1234-1234-1234-123456789abc'
            registry.bind_entity('notion', page, T6Id.mint(EntityKind.PERSON), actor='editor')
            result = registry.register(actor='editor', idempotency_key='one', notion_page_id=page.replace('-', ''))
            self.assertIsInstance(result, PageAlreadyBound)
