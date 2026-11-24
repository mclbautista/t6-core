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
                self.assertEqual(db.execute('SELECT version, forward_only FROM schema_version ORDER BY version').fetchall(), [(1, 0), (2, 0), (3, 0), (4, 0), (5, 0), (6, 0), (7, 0), (8, 0), (9, 0), (10, 0), (11, 0), (12, 0), (13, 0), (14, 0), (15, 0), (16, 0), (17, 0), (18, 0), (19, 0), (20, 0), (21, 0), (22, 1), (23, 0), (24, 0), (25, 0), (26, 0), (27, 1), (28, 0), (29, 0), (30, 0), (31, 0), (32, 0), (33, 0), (34, 0), (35, 0), (36, 0), (37, 0), (38, 0), (39, 0), (40, 0), (41, 0), (42, 0), (43, 0), (44, 0), (45, 0), (46, 0), (47, 0), (48, 0), (49, 0), (50, 0), (51, 0), (52, 0), (53, 0), (54, 0), (55, 0), (56, 0), (57, 0), (58, 0), (59, 0), (60, 0), (61, 0), (62, 0), (63, 0), (64, 0), (65, 0), (66, 0)])
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
                self.assertEqual(db.execute('SELECT version FROM schema_version ORDER BY version').fetchall(), [(1,), (2,), (3,), (4,), (5,), (6,), (7,), (8,), (9,), (10,), (11,), (12,), (13,), (14,), (15,), (16,), (17,), (18,), (19,), (20,), (21,), (22,), (23,), (24,), (25,), (26,), (27,), (28,), (29,), (30,), (31,), (32,), (33,), (34,), (35,), (36,), (37,), (38,), (39,), (40,), (41,), (42,), (43,), (44,), (45,), (46,), (47,), (48,), (49,), (50,), (51,), (52,), (53,), (54,), (55,), (56,), (57,), (58,), (59,), (60,), (61,), (62,), (63,), (64,), (65,), (66,)])
                self.assertIn('project_id', [row[1] for row in db.execute('PRAGMA table_info(bindings)')])

    def test_version_sixteen_upgrades_with_all_features(self) -> None:
        from t6.registry import sqlite as migrations
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'db'
            with patch.object(migrations, '_MIGRATIONS', migrations._MIGRATIONS[:43]):
                SqliteProjectRegistry(path)
            SqliteProjectRegistry(path)
            with closing(sqlite3.connect(path)) as db, db:
                self.assertEqual(db.execute('SELECT MAX(version) FROM schema_version').fetchone(), (66,))
                self.assertIsNotNone(db.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'milestones'").fetchone())

    def test_version_nineteen_upgrades_packages(self) -> None:
        from t6.registry import sqlite as migrations
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'db'
            with patch.object(migrations, '_MIGRATIONS', migrations._MIGRATIONS[:44]):
                SqliteProjectRegistry(path)
            SqliteProjectRegistry(path)
            with closing(sqlite3.connect(path)) as db, db:
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
                self.assertTrue({'concerns', 'concern_notes', 'project_archives', 'project_person_roles', 'approvals', 'deliverables', 'deliverable_sync_intents', 'deliverable_migration_receipts', 'milestones', 'packages', 'concern_brief_messages', 'chat_links', 'chat_link_history', 'chat_link_previews'} <= tables)
                self.assertEqual(db.execute('SELECT MAX(version) FROM schema_version').fetchone(), (66,))

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
                self.assertEqual([row[0] for row in db.execute('SELECT version FROM schema_version ORDER BY version')], list(range(1, 67)))
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
                self.assertTrue({'abang_requests', 'concern_pm_attempts', 'bays', 'media_locations', 'drives', 'transcripts', 'transcript_speaker_maps',
                                 'timelines', 'calendar_channels', 'frameio_held_activity', 'drive_shipment_facts', 'bay_scan_reports'} <= tables)
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

    def test_timelines_linked_before_the_stamp_state_migrate_as_stamped(self) -> None:
        from t6.registry import sqlite as migrations
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'db'
            with patch.object(migrations, '_MIGRATIONS', migrations._MIGRATIONS[:47]):
                registry = SqliteProjectRegistry(path)
                project = registry.register(actor='setup', idempotency_key='setup')
                assert isinstance(project, Registered)
                from media_seed import link_media
                media = link_media(registry, project.project.id, 'db/folder/show', kind='resolve-project', role='working')
                with registry._transaction() as db:
                    db.execute('INSERT INTO timelines VALUES (?, ?, ?, ?)', ('t6t_' + 'a' * 32, project.project.id, media, 'Ep1'))
            SqliteProjectRegistry(path)
            with closing(sqlite3.connect(path)) as db, db:
                self.assertEqual(db.execute('SELECT stamp_state, stamp_reason, replaces, resolve_uuid FROM timelines').fetchall(), [('stamped', None, None, None)])

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
                db.execute('INSERT INTO schema_version VALUES (67, 0)')
            SqliteProjectRegistry(path)
            with closing(sqlite3.connect(path)) as db, db:
                db.execute('INSERT INTO schema_version VALUES (68, 1)')
            with self.assertRaises(RegistryError):
                SqliteProjectRegistry(path)

    def test_forward_only_test_migration_blocks_older_release(self) -> None:
        from t6.registry import RegistryError
        from t6.registry import sqlite as migrations
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'db'
            SqliteProjectRegistry(path)
            with patch.object(migrations, '_MIGRATIONS', migrations._MIGRATIONS + ((67, True, "CREATE TABLE incompatible (id INTEGER)"),)):
                SqliteProjectRegistry(path)
            with self.assertRaises(RegistryError):
                SqliteProjectRegistry(path)

    def test_migration_61_backfills_open_chat_link_approvals(self) -> None:
        import json
        from t6.registry import sqlite as migrations
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'db'
            with patch.object(migrations, '_MIGRATIONS', migrations._MIGRATIONS[:60]):
                registry = SqliteProjectRegistry(path)
                with registry._transaction() as db:
                    db.execute("INSERT INTO approvals VALUES ('t6a_open', 'chat link', 'Show [S1] t6p_1: chat -100, topic 12', 't6n_1', '\"permission:operator\"', 'requested', NULL, NULL, NULL)")
                    db.execute("INSERT INTO audit (project_id, action, actor, at, before, after) VALUES ('t6a_open', 'approval_request', 't6n_1', 1000.0, NULL, '{}')")
                    db.execute("INSERT INTO approvals VALUES ('t6a_done', 'chat link', 'Show [S1] t6p_1: chat -100, topic 13', 't6n_1', '\"permission:operator\"', 'granted', 't6n_2', 1200.0, NULL)")
                    db.execute("INSERT INTO audit (project_id, action, actor, at, before, after) VALUES ('t6a_done', 'approval_request', 't6n_1', 1100.0, NULL, '{}')")
                    db.execute("INSERT INTO approvals VALUES ('t6a_other', 'vfx committee', 't6p_1', 't6n_1', '\"permission:operator\"', 'requested', NULL, NULL, NULL)")
                    db.execute("INSERT INTO audit (project_id, action, actor, at, before, after) VALUES ('t6a_other', 'approval_request', 't6n_1', 1150.0, NULL, '{}')")
            SqliteProjectRegistry(path)
            with closing(sqlite3.connect(path)) as db:
                rows = db.execute("SELECT queue, kind, dedupe_key, payload, run_at, state, created_at FROM internal_jobs WHERE kind = 'chat_link_reminder'").fetchall()
            self.assertEqual(rows, [('main', 'chat_link_reminder', 't6a_open', json.dumps({'approval': 't6a_open'}, separators=(',', ':')), 1000.0 + 86400, 'queued', 1000.0)])

    def test_project_and_other_entity_cannot_share_notion_page(self) -> None:
        from t6.registry import PageAlreadyBound
        with tempfile.TemporaryDirectory() as directory:
            registry = SqliteProjectRegistry(Path(directory) / 'db')
            page = '12345678-1234-1234-1234-123456789abc'
            registry.bind_entity('notion', page, T6Id.mint(EntityKind.PERSON), actor='editor')
            result = registry.register(actor='editor', idempotency_key='one', notion_page_id=page.replace('-', ''))
            self.assertIsInstance(result, PageAlreadyBound)


class SnoozeRemovalMigrationTests(unittest.TestCase):
    """Migration 56: snooze is gone. Every snoozed Concern is reopened with no override; no row is deleted."""

    def test_snoozed_rows_become_open_with_no_override_and_other_rows_are_untouched(self) -> None:
        from t6.registry import sqlite as migrations
        from t6.concern import ConcernStore
        from t6.preview import PreviewTokens
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'db'
            with patch.object(migrations, '_MIGRATIONS', migrations._MIGRATIONS[:54]):
                registry = SqliteProjectRegistry(path)
                project = registry.register(actor='setup', idempotency_key='setup')
                assert isinstance(project, Registered)
                with registry._transaction() as db:
                    for concern, state, override, evidence in (('t6c_snoozed', 'snoozed', 1, 1.0), ('t6c_open', 'open', 1, 2.0), ('t6c_closed', 'closed', 1, 3.0), ('t6c_plain', 'open', 0, 4.0)):
                        db.execute("INSERT INTO concerns (id, project_id, owner_id, waiting_on, anchor, as_of, opened_at, evidence_at, state, manual_override, revision, text, text_key) VALUES (?, ?, NULL, '', ?, '2026-11-01', 1.0, ?, ?, ?, 3, ?, ?)",
                                   (concern, project.project.id, 'Line ' + concern, evidence, state, override, 'Line ' + concern, 'line ' + concern))
            with closing(sqlite3.connect(path)) as db:
                before = {r[0]: r[1:] for r in db.execute('SELECT id, state, manual_override, revision, evidence_at, opened_at, text FROM concerns')}
            migrated = SqliteProjectRegistry(path)
            with closing(sqlite3.connect(path)) as db:
                after = {r[0]: r[1:] for r in db.execute('SELECT id, state, manual_override, revision, evidence_at, opened_at, text FROM concerns')}
            self.assertEqual(after['t6c_snoozed'], ('open', 0, 4, 1.0, 1.0, 'Line t6c_snoozed'))
            self.assertEqual({k: v for k, v in after.items() if k != 't6c_snoozed'}, {k: v for k, v in before.items() if k != 't6c_snoozed'})
            listed = ConcernStore(migrated, PreviewTokens('k')).raise_at_psr()
            self.assertEqual([c['id'] for c in listed], ['t6c_snoozed', 't6c_open', 't6c_plain'])


class ConcernMigrationTests(unittest.TestCase):
    """Migration 44: the Concern carries its text and source, and may have no owner."""

    def seeded(self, path: Path) -> str:
        from t6.registry import sqlite as migrations
        with patch.object(migrations, '_MIGRATIONS', migrations._MIGRATIONS[:43]):
            registry = SqliteProjectRegistry(path)
            project = registry.register(actor='setup', idempotency_key='setup')
            assert isinstance(project, Registered)
            project_id = project.project.id
            with registry._transaction() as db:
                db.execute("INSERT INTO persons VALUES ('coordinator', 'staff', '[]', '[]', NULL, 0, 1)")
                for concern, state, anchor, opened in (('t6c_open', 'open', 'Waiting on  Client', 1.0), ('t6c_quiet', 'quiet', 'Waiting on lab', 2.0), ('t6c_expired', 'expired', 'Waiting on Editor', 3.0),
                                                       ('t6c_closed', 'closed', 'Waiting on Client', 4.0), ('t6c_rule', 'open', 'submission is before playdate', 5.0), ('t6c_twin', 'open', 'waiting on client', 6.0),
                                                       ('t6c_scan', 'open', 'Waiting on grader', 7.0), ('t6c_unclosed', 'closed', 'Waiting on nobody', 8.0)):
                    db.execute("INSERT INTO concerns (id, project_id, owner_id, waiting_on, anchor, as_of, opened_at, evidence_at, state, manual_override, revision) VALUES (?, ?, 'coordinator', 'x', ?, '2026-11-01', ?, ?, ?, 1, 3)", (concern, project_id, anchor, opened, opened, state))
                db.execute("INSERT INTO concern_notes (concern_id, text, actor, at, message_id) VALUES ('t6c_open', 'a note', 'person', 9.0, '7')")
                db.execute("INSERT INTO concern_brief_messages VALUES ('chat', '1', 't6c_open')")
                db.execute("INSERT INTO concern_date_rules VALUES (?, 'submission_before_playdate', 't6c_rule')", (project_id,))
                db.execute("INSERT INTO concern_pm_changes (project_id, concern_id, action, quote, at) VALUES (?, 't6c_scan', 'concern_auto_raise', 'q', 1.0)", (project_id,))
                for at, action in ((50.0, 'concern_resolve'), (60.0, 'concern_auto_resolve'), (55.0, 'concern_note')):
                    db.execute("INSERT INTO audit (project_id, action, actor, at, before, after) VALUES ('t6c_closed', ?, 'person', ?, NULL, '{}')", (action, at))
        return project_id

    def test_every_row_note_and_message_link_survives_and_is_backfilled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'db'
            self.seeded(path)
            with closing(sqlite3.connect(path)) as db:
                before = db.execute('SELECT id, project_id, owner_id, waiting_on, anchor, as_of, opened_at, evidence_at, state, manual_override, settles_id, revision FROM concerns ORDER BY id').fetchall()
            SqliteProjectRegistry(path)
            with closing(sqlite3.connect(path)) as db:
                after = db.execute('SELECT id, project_id, owner_id, waiting_on, anchor, as_of, opened_at, evidence_at, state, manual_override, settles_id, revision FROM concerns ORDER BY id').fetchall()
                self.assertEqual(before, after)
                self.assertEqual(len(after), 8)
                rows = {r[0]: r[1:] for r in db.execute('SELECT id, text, source, text_key, closed_at FROM concerns')}
                self.assertEqual(rows['t6c_open'], ('Waiting on  Client', 'person', 'waiting on client', None))
                self.assertEqual(rows['t6c_rule'][1], 'date_rule')
                self.assertEqual(rows['t6c_scan'][1], 'pm_notes_scan')
                self.assertEqual(rows['t6c_closed'][2:], ('waiting on client', 60.0))
                self.assertEqual(rows['t6c_unclosed'][3], None)
                # The newer of two open twins gives up the key so the unique index can exist; the closed one keeps its own.
                self.assertEqual((rows['t6c_open'][2], rows['t6c_twin'][2]), ('waiting on client', None))
                self.assertEqual(db.execute('SELECT COUNT(*) FROM concern_notes').fetchone(), (1,))
                self.assertEqual(db.execute('SELECT concern_id FROM concern_brief_messages').fetchall(), [('t6c_open',)])
                self.assertEqual(db.execute('SELECT concern_id FROM concern_date_rules').fetchall(), [('t6c_rule',)])
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute("INSERT INTO concerns (id, project_id, owner_id, waiting_on, anchor, as_of, opened_at, evidence_at, state, text_key) SELECT 'dup', project_id, NULL, '', 'x', 'y', 0, 0, 'open', text_key FROM concerns WHERE id = 't6c_open'")
                db.execute("INSERT INTO concerns (id, project_id, owner_id, waiting_on, anchor, as_of, opened_at, evidence_at, state) SELECT 'noowner', project_id, NULL, '', 'x', 'y', 0, 0, 'open' FROM concerns WHERE id = 't6c_open'")
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute('DELETE FROM concern_notes')

    def test_the_previous_release_opens_and_writes_the_migrated_file(self) -> None:
        from t6.registry import sqlite as migrations
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'db'
            project = self.seeded(path)
            SqliteProjectRegistry(path)
            with patch.object(migrations, '_MIGRATIONS', migrations._MIGRATIONS[:43]):
                previous = SqliteProjectRegistry(path)
                with previous._transaction() as db:
                    db.execute("INSERT INTO concerns (id, project_id, owner_id, waiting_on, anchor, as_of, opened_at, evidence_at, state) VALUES ('t6c_old', ?, 'coordinator', 'lab', 'Waiting on lab', '2026-11-01', 1, 1, 'open')", (project,))
                    self.assertEqual(db.execute("SELECT owner_id, waiting_on, anchor, state, manual_override, revision FROM concerns WHERE id = 't6c_old'").fetchone(), ('coordinator', 'lab', 'Waiting on lab', 'open', 0, 1))
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM concerns").fetchone(), (9,))
            from t6.concern import ConcernStore
            from t6.preview import PreviewTokens
            record = ConcernStore(SqliteProjectRegistry(path), PreviewTokens('k')).get('t6c_old')
            assert record is not None
            self.assertEqual((record['text'], record['source']), ('Waiting on lab', 'person'))

    def test_the_release_before_the_listings_table_opens_the_migrated_file(self) -> None:
        from t6.registry import sqlite as migrations
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'db'
            project = self.seeded(path)
            SqliteProjectRegistry(path)
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("INSERT INTO concern_listings VALUES ('2026-11-16', 't6c_open')")
                db.execute("INSERT INTO concern_brief_messages (chat, message_id, concern_id, brief_date) VALUES ('chat', '9', 't6c_open', '2026-11-16')")
            with patch.object(migrations, '_MIGRATIONS', migrations._MIGRATIONS[:44]):
                previous = SqliteProjectRegistry(path)
                with previous._transaction() as db:
                    db.execute("INSERT INTO concern_brief_messages (chat, message_id, concern_id) VALUES ('chat', '10', 't6c_open')")
                    self.assertEqual(db.execute('SELECT chat, message_id, concern_id FROM concern_brief_messages ORDER BY message_id').fetchall(), [('chat', '1', 't6c_open'), ('chat', '10', 't6c_open'), ('chat', '9', 't6c_open')])
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM concerns WHERE project_id = ?', (project,)).fetchone(), (8,))
