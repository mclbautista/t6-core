import sqlite3
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
            with sqlite3.connect(path) as db:
                db.executescript((Path(__file__).parent / 'fixtures/registry_schema_v0.sql').read_text())
                db.execute("INSERT INTO projects VALUES ('t6p_0123456789abcdef01234567', 1, NULL, NULL, 'out', 1)")
                db.execute("INSERT INTO audit (project_id, action, actor, at, before, after) VALUES ('t6p_0123456789abcdef01234567', 'registered', 'legacy', 1, NULL, '{}')")
                db.execute("INSERT INTO bindings VALUES ('notion', 'old', 't6p_0123456789abcdef01234567', 1, 0, 1)")
            registry = SqliteProjectRegistry(path)
            project = registry.get_by_page('old')
            assert project is not None
            self.assertEqual(project.id, 't6p_0123456789abcdef01234567')
            self.assertEqual(registry.activity('t6p_0123456789abcdef01234567')[0].actor, 'legacy')
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute('SELECT version, forward_only FROM schema_version ORDER BY version').fetchall(), [(1, 0), (2, 0), (3, 0), (4, 0), (5, 0), (6, 0), (7, 0), (8, 0), (9, 0), (10, 0), (11, 0), (12, 0), (13, 0), (14, 0), (15, 0), (16, 0), (17, 0), (18, 0), (19, 0), (20, 0), (21, 0), (22, 1), (23, 0), (24, 0), (25, 0), (26, 0), (27, 0), (28, 0), (29, 0), (30, 0)])
            SqliteProjectRegistry(path)

    def test_previous_release_write_is_reconciled_on_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "db"
            registry = SqliteProjectRegistry(path)
            project = registry.register(actor="new", idempotency_key="new")
            assert isinstance(project, Registered)
            project_id = project.project.id
            # The previous release writes only to the original audit table.
            with sqlite3.connect(path) as old_release:
                old_release.execute("INSERT INTO audit (project_id, action, actor, at, before, after) VALUES (?, 'legacy_change', 'old', 42, NULL, '{}')", (project_id,))
            reopened = SqliteProjectRegistry(path)
            self.assertEqual(sum(item.actor == "old" for item in reopened.activity(project_id)), 1)
            reopened = SqliteProjectRegistry(path)
            self.assertEqual(sum(item.actor == "old" for item in reopened.activity(project_id)), 1)

    def test_version_one_upgrades(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'db'
            with sqlite3.connect(path) as db:
                db.executescript((Path(__file__).parent / 'fixtures/registry_schema_v0.sql').read_text())
                db.execute('CREATE TABLE schema_version (version INTEGER PRIMARY KEY, forward_only INTEGER NOT NULL)')
                db.execute('INSERT INTO schema_version VALUES (1, 0)')
            SqliteProjectRegistry(path)
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute('SELECT version FROM schema_version ORDER BY version').fetchall(), [(1,), (2,), (3,), (4,), (5,), (6,), (7,), (8,), (9,), (10,), (11,), (12,), (13,), (14,), (15,), (16,), (17,), (18,), (19,), (20,), (21,), (22,), (23,), (24,), (25,), (26,), (27,), (28,), (29,), (30,)])
                self.assertIn('project_id', [row[1] for row in db.execute('PRAGMA table_info(bindings)')])

    def test_version_fifteen_upgrades_with_all_features(self) -> None:
        from t6.registry import sqlite as migrations
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'db'
            with patch.object(migrations, '_MIGRATIONS', migrations._MIGRATIONS[:15]):
                SqliteProjectRegistry(path)
            SqliteProjectRegistry(path)
            with sqlite3.connect(path) as db:
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
                self.assertTrue({'concerns', 'concern_notes', 'project_archives', 'project_person_roles', 'approvals', 'deliverables', 'deliverable_sync_intents', 'deliverable_migration_receipts', 'concern_brief_messages', 'chat_links', 'chat_link_history', 'chat_link_previews', 'milestones', 'packages'} <= tables)
                self.assertIn('message_id', [row[1] for row in db.execute('PRAGMA table_info(concern_notes)')])
                self.assertEqual(db.execute('SELECT MAX(version) FROM schema_version').fetchone(), (30,))

    def test_version_sixteen_upgrades_concern_messages(self) -> None:
        from t6.registry import sqlite as migrations
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'db'
            with patch.object(migrations, '_MIGRATIONS', migrations._MIGRATIONS[:16]):
                SqliteProjectRegistry(path)
            SqliteProjectRegistry(path)
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute('SELECT MAX(version) FROM schema_version').fetchone(), (30,))
                self.assertIn('message_id', [row[1] for row in db.execute('PRAGMA table_info(concern_notes)')])
                self.assertIsNotNone(db.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'concern_brief_messages'").fetchone())

    def test_version_seventeen_upgrades_chat_links(self) -> None:
        from t6.registry import sqlite as migrations
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'db'
            with patch.object(migrations, '_MIGRATIONS', migrations._MIGRATIONS[:17]):
                SqliteProjectRegistry(path)
            SqliteProjectRegistry(path)
            with sqlite3.connect(path) as db:
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
                self.assertTrue({'chat_links', 'chat_link_history', 'chat_link_previews'} <= tables)
                self.assertEqual(db.execute('SELECT MAX(version) FROM schema_version').fetchone(), (30,))

    def test_version_eighteen_upgrades_milestones(self) -> None:
        from t6.registry import sqlite as migrations
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'db'
            with patch.object(migrations, '_MIGRATIONS', migrations._MIGRATIONS[:18]):
                SqliteProjectRegistry(path)
            SqliteProjectRegistry(path)
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute('SELECT MAX(version) FROM schema_version').fetchone(), (30,))
                self.assertIsNotNone(db.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'milestones'").fetchone())

    def test_version_nineteen_upgrades_packages(self) -> None:
        from t6.registry import sqlite as migrations
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'db'
            with patch.object(migrations, '_MIGRATIONS', migrations._MIGRATIONS[:19]):
                SqliteProjectRegistry(path)
            SqliteProjectRegistry(path)
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute('SELECT MAX(version) FROM schema_version').fetchone(), (30,))
                self.assertIsNotNone(db.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'packages'").fetchone())

    def test_version_twenty_one_upgrades_delivery(self) -> None:
        from t6.registry import sqlite as migrations
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'db'
            with patch.object(migrations, '_MIGRATIONS', migrations._MIGRATIONS[:21]):
                SqliteProjectRegistry(path)
            with sqlite3.connect(path) as db:
                db.execute("INSERT INTO packages VALUES ('package', 'deliverable', 'kind', 'sender', 'destination', 'recipient', NULL, '{}', '[]', 'prepared', NULL, 1)")
            SqliteProjectRegistry(path)
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute('SELECT id, recipient FROM packages').fetchall(), [('package', 'recipient')])
                self.assertEqual(db.execute('SELECT version, forward_only FROM schema_version WHERE version >= 21 ORDER BY version').fetchall(), [(21, 0), (22, 1), (23, 0), (24, 0), (25, 0), (26, 0), (27, 0), (28, 0), (29, 0), (30, 0)])
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
                self.assertTrue({'deliverable_date_moves', 'deliverable_misses', 'delivery_carriers', 'linked_chat_cursor', 'linked_chat_messages', 'linked_chat_members', 'versions', 'version_locks', 'concern_milestones', 'concern_date_rules', 'delivery_share_views', 'linked_chat_topics', 'linked_chat_notices', 'linked_chat_sends'} <= tables)

    def test_version_twenty_six_upgrades_linked_chat_events(self) -> None:
        from t6.registry import sqlite as migrations
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'db'
            with patch.object(migrations, '_MIGRATIONS', migrations._MIGRATIONS[:26]):
                SqliteProjectRegistry(path)
            SqliteProjectRegistry(path)
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute('SELECT version FROM schema_version WHERE version >= 25 ORDER BY version').fetchall(), [(25,), (26,), (27,), (28,), (29,), (30,)])
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
                self.assertTrue({'concern_milestones', 'concern_date_rules', 'delivery_share_views', 'linked_chat_topics', 'linked_chat_notices', 'linked_chat_sends'} <= tables)

    def test_open_abang_request_gets_a_timer_on_upgrade(self) -> None:
        from t6.registry import sqlite as migrations
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'db'
            with patch.object(migrations, '_MIGRATIONS', migrations._MIGRATIONS[:28]):
                SqliteProjectRegistry(path)
            with sqlite3.connect(path) as db:
                db.execute("INSERT INTO abang_requests (id, person_id, source_chat, destination_chat, actor, state) VALUES ('request', 'vendor', -100, -200, 'coordinator', 'requested')")
                db.execute("INSERT INTO abang_history (request_id, state, actor, at) VALUES ('request', 'requested', 'coordinator', 100)")
            SqliteProjectRegistry(path)
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute("SELECT kind, run_at, state FROM internal_jobs WHERE dedupe_key = 'request'").fetchone(), ('abang_nudge', 259300, 'queued'))

    def test_version_thirty_upgrades_existing_abang_schema(self) -> None:
        from t6.registry import sqlite as migrations
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'db'
            with patch.object(migrations, '_MIGRATIONS', migrations._MIGRATIONS[:29]):
                SqliteProjectRegistry(path)
            SqliteProjectRegistry(path)
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute('SELECT MAX(version) FROM schema_version').fetchone(), (30,))
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
                self.assertTrue({'abang_candidates', 'concern_pm_lines', 'concern_pm_pending', 'concern_pm_changes'} <= tables)

    def test_non_project_bindings_keep_legacy_pages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = SqliteProjectRegistry(Path(directory) / 'db')
            identifier = T6Id.mint(EntityKind.DELIVERABLE)
            registry.bind_entity('notion', 'first', identifier, actor='editor')
            registry.bind_entity('notion', 'second', identifier, actor='editor')
            self.assertEqual(registry.bound_entity('notion', 'first'), str(identifier))
            self.assertEqual(registry.bound_entity('notion', 'second'), str(identifier))
            with sqlite3.connect(Path(directory) / 'db') as db:
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

    def test_newer_schema_compatibility(self) -> None:
        from t6.registry import RegistryError
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'db'
            SqliteProjectRegistry(path)
            with sqlite3.connect(path) as db:
                db.execute('INSERT INTO schema_version VALUES (31, 0)')
            SqliteProjectRegistry(path)
            with sqlite3.connect(path) as db:
                db.execute('INSERT INTO schema_version VALUES (32, 1)')
            with self.assertRaises(RegistryError):
                SqliteProjectRegistry(path)

    def test_forward_only_test_migration_blocks_older_release(self) -> None:
        from t6.registry import RegistryError
        from t6.registry import sqlite as migrations
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'db'
            SqliteProjectRegistry(path)
            with patch.object(migrations, '_MIGRATIONS', migrations._MIGRATIONS + ((31, True, "CREATE TABLE incompatible (id INTEGER)"),)):
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
