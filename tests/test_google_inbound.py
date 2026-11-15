"""Inbound Google Calendar edits run through the booking rules, and the channel renewal job (#270). Fake calendar, in-memory database, injected clock."""
import json
import os
import unittest
from typing import Any
from unittest.mock import patch

from t6.bay import BayStore
from t6.domain.events import JsonValue
from t6.google.calendar import CalendarError
from t6.google.inbound import CHANGED_KIND, RENEW_KIND, ChannelRenewal, InboundEdits
from t6.google.sync import BOOKING_SYNC_KIND, CalendarSync, event_id
from t6.google.testing import FakeCalendar, FakeIdentities, FakeNames
from t6.notion.generated import BaysRow
from t6.notion.testing import FakeNotion, title
from t6.server.testing import FakeClock
from t6.worker.internal_queue import InternalJobQueue, InternalQueueClient
from t6.worker.runner import Worker
from test_person import MemoryRegistry

WRITER, VIEWER = 'writer@example.invalid', 'viewer@example.invalid'
TOKEN = 'channel-secret-value'
ADDRESS = 'https://edge.example.invalid/events/google/calendar'
DAY = 86400


class InboundCase(unittest.TestCase):
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
            db.execute("INSERT INTO persons VALUES ('viewer', 'staff', '[]', '[]', NULL, 0, 1)")
            db.execute("INSERT INTO person_permissions VALUES ('viewer', 'drives')")
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
        self.calendars = {self.bays[0]: 'cal-one', self.bays[1]: 'cal-two'}
        self.clock = FakeClock()
        self.registry._clock = self.clock
        self.calendar = FakeCalendar(clock=self.clock)
        self.queue = InternalJobQueue(self.registry, clock=self.clock)
        names = FakeNames({self.project: 'Example Show'}, {'writer': 'Ana Example'})
        identities = FakeIdentities({'writer': WRITER, 'viewer': VIEWER})
        self.sync = CalendarSync(self.registry, self.calendar, self.calendars, names)
        self.inbound = InboundEdits(self.registry, self.calendar, self.calendars, names, identities, clock=self.clock)
        self.counter = 0
        self.renewal = ChannelRenewal(self.registry, self.calendar, self.calendars, address=ADDRESS, token=TOKEN, clock=self.clock, new_id=self.new_channel)
        self.worker = Worker(InternalQueueClient(self.queue), 'main', {BOOKING_SYNC_KIND: self.sync.handle, CHANGED_KIND: self.inbound.handle, RENEW_KIND: self.renewal.handle}, internal_client=None)
        self.message = 0
        self.renew()

    def new_channel(self) -> str:
        self.counter += 1
        return f'chan-{self.counter}'

    def renew(self) -> None:
        self.queue.submit('main', RENEW_KIND, f'renew:{self.clock.now}', {})
        self.drain()

    def drain(self) -> None:
        while self.worker.run_once():
            pass

    def notify(self, channel: str = 'chan-1', state: str = 'exists') -> None:
        """What the edge queues for a google_channel notification: the x-goog headers without the channel token."""
        self.message += 1
        headers: dict[str, JsonValue] = {'x-goog-channel-id': channel, 'x-goog-message-number': str(self.message), 'x-goog-resource-id': 'res-' + channel, 'x-goog-resource-state': state}
        self.queue.submit('main', CHANGED_KIND, f'edge:/events/google/calendar:{channel}:{self.message}', {'headers': headers})
        self.drain()

    def write(self, noun: str, verb: str, values: dict[str, Any]) -> Any:
        from t6.verbs import VERBS, Done, Preview, invoke
        entry = next(e for e in VERBS if (e.noun, e.verb) == (noun, verb))
        def call(data: dict[str, Any]) -> Any:
            return invoke(entry, entry.input_model.model_validate(data), actor='writer', people=self.people, open_notion=lambda _: None)
        preview = call(values)
        assert isinstance(preview, Preview)
        done = call({**values, 'idempotency_key': preview.plan['idempotency_key'], 'confirm': preview.token})
        assert isinstance(done, Done)
        return done

    def book(self, start: str = '10:00', end: str = '12:00', bay: int = 0) -> str:
        done = self.write('bay', 'book', {'bay': self.bays[bay], 'person': 'writer', 'project': self.project, 'start': f'2026-11-20T{start}:00+08:00', 'end': f'2026-11-20T{end}:00+08:00'})
        self.drain()
        return str(done.result['booking_id'])

    def booking(self, booking: str) -> dict[str, Any]:
        from t6.bay import BookingStore
        from t6.preview import PreviewTokens
        found = BookingStore(self.registry, PreviewTokens('test')).get(booking)
        assert found is not None
        return found

    def audit(self, action: str) -> list[tuple[str, str]]:
        with self.registry._transaction() as db:
            return [(r[0], r[1]) for r in db.execute('SELECT project_id, actor FROM audit WHERE action = ? ORDER BY id', (action,))]

    def event(self, booking: str, calendar: str = 'cal-one') -> dict[str, Any]:
        return self.calendar.events[(calendar, event_id(booking))]


class EditsBecomeVerbs(InboundCase):
    def test_an_extend_by_a_booker_is_booking_extend_with_that_person_as_actor(self) -> None:
        booking = self.book()
        self.notify()  # our own write comes back first
        self.calendar.external_edit('cal-one', event_id(booking), editor=WRITER, end='2026-11-20T14:00:00+08:00')
        self.notify()
        self.assertEqual(self.booking(booking)['end'], '2026-11-20T14:00:00.000000+08:00')
        self.assertEqual(self.audit('booking_extend'), [(booking, 'writer')])
        self.assertEqual(self.audit('booking_move'), [])

    def test_a_move_in_time_is_booking_move(self) -> None:
        booking = self.book()
        self.notify()
        self.calendar.external_edit('cal-one', event_id(booking), editor=WRITER, start='2026-11-21T09:00:00+08:00', end='2026-11-21T11:00:00+08:00')
        self.notify()
        self.assertEqual(self.audit('booking_move'), [(booking, 'writer')])
        self.assertEqual(self.booking(booking)['start'], '2026-11-21T09:00:00.000000+08:00')

    def test_a_shorter_event_is_booking_move_not_extend(self) -> None:
        booking = self.book()
        self.notify()
        self.calendar.external_edit('cal-one', event_id(booking), editor=WRITER, end='2026-11-20T11:00:00+08:00')
        self.notify()
        self.assertEqual(self.audit('booking_move'), [(booking, 'writer')])
        self.assertEqual(self.booking(booking)['end'], '2026-11-20T11:00:00.000000+08:00')

    def test_an_event_moved_to_another_bay_calendar_is_booking_move_to_that_bay(self) -> None:
        booking = self.book()
        self.notify()
        self.calendar.external_move('cal-one', 'cal-two', event_id(booking), editor=WRITER)
        self.notify('chan-1')
        self.notify('chan-2')
        self.assertEqual(self.booking(booking)['bay_id'], self.bays[1])
        self.assertEqual(self.audit('booking_move'), [(booking, 'writer')])
        self.assertEqual(list(self.calendar.events), [('cal-two', event_id(booking))])

    def test_deleting_the_event_is_booking_cancel(self) -> None:
        booking = self.book()
        self.notify()
        self.calendar.external_delete('cal-one', event_id(booking), editor=WRITER)
        self.notify()
        self.assertTrue(self.booking(booking)['cancelled'])
        self.assertEqual(self.audit('booking_cancel'), [(booking, 'writer')])
        self.assertEqual(self.calendar.events, {})

    def test_an_edit_is_applied_once_per_event_version(self) -> None:
        booking = self.book()
        self.notify()
        self.calendar.external_edit('cal-one', event_id(booking), editor=WRITER, end='2026-11-20T14:00:00+08:00')
        self.notify()
        self.notify()  # a second notification for the same change
        with self.registry._transaction() as db:
            db.execute("UPDATE calendar_sync_tokens SET sync_token = '0'")  # a lost acknowledgement: the same change is listed again
        self.notify()
        self.assertEqual(self.audit('booking_extend'), [(booking, 'writer')])

    def test_an_event_t6_did_not_write_is_left_alone(self) -> None:
        self.calendar.events[('cal-one', 'someoneelse')] = {'summary': 'Hand made', 'start': '2026-11-20T10:00:00+08:00', 'end': '2026-11-20T12:00:00+08:00'}
        self.calendar.touch('cal-one', 'someoneelse', editor=WRITER)
        self.notify()
        self.assertEqual(list(self.calendar.events), [('cal-one', 'someoneelse')])
        self.assertEqual(self.audit('booking_move'), [])


class RejectedEditsAreReverted(InboundCase):
    def assert_reverted(self, booking: str, reason: str) -> None:
        body = self.event(booking)
        self.assertEqual((body['start'], body['end']), ('2026-11-20T10:00:00.000000+08:00', '2026-11-20T12:00:00.000000+08:00'))
        self.assertIn(f'Booking ID: {booking}', body['description'])
        self.assertIn('Note: change reverted', body['description'])
        self.assertIn(reason, body['description'])
        current = self.booking(booking)
        self.assertEqual((current['start'], current['end'], current['cancelled']), ('2026-11-20T10:00:00.000000+08:00', '2026-11-20T12:00:00.000000+08:00', False))
        for action in ('booking_move', 'booking_extend', 'booking_cancel'):
            self.assertEqual(self.audit(action), [])

    def test_a_clash_is_reverted_with_a_note_and_the_booking_does_not_change(self) -> None:
        booking = self.book()
        self.book(start='12:00', end='14:00')
        self.notify()
        self.calendar.external_edit('cal-one', event_id(booking), editor=WRITER, end='2026-11-20T13:00:00+08:00')
        self.notify()
        self.assert_reverted(booking, 'overlapping Booking')

    def test_an_editor_without_the_bookings_permission_is_reverted(self) -> None:
        booking = self.book()
        self.notify()
        self.calendar.external_edit('cal-one', event_id(booking), editor=VIEWER, end='2026-11-20T14:00:00+08:00')
        self.notify()
        self.assert_reverted(booking, 'bookings permission')

    def test_an_unknown_editor_has_no_permission(self) -> None:
        booking = self.book()
        self.notify()
        for editor in ('stranger@example.invalid', None):
            self.calendar.external_edit('cal-one', event_id(booking), editor=editor, end='2026-11-20T14:00:00+08:00')
            self.notify()
            self.assert_reverted(booking, 'bookings permission')

    def test_a_deletion_by_someone_without_the_permission_puts_the_event_back(self) -> None:
        booking = self.book()
        self.notify()
        self.calendar.external_delete('cal-one', event_id(booking), editor=VIEWER)
        self.notify()
        self.assertIn(('cal-one', event_id(booking)), self.calendar.events)
        self.assert_reverted(booking, 'bookings permission')

    def test_an_event_without_a_timed_range_is_reverted(self) -> None:
        booking = self.book()
        self.notify()
        self.calendar.external_edit('cal-one', event_id(booking), editor=WRITER, start='2026-11-20', end='2026-11-21')
        self.notify()
        self.assert_reverted(booking, 'start and end')

    def test_the_revert_does_not_loop(self) -> None:
        booking = self.book()
        self.notify()
        self.calendar.external_edit('cal-one', event_id(booking), editor=VIEWER, end='2026-11-20T14:00:00+08:00')
        self.notify()
        writes = list(self.calendar.calls)
        self.notify()  # the revert's own notification
        self.assertEqual(self.calendar.calls, writes)


class OurOwnWritesDoNotLoop(InboundCase):
    def test_the_echo_of_our_own_write_changes_nothing(self) -> None:
        booking = self.book()
        self.write('booking', 'extend', {'booking': booking, 'end': '2026-11-20T14:00:00+08:00'})
        self.drain()
        writes, audits = list(self.calendar.calls), len(self.audit('booking_extend'))
        self.notify()
        self.notify()
        self.assertEqual(self.calendar.calls, writes)
        self.assertEqual(len(self.audit('booking_extend')), audits)
        self.assertEqual(self.audit('booking_move'), [])

    def test_an_inbound_edit_does_not_start_a_ping_pong(self) -> None:
        booking = self.book()
        self.notify()
        self.calendar.external_edit('cal-one', event_id(booking), editor=WRITER, end='2026-11-20T14:00:00+08:00')
        self.notify()  # applied, and the sync job writes the event back
        writes = len(self.calendar.calls)
        self.notify()  # the notification for that write
        self.notify()
        self.assertEqual(len(self.calendar.calls), writes)
        self.assertEqual(len(self.audit('booking_extend')), 1)

    def test_an_invalid_sync_token_falls_back_to_a_full_list(self) -> None:
        booking = self.book()
        self.notify()
        self.calendar.expire_sync_tokens()
        self.calendar.external_edit('cal-one', event_id(booking), editor=WRITER, end='2026-11-20T14:00:00+08:00')
        self.notify()
        self.assertEqual(self.audit('booking_extend'), [(booking, 'writer')])


class NotificationsAreChecked(InboundCase):
    def handle(self, headers: dict[str, JsonValue]) -> Any:
        from t6.worker.client import WorkerJob
        return self.inbound.handle(WorkerJob(1, 'main', CHANGED_KIND, 'k', {'headers': headers}, 't', 1))

    def test_an_unknown_channel_is_ignored(self) -> None:
        result = self.handle({'x-goog-channel-id': 'not-ours', 'x-goog-message-number': '1', 'x-goog-resource-state': 'exists'})
        self.assertEqual(result['ignored'], 'unknown channel')

    def test_the_first_sync_message_is_ignored(self) -> None:
        result = self.handle({'x-goog-channel-id': 'chan-1', 'x-goog-message-number': '1', 'x-goog-resource-state': 'sync'})
        self.assertEqual(result['ignored'], 'sync message')

    def test_a_channel_that_has_expired_is_ignored(self) -> None:
        self.clock.advance(8 * DAY)
        result = self.handle({'x-goog-channel-id': 'chan-1', 'x-goog-message-number': '1', 'x-goog-resource-state': 'exists'})
        self.assertEqual(result['ignored'], 'unknown channel')

    def test_a_calendar_that_cannot_be_read_retries_the_job(self) -> None:
        self.calendar.fail_reads(1)
        with self.assertRaises(CalendarError):
            self.handle({'x-goog-channel-id': 'chan-1', 'x-goog-message-number': '1', 'x-goog-resource-state': 'exists'})

    def test_an_identity_lookup_that_fails_retries_the_job(self) -> None:
        booking = self.book()
        self.notify()
        self.calendar.external_edit('cal-one', event_id(booking), editor=WRITER, end='2026-11-20T14:00:00+08:00')
        self.inbound.identities.fail_next(1)  # type: ignore[attr-defined]
        with self.assertRaises(CalendarError):
            self.handle({'x-goog-channel-id': 'chan-1', 'x-goog-message-number': '9', 'x-goog-resource-state': 'exists'})
        self.assertEqual(self.audit('booking_extend'), [])


class ChannelRenewalTests(InboundCase):
    def channels(self) -> list[tuple[str, str, str, float, int]]:
        with self.registry._transaction() as db:
            return [tuple(r) for r in db.execute('SELECT channel_id, calendar, resource_id, expires_at, active FROM calendar_channels ORDER BY channel_id')]

    def test_every_bay_calendar_gets_a_channel_with_the_token_and_the_address(self) -> None:
        self.assertEqual([c[:2] for c in self.channels()], [('chan-1', 'cal-one'), ('chan-2', 'cal-two')])
        for channel in self.calendar.channels.values():
            self.assertEqual((channel['address'], channel['token']), (ADDRESS, TOKEN))
        self.assertTrue(all(c[3] > self.clock.now and c[4] == 1 for c in self.channels()))

    def test_a_channel_far_from_expiry_is_not_renewed(self) -> None:
        self.clock.advance(DAY)
        self.renew()
        self.assertEqual(len(self.channels()), 2)

    def test_a_channel_close_to_expiry_is_replaced_and_the_old_one_stopped(self) -> None:
        self.clock.advance(5 * DAY)
        self.renew()
        rows = self.channels()
        self.assertEqual([(c[0], c[4]) for c in rows], [('chan-1', 0), ('chan-2', 0), ('chan-3', 1), ('chan-4', 1)])
        self.assertEqual(sorted(self.calendar.channels), ['chan-3', 'chan-4'])
        self.assertEqual(self.calendar.stopped, [('chan-1', 'res-chan-1'), ('chan-2', 'res-chan-2')])

    def test_a_notification_on_the_new_channel_works_and_edits_are_not_applied_twice(self) -> None:
        booking = self.book()
        self.notify()
        self.clock.advance(5 * DAY)
        self.renew()
        self.calendar.external_edit('cal-one', event_id(booking), editor=WRITER, end='2026-11-20T14:00:00+08:00')
        self.notify('chan-3')
        self.notify('chan-1')  # the old channel overlaps for a short time
        self.assertEqual(self.audit('booking_extend'), [(booking, 'writer')])

    def test_a_rotated_token_renews_at_once(self) -> None:
        self.renewal.token = 'another-secret-value'
        self.clock.advance(60)
        self.renew()
        self.assertEqual(sorted(self.calendar.channels), ['chan-3', 'chan-4'])
        self.assertEqual({c['token'] for c in self.calendar.channels.values()}, {'another-secret-value'})

    def test_a_failed_watch_retries_the_job_and_keeps_the_old_channel(self) -> None:
        self.clock.advance(5 * DAY)
        self.calendar.fail_watch(1)
        self.queue.submit('main', RENEW_KIND, 'renew:retry', {})
        self.worker.run_once()
        self.assertEqual(sorted(self.calendar.channels), ['chan-1', 'chan-2'])
        self.clock.advance(31)
        self.drain()
        self.assertEqual(len(self.calendar.channels), 2)
        self.assertFalse({'chan-1', 'chan-2'} & set(self.calendar.channels))

    def test_the_secret_is_neither_stored_nor_printed(self) -> None:
        from t6.worker.client import WorkerJob
        result = self.renewal.handle(WorkerJob(1, 'main', RENEW_KIND, 'k', {}, 't', 1))
        self.assertNotIn(TOKEN, json.dumps(result))
        with self.registry._transaction() as db:
            dump = json.dumps([list(r) for r in db.execute('SELECT * FROM calendar_channels')] + [list(r) for r in db.execute('SELECT result FROM internal_jobs')])
        self.assertNotIn(TOKEN, dump)

    def test_a_baseline_sync_token_is_stored_so_old_events_are_not_replayed(self) -> None:
        with self.registry._transaction() as db:
            rows = db.execute('SELECT calendar FROM calendar_sync_tokens ORDER BY calendar').fetchall()
        self.assertEqual(rows, [('cal-one',), ('cal-two',)])

    def test_renewal_queues_the_next_run_once_a_day(self) -> None:
        first, second = self.renewal.schedule(self.queue), self.renewal.schedule(self.queue)
        self.assertEqual((first.job_id, second.created), (second.job_id, False))
        with self.registry._transaction() as db:
            runs = [r[0] for r in db.execute("SELECT run_at FROM internal_jobs WHERE kind = ? AND state = 'queued' ORDER BY run_at", (RENEW_KIND,))]
        self.assertEqual(runs, [self.clock.now, self.clock.now + DAY])

    def test_no_address_or_token_means_no_inbound(self) -> None:
        from t6.google.inbound import NoInbound
        from t6.worker.client import WorkerJob
        result: Any = NoInbound().handle(WorkerJob(1, 'main', CHANGED_KIND, 'k', {}, 't', 1))
        self.assertEqual(result['event'], 'skipped')


class ConfigTests(unittest.TestCase):
    def test_without_the_notify_settings_both_jobs_finish_and_say_so(self) -> None:
        from t6.google.config import NOTIFY_URL_VARIABLE, CHANNEL_TOKEN_VARIABLE
        from t6.google.inbound import inbound_handlers
        from t6.worker.client import WorkerJob
        handlers = inbound_handlers({}, lambda: MemoryRegistry(), lambda registry: None)
        self.assertEqual(set(handlers), {CHANGED_KIND, RENEW_KIND})
        for kind, handler in handlers.items():
            result: Any = handler(WorkerJob(1, 'main', kind, 'k', {}, 't', 1))
            self.assertEqual(result['event'], 'skipped')
            self.assertIn(NOTIFY_URL_VARIABLE, result['reason'])
            self.assertIn(CHANNEL_TOKEN_VARIABLE, result['reason'])

    def test_a_token_that_is_too_long_for_google_is_refused(self) -> None:
        from t6.google.config import channel_token
        with self.assertRaises(ValueError):
            channel_token({'T6_GOOGLE_CHANNEL_TOKEN': 'x' * 257})
        self.assertEqual(channel_token({'T6_GOOGLE_CHANNEL_TOKEN': ' abc '}), 'abc')


if __name__ == '__main__':
    unittest.main()
