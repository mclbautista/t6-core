"""Bay migration through the in-memory t6 database."""
import unittest
from typing import Any
from unittest.mock import patch

from t6.bay import BayStore
from t6.notion.generated import BaysRow
from t6.notion.testing import FakeNotion, title
from test_person import MemoryRegistry


class BayMigrationTests(unittest.TestCase):
    def test_migration_is_safe_twice_and_does_not_match_names(self) -> None:
        registry = MemoryRegistry()
        self.addCleanup(registry.db.close)
        fake = FakeNotion()
        fake.add_snapshot_data_source('bays', 'bays')
        first = fake.add_page('bays', {BaysRow.notion_name('name'): title('Bay A')})
        second = fake.add_page('bays', {BaysRow.notion_name('name'): title('Bay A')})
        rows = [BaysRow.from_page(page) for page in (first, second)]
        store = BayStore(registry)
        preview = store.migrate(rows, actor='test')
        applied = store.migrate(rows, actor='test', apply=True, digest=preview['digest'])
        ids = [entry['t6_id'] for entry in applied['entries']]
        self.assertEqual(len(set(ids)), 2)
        self.assertTrue(all(identifier.startswith('t6b_') for identifier in ids))
        again = store.migrate(rows, actor='test', apply=True, digest=preview['digest'])
        self.assertEqual([entry['t6_id'] for entry in again['entries']], ids)


class BookingTests(unittest.TestCase):
    def setUp(self) -> None:
        from t6.person import People
        from t6.preview import PreviewTokens
        from t6.registry import Registered
        import os
        from unittest.mock import patch
        env = patch.dict(os.environ, {'T6_PREVIEW_SIGNING_KEY': 'test'})
        env.start()
        self.addCleanup(env.stop)
        self.registry = MemoryRegistry()
        self.addCleanup(self.registry.db.close)
        self.people = People(self.registry, PreviewTokens('test'))
        with self.registry._transaction() as db:
            for person in ('writer', 'reader', 'editor'):
                db.execute("INSERT INTO persons VALUES (?, 'staff', '[]', '[]', NULL, 0, 1)", (person,))
            db.execute("INSERT INTO person_permissions VALUES ('writer', 'bookings')")
            db.execute("INSERT INTO person_shifts VALUES ('editor', 'night', '19:00', '04:00')")
        from t6.ids import EntityKind, T6Id
        self.registry._new_id = lambda: str(T6Id.mint(EntityKind.PROJECT))
        project = self.registry.register(actor='setup', idempotency_key='project')
        assert isinstance(project, Registered)
        self.project = project.project.id
        fake = FakeNotion()
        fake.add_snapshot_data_source('bays', 'bays')
        rows = [BaysRow.from_page(fake.add_page('bays', {BaysRow.notion_name('name'): title('Bay')})) for _ in range(2)]
        store = BayStore(self.registry)
        preview = store.migrate(rows, actor='setup')
        self.bays = [x['t6_id'] for x in store.migrate(rows, actor='setup', apply=True, digest=preview['digest'])['entries']]

    def call(self, noun: str, verb: str, values: dict[str, Any], actor: str = 'writer') -> Any:
        from t6.verbs import VERBS, invoke
        entry = next(e for e in VERBS if (e.noun, e.verb) == (noun, verb))
        return invoke(entry, entry.input_model.model_validate(values), actor=actor, people=self.people, open_notion=lambda _: None)

    def write(self, noun: str, verb: str, values: dict[str, Any]) -> Any:
        from t6.verbs import Preview, Done
        preview = self.call(noun, verb, values)
        self.assertIsInstance(preview, Preview)
        result = self.call(noun, verb, {**values, 'idempotency_key': preview.plan['idempotency_key'], 'confirm': preview.token})
        self.assertIsInstance(result, Done)
        return result

    def test_book_overlap_force_retry_and_permissions(self) -> None:
        from t6.verbs import Refused
        values = {'bay': self.bays[0], 'person': 'editor', 'project': self.project, 'start': '2026-11-20T10:00:00+08:00', 'end': '2026-11-20T19:00:00+08:00', 'idempotency_key': 'one'}
        refused = self.call('bay', 'book', values, actor='reader')
        self.assertIsInstance(refused, Refused)
        self.assertIn('bookings', refused.reason)
        first = self.write('bay', 'book', values)
        self.assertEqual(first, self.write('bay', 'book', values))
        clash = {**values, 'idempotency_key': 'two'}
        self.assertIsInstance(self.call('bay', 'book', clash), Refused)
        forced = self.write('bay', 'book', {**clash, 'force': True})
        self.assertTrue(forced.result['forced'])
        read = self.call('booking', 'get', {'booking': forced.result['booking_id']}, actor='reader')
        self.assertTrue(read.result['booking']['forced'])
        self.assertEqual(sum(item.action.startswith("booking_") for item in self.registry.activity(self.project)), 2)
        self.assertEqual(len(self.registry.audit(first.result['booking_id'])), 1)

    def book(self, key: str, start: str, end: str, bay: int = 0) -> str:
        done = self.write('bay', 'book', {'bay': self.bays[bay], 'person': 'editor', 'project': self.project, 'start': start, 'end': end, 'idempotency_key': key})
        return str(done.result['booking_id'])

    def sync_jobs(self, states: tuple[str, ...] = ('queued', 'claimed')) -> list[Any]:
        from t6.google.sync import BOOKING_SYNC_KIND
        from t6.worker.internal_queue import InternalJobQueue
        return InternalJobQueue(self.registry).find(BOOKING_SYNC_KIND, states=states)

    def settle_sync_jobs(self) -> None:
        from t6.worker.internal_queue import InternalJobQueue
        queue = InternalJobQueue(self.registry)
        for job in self.sync_jobs():
            queue.finish(job.job_id, {'event': 'skipped'})

    def test_a_booking_change_queues_its_sync_through_the_internal_job_queue(self) -> None:
        from t6.google.sync import BOOKING_SYNC_KIND
        from t6.worker.internal_queue import InternalJobQueue
        with patch.object(InternalJobQueue, 'submit', autospec=True, side_effect=InternalJobQueue.submit) as submit:
            booking = self.book('one', '2026-11-20T10:00:00+08:00', '2026-11-20T11:00:00+08:00')
        calls = [call for call in submit.call_args_list if call.args[2] == BOOKING_SYNC_KIND]
        self.assertEqual(len(calls), 1)
        self.assertIsNotNone(calls[0].kwargs.get('db'))
        self.assertEqual([job.payload['booking_id'] for job in self.sync_jobs()], [booking])

    def test_resync_queues_one_sync_per_booking_that_is_live_and_not_over(self) -> None:
        from t6.bay import BookingStore
        from t6.preview import PreviewTokens
        from datetime import datetime
        coming = self.book('coming', '2026-11-20T10:00:00+08:00', '2026-11-20T11:00:00+08:00')
        later = self.book('later', '2026-12-01T10:00:00+08:00', '2026-12-01T11:00:00+08:00', bay=1)
        over = self.book('over', '2026-11-10T10:00:00+08:00', '2026-11-10T11:00:00+08:00')
        cancelled = self.book('cancelled', '2026-11-21T10:00:00+08:00', '2026-11-21T11:00:00+08:00')
        self.write('booking', 'cancel', {'booking': cancelled})
        self.settle_sync_jobs()
        now = datetime.fromisoformat('2026-11-16T09:00:00+08:00').timestamp()
        store = BookingStore(self.registry, PreviewTokens('test'))
        self.assertEqual(store.resync_calendar(now=now), 2)
        self.assertEqual(sorted(job.payload['booking_id'] for job in self.sync_jobs()), sorted([coming, later]))
        self.assertNotIn(over, [job.payload['booking_id'] for job in self.sync_jobs()])

    def test_resync_twice_does_not_double_what_is_still_queued_but_queues_again_once_it_finished(self) -> None:
        from t6.bay import BookingStore
        from t6.preview import PreviewTokens
        from datetime import datetime
        self.book('coming', '2026-11-20T10:00:00+08:00', '2026-11-20T11:00:00+08:00')
        self.settle_sync_jobs()
        now = datetime.fromisoformat('2026-11-16T09:00:00+08:00').timestamp()
        store = BookingStore(self.registry, PreviewTokens('test'))
        self.assertEqual(store.resync_calendar(now=now), 1)
        self.assertEqual(store.resync_calendar(now=now + 60), 0)
        self.assertEqual(len(self.sync_jobs()), 1)
        self.settle_sync_jobs()  # the first run ended with no calendar configured; the second run, after the setting is fixed, must work
        self.assertEqual(store.resync_calendar(now=now + 120), 1)

    def test_admin_calendar_resync_command_prints_the_count(self) -> None:
        import io
        import json
        import tempfile
        from contextlib import redirect_stdout
        from pathlib import Path
        from t6.cli import main
        from t6.registry import SqliteProjectRegistry
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'registry.sqlite3'
            registry = SqliteProjectRegistry(path)
            with registry._transaction() as db:
                db.execute("INSERT INTO bookings VALUES ('t6g_one', 'bay', 'person', 'project', '2999-01-01T10:00:00+08:00', '2999-01-01T11:00:00+08:00', 0, 0)")
                db.execute("INSERT INTO bookings VALUES ('t6g_two', 'bay', 'person', 'project', '2999-01-02T10:00:00+08:00', '2999-01-02T11:00:00+08:00', 0, 1)")
                db.execute("INSERT INTO bookings VALUES ('t6g_old', 'bay', 'person', 'project', '2001-01-02T10:00:00+08:00', '2001-01-02T11:00:00+08:00', 0, 0)")
            for expected in (1, 0):
                out = io.StringIO()
                with redirect_stdout(out):
                    status = main(['admin', 'calendar', 'resync', '--registry-db', str(path)])
                self.assertEqual((status, json.loads(out.getvalue())['queued']), (0, expected))

    def test_shift_and_mutations(self) -> None:
        from t6.verbs import NeedsPerson, Refused
        values = {'bay': self.bays[0], 'person': 'reader', 'project': self.project, 'start': '2026-11-20', 'shift': 'night'}
        self.assertIsInstance(self.call('bay', 'book', values), NeedsPerson)
        result = self.write('bay', 'book', {**values, 'person': 'editor'})
        booking = result.result['booking_id']
        self.assertEqual(result.result['end'], '2026-11-21T04:00:00.000000+08:00')
        self.write('booking', 'move', {'booking': booking, 'bay': self.bays[1], 'start': '2026-11-22T10:00:00+08:00', 'end': '2026-11-22T12:00:00+08:00'})
        self.write('booking', 'extend', {'booking': booking, 'end': '2026-11-22T14:00:00+08:00'})
        self.write('booking', 'cancel', {'booking': booking})
        self.assertIsInstance(self.call('booking', 'extend', {'booking': booking, 'end': '2026-11-22T15:00:00+08:00'}), Refused)
        self.assertEqual(sum(item.action.startswith("booking_") for item in self.registry.activity(self.project)), 4)

    def test_stale_preview_and_cli_booking(self) -> None:
        import io
        import json
        import os
        from contextlib import redirect_stdout
        from unittest.mock import patch
        from cli_api_fake import client_patch
        from t6.cli import main
        from t6.verbs import Preview, Refused
        values = {'bay': self.bays[0], 'person': 'editor', 'project': self.project, 'start': '2026-11-20T10:00:00+08:00', 'end': '2026-11-20T11:00:00+08:00', 'idempotency_key': 'stale'}
        preview = self.call('bay', 'book', values)
        self.assertIsInstance(preview, Preview)
        self.write('bay', 'book', {**values, 'idempotency_key': 'other'})
        self.assertIsInstance(self.call('bay', 'book', {**values, 'confirm': preview.token}), Refused)
        out = io.StringIO()
        with patch.dict(os.environ, {'T6_PERSON_ID': 'writer', 'T6_PREVIEW_SIGNING_KEY': 'test'}), patch('t6.cli._open_registry', return_value=self.registry), client_patch(lambda _: None), redirect_stdout(out):
            status = main(['bay', 'book', self.bays[1], 'editor', self.project, '--start', '2026-11-20T10:00:00+08:00', '--end', '2026-11-20T11:00:00+08:00'])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(out.getvalue())['outcome'], 'preview')


class BayCliTests(unittest.TestCase):
    def test_admin_migrate_and_commands_catalogue(self) -> None:
        import io
        import json
        import os
        import tempfile
        from contextlib import redirect_stdout, redirect_stderr
        from pathlib import Path
        from unittest.mock import patch
        from t6.cli import main
        registry = MemoryRegistry()
        self.addCleanup(registry.db.close)
        fake = FakeNotion()
        fake.add_snapshot_data_source('bays', 'bays')
        for _ in range(2):
            fake.add_page('bays', {BaysRow.notion_name('name'): title('Bay')})
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'sources.json'
            source.write_text(json.dumps({'bays': 'bays'}))
            def run(*args: str) -> Any:
                out = io.StringIO()
                with patch.dict(os.environ, {'T6_NOTION_TOKEN': 'fake'}, clear=True), patch('t6.registry.SqliteProjectRegistry', return_value=registry), redirect_stdout(out), redirect_stderr(io.StringIO()):
                    self.assertEqual(main(list(args), open_notion=lambda _: fake.client()), 0)
                return json.loads(out.getvalue())
            args = ('admin', 'bay', 'migrate', '--data-sources', str(source))
            preview = run(*args)
            path = Path(tmp) / 'preview.json'
            path.write_text(json.dumps(preview))
            first = run(*args, '--apply', '--preview', str(path))
            second = run(*args, '--apply', '--preview', str(path))
            self.assertEqual([e['t6_id'] for e in first['entries']], [e['t6_id'] for e in second['entries']])
            catalogue = run('commands', '--json')
            self.assertTrue({('bay', 'book'), ('booking', 'move'), ('booking', 'extend'), ('booking', 'cancel')} <= {(e['noun'], e['verb']) for e in catalogue})
