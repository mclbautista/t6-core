"""Outbound Booking sync to one Google Calendar per Bay, with a fake calendar (#260)."""
import ast
import os
import re
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from t6.bay import BayStore
from t6.google.sync import BOOKING_SYNC_KIND, CalendarSync
from t6.google.testing import FakeCalendar
from t6.notion.generated import BaysRow
from t6.notion.testing import FakeNotion, title
from t6.server.testing import FakeClock
from t6.worker.internal_queue import InternalJobQueue, InternalQueueClient
from t6.worker.runner import Worker
from test_person import MemoryRegistry


class BookingCalendarSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        from t6.ids import EntityKind, T6Id
        from t6.person import People
        from t6.preview import PreviewTokens
        from t6.registry import Registered
        env = patch.dict(os.environ, {'T6_PREVIEW_SIGNING_KEY': 'test'})
        env.start()
        self.addCleanup(env.stop)
        self.registry = MemoryRegistry()
        self.addCleanup(self.registry.db.close)
        self.people = People(self.registry, PreviewTokens('test'))
        with self.registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES ('writer', 'staff', '[]', '[]', NULL, 0, 1)")
            db.execute("INSERT INTO person_permissions VALUES ('writer', 'bookings')")
        self.registry._new_id = lambda: str(T6Id.mint(EntityKind.PROJECT))
        project = self.registry.register(actor='setup', idempotency_key='project')
        assert isinstance(project, Registered)
        self.project = project.project.id
        fake = FakeNotion()
        fake.add_snapshot_data_source('bays', 'bays')
        rows = [BaysRow.from_page(fake.add_page('bays', {BaysRow.notion_name('name'): title('Bay')})) for _ in range(3)]
        store = BayStore(self.registry)
        preview = store.migrate(rows, actor='setup')
        self.bays = [x['t6_id'] for x in store.migrate(rows, actor='setup', apply=True, digest=preview['digest'])['entries']]
        self.calendars = {self.bays[0]: 'cal-one', self.bays[1]: 'cal-two'}
        self.clock = FakeClock()
        self.registry._clock = self.clock
        self.calendar = FakeCalendar()
        self.queue = InternalJobQueue(self.registry, clock=self.clock)
        self.sync = CalendarSync(self.registry, self.calendar, self.calendars)
        self.worker = Worker(InternalQueueClient(self.queue), 'main', {BOOKING_SYNC_KIND: self.sync.handle}, internal_client=None)

    def write(self, noun: str, verb: str, values: dict[str, Any]) -> Any:
        from t6.verbs import VERBS, Done, Preview, invoke
        entry = next(e for e in VERBS if (e.noun, e.verb) == (noun, verb))
        def call(data: dict[str, Any]) -> Any:
            return invoke(entry, entry.input_model.model_validate(data), actor='writer', people=self.people, open_notion=lambda _: None)
        preview = call(values)
        assert isinstance(preview, Preview)
        result = call({**values, 'idempotency_key': preview.plan['idempotency_key'], 'confirm': preview.token})
        assert isinstance(result, Done)
        return result

    def drain(self) -> None:
        while self.worker.run_once():
            pass

    def book(self) -> str:
        done = self.write('bay', 'book', {'bay': self.bays[0], 'person': 'writer', 'project': self.project, 'start': '2026-11-20T10:00:00+08:00', 'end': '2026-11-20T12:00:00+08:00'})
        return str(done.result['booking_id'])

    def test_create_move_extend_cancel_update_the_event_once_each(self) -> None:
        booking = self.book()
        self.drain()
        self.assertEqual(self.calendar.calls, [('insert', 'cal-one')])
        self.assertEqual(len(self.calendar.events), 1)
        self.write('booking', 'move', {'booking': booking, 'bay': self.bays[1], 'start': '2026-11-21T10:00:00+08:00', 'end': '2026-11-21T12:00:00+08:00'})
        self.drain()
        self.assertEqual(self.calendar.calls[1:], [('delete', 'cal-one'), ('insert', 'cal-two')])
        self.assertEqual(list(self.calendar.events), [('cal-two', next(iter(self.calendar.events))[1])])
        self.write('booking', 'extend', {'booking': booking, 'end': '2026-11-21T14:00:00+08:00'})
        self.drain()
        self.assertEqual(self.calendar.calls[3:], [('update', 'cal-two')])
        (event,) = self.calendar.events.values()
        self.assertEqual(event['end'], '2026-11-21T14:00:00.000000+08:00')
        self.write('booking', 'cancel', {'booking': booking})
        self.drain()
        self.assertEqual(self.calendar.calls[4:], [('delete', 'cal-two')])
        self.assertEqual(self.calendar.events, {})

    def test_no_job_before_confirmation_and_one_job_per_change(self) -> None:
        from t6.verbs import VERBS, invoke
        entry = next(e for e in VERBS if (e.noun, e.verb) == ('bay', 'book'))
        values = {'bay': self.bays[0], 'person': 'writer', 'project': self.project, 'start': '2026-11-20T10:00:00+08:00', 'end': '2026-11-20T12:00:00+08:00'}
        invoke(entry, entry.input_model.model_validate(values), actor='writer', people=self.people, open_notion=lambda _: None)
        self.assertFalse(self.worker.run_once())
        self.book()
        self.assertTrue(self.worker.run_once())
        self.assertFalse(self.worker.run_once())

    def test_failed_write_is_retried_and_lands_once(self) -> None:
        self.calendar.fail_next(1)
        self.book()
        self.worker.run_once()
        self.assertEqual(self.calendar.events, {})
        self.assertFalse(self.worker.run_once())
        self.clock.advance(31)
        self.drain()
        self.assertEqual(len(self.calendar.events), 1)

    def test_a_write_that_landed_but_lost_its_answer_does_not_duplicate(self) -> None:
        self.calendar.fail_next(1, after_applying=True)
        self.book()
        self.worker.run_once()
        self.assertEqual(len(self.calendar.events), 1)
        self.clock.advance(31)
        self.drain()
        self.assertEqual(len(self.calendar.events), 1)

    def test_bay_without_a_calendar_fails_the_job_without_retry(self) -> None:
        done = self.write('bay', 'book', {'bay': self.bays[2], 'person': 'writer', 'project': self.project, 'start': '2026-11-20T10:00:00+08:00', 'end': '2026-11-20T12:00:00+08:00'})
        self.assertTrue(self.worker.run_once())
        self.clock.advance(3600)
        self.assertFalse(self.worker.run_once())
        self.assertEqual(self.calendar.events, {})
        self.assertTrue(done.result['booking_id'])

    def test_event_carries_ids_only(self) -> None:
        self.book()
        self.drain()
        (event,) = self.calendar.events.values()
        self.assertEqual(set(event), {'summary', 'description', 'start', 'end'})
        self.assertEqual(event['summary'], self.project)

    def test_fake_acl_list_and_set(self) -> None:
        self.assertEqual(self.calendar.list_acl('cal-one'), [])
        self.calendar.set_acl('cal-one', 'person@example.invalid', 'reader')
        self.calendar.set_acl('cal-one', 'person@example.invalid', 'writer')
        self.assertEqual(self.calendar.list_acl('cal-one'), [{'email': 'person@example.invalid', 'role': 'writer'}])


class GoogleCredentialTests(unittest.TestCase):
    def test_only_the_google_config_names_the_credential(self) -> None:
        root = Path(__file__).parents[1] / 'src' / 't6'
        holders = []
        for path in root.rglob('*.py'):
            tree = ast.parse(path.read_text())
            if any(isinstance(node, ast.Constant) and isinstance(node.value, str) and re.fullmatch(r'[A-Z0-9_]*GOOGLE[A-Z0-9_]*(CREDENTIALS|KEY|TOKEN|SECRET)[A-Z0-9_]*', node.value) for node in ast.walk(tree)):
                holders.append(path.relative_to(root).as_posix())
        self.assertEqual(holders, ['google/config.py'])

    def test_apps_naming_the_credential_fail_the_consumer_check(self) -> None:
        from test_consumer_check import run_check
        code, report = run_check({'app.py': "import os\nkey = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS')\n"})
        self.assertEqual((code, report['findings'][0]['rule']), (1, 'credential'))

    def test_the_gateway_and_apps_never_import_the_google_package(self) -> None:
        root = Path(__file__).parents[1] / 'src' / 't6'
        for path in root.rglob('*.py'):
            relative = path.relative_to(root).as_posix()
            if relative.startswith(('google/', 'worker/')) or relative in ('bay.py', 'registry/sqlite.py'):
                continue
            self.assertNotIn('t6.google', path.read_text(), relative)


if __name__ == '__main__':
    unittest.main()
