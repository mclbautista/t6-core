"""Bay calendar sharing mirrors the bookings permission (#271). Fake calendar, in-memory database, injected clock."""
import os
import unittest
from typing import Any
from unittest.mock import patch

from t6.google.calendar import CalendarError
from t6.google.sharing import RECONCILE_KIND, SHARING_KIND, SharingSync, short_hash
from t6.google.testing import FakeCalendar, FakeIdentities
from t6.server.testing import FakeClock
from t6.worker.internal_queue import InternalJobQueue, InternalQueueClient
from t6.worker.runner import Worker
from test_person import MemoryRegistry

EMAILS = {'ana': 'ana@example.invalid', 'ben': 'ben@example.invalid', 'cy': 'cy@example.invalid'}
SERVICE = 'robot@example-project.iam.example.invalid'
OWNER = 'owner@example.invalid'
DAY = 86400


class SharingCase(unittest.TestCase):
    def setUp(self) -> None:
        from t6.person import People
        from t6.preview import PreviewTokens
        env = patch.dict(os.environ, {'T6_PREVIEW_SIGNING_KEY': 'test'})
        env.start()
        self.addCleanup(env.stop)
        self.registry = MemoryRegistry()
        self.addCleanup(self.registry.db.close)
        self.people = People(self.registry, PreviewTokens('test'))
        with self.registry._transaction() as db:
            for person in ('op', 'ana', 'ben', 'cy'):
                db.execute("INSERT INTO persons VALUES (?, 'staff', '[]', '[]', NULL, 0, 1)", (person,))
            db.execute("INSERT INTO person_permissions VALUES ('op', 'operator')")
            db.execute("INSERT INTO person_permissions VALUES ('ana', 'bookings')")
        self.clock = FakeClock()
        self.registry._clock = self.clock
        self.calendar = FakeCalendar(clock=self.clock)
        self.calendars = {'bay-1': 'cal-one', 'bay-2': 'cal-two'}
        for calendar in self.calendars.values():  # what Google always lists: the service account and the calendar owner as managers
            self.calendar.acl[calendar] = {SERVICE: 'owner', OWNER: 'owner'}
        self.identities = FakeIdentities(dict(EMAILS))
        self.sharing = SharingSync(self.registry, self.calendar, self.calendars, self.identities, service_account=SERVICE, clock=self.clock)
        self.queue = InternalJobQueue(self.registry, clock=self.clock)
        self.worker = Worker(InternalQueueClient(self.queue), 'main', {SHARING_KIND: self.sharing.handle_change, RECONCILE_KIND: self.sharing.handle_reconcile}, internal_client=None)

    def drain(self) -> None:
        while self.worker.run_once():
            pass

    def roles(self, calendar: str = 'cal-one') -> dict[str, str]:
        return dict(self.calendar.acl[calendar])

    def permission(self, verb: str, person: str, permission: str = 'bookings') -> Any:
        from t6.verbs import VERBS, Done, Preview, invoke
        entry = next(e for e in VERBS if (e.noun, e.verb) == ('person', verb))
        def call(data: dict[str, Any]) -> Any:
            return invoke(entry, entry.input_model.model_validate(data), actor='op', people=self.people, open_notion=lambda _: None)
        values = {'person_id': person, 'permission': permission}
        preview = call(values)
        assert isinstance(preview, Preview), preview
        done = call({**values, 'idempotency_key': preview.plan['idempotency_key'], 'confirm': preview.token})
        assert isinstance(done, Done), done
        return done

    def jobs(self, kind: str) -> int:
        with self.registry._transaction() as db:
            return int(db.execute('SELECT COUNT(*) FROM internal_jobs WHERE kind = ?', (kind,)).fetchone()[0])


class GrantAndRevokeTests(SharingCase):
    def test_grant_queues_one_job_in_the_grant_transaction_and_gives_edit_on_every_bay_calendar(self) -> None:
        self.permission('grant', 'ben')
        self.assertEqual(self.jobs(SHARING_KIND), 1)
        self.assertNotIn('ben@example.invalid', self.roles())  # nothing is written until the worker runs the job
        self.drain()
        for calendar in ('cal-one', 'cal-two'):
            self.assertEqual(self.roles(calendar)['ben@example.invalid'], 'writer')

    def test_revoke_removes_edit_and_keeps_view(self) -> None:
        self.permission('grant', 'ben')
        self.drain()
        self.permission('revoke', 'ben')
        self.drain()
        for calendar in ('cal-one', 'cal-two'):
            self.assertEqual(self.roles(calendar)['ben@example.invalid'], 'reader')

    def test_other_permissions_and_refused_grants_queue_nothing(self) -> None:
        self.permission('grant', 'ben', 'drives')
        self.assertEqual(self.jobs(SHARING_KIND), 0)
        from t6.verbs import VERBS, Refused, invoke
        entry = next(e for e in VERBS if (e.noun, e.verb) == ('person', 'grant'))
        outcome = invoke(entry, entry.input_model.model_validate({'person_id': 'ben', 'permission': 'bookings'}), actor='cy', people=self.people, open_notion=lambda _: None)
        self.assertIsInstance(outcome, Refused)
        self.assertEqual(self.jobs(SHARING_KIND), 0)

    def test_the_job_gives_view_to_a_staff_person_with_no_entry_and_touches_only_that_person(self) -> None:
        self.calendar.acl['cal-one']['ana@example.invalid'] = 'reader'  # drift of another Person: only the reconcile job corrects it
        self.permission('grant', 'ben')
        self.drain()
        self.assertEqual(self.roles()['ana@example.invalid'], 'reader')

    def test_a_person_without_an_email_is_skipped_and_the_job_says_so(self) -> None:
        self.identities.emails.pop('ben')
        self.permission('grant', 'ben')
        self.drain()
        self.assertEqual(self.roles(), {SERVICE: 'owner', OWNER: 'owner'})
        with self.registry._transaction() as db:
            result = db.execute("SELECT result FROM internal_jobs WHERE kind = ?", (SHARING_KIND,)).fetchone()[0]
        self.assertIn('no_email', result)

    def test_an_email_lookup_failure_makes_the_job_retry(self) -> None:
        self.identities.fail_next(1)
        self.permission('grant', 'ben')
        self.drain()
        self.assertNotIn('ben@example.invalid', self.roles())
        self.clock.now += 3600
        self.drain()
        self.assertEqual(self.roles()['ben@example.invalid'], 'writer')


class ReconcileTests(SharingCase):
    def reconcile(self) -> Any:
        return self.sharing.handle_reconcile(_job())

    def test_drift_is_corrected_t6_wins_and_each_fix_is_logged_without_emails(self) -> None:
        self.calendar.acl['cal-one'].update({'ana@example.invalid': 'reader', 'ben@example.invalid': 'writer'})  # holder lost edit; a non-holder gained it
        # cal-two: nobody listed, so every staff Person is missing
        with self.assertLogs('t6.google.sharing', 'INFO') as logs:
            result = self.reconcile()
        self.assertEqual(self.roles('cal-one'), {SERVICE: 'owner', OWNER: 'owner', 'ana@example.invalid': 'writer', 'ben@example.invalid': 'reader', 'cy@example.invalid': 'reader'})
        self.assertEqual(self.roles('cal-two'), {SERVICE: 'owner', OWNER: 'owner', 'ana@example.invalid': 'writer', 'ben@example.invalid': 'reader', 'cy@example.invalid': 'reader'})
        self.assertEqual(result['fixed_count'], 6)
        self.assertEqual(len(logs.records), 6)
        text = '\n'.join(r.getMessage() for r in logs.records) + str(result)
        self.assertIn('reader->writer', text)
        self.assertIn('writer->reader', text)
        self.assertIn('none->reader', text)
        self.assertNotIn('example.invalid', text)
        self.assertIn(short_hash('ana@example.invalid'), text)

    def test_a_correct_acl_is_left_alone(self) -> None:
        self.reconcile()
        before = self.roles('cal-one')
        self.calendar.calls.clear()
        with self.assertNoLogs('t6.google.sharing', 'INFO'):
            result = self.reconcile()
        self.assertEqual(result['fixed_count'], 0)
        self.assertEqual(self.roles('cal-one'), before)

    def test_the_service_account_and_the_calendar_owner_are_never_changed(self) -> None:
        self.identities.emails['cy'] = OWNER  # the owner is also a Person without the permission
        self.calendar.acl['cal-one'][SERVICE] = 'owner'
        self.reconcile()
        self.assertEqual(self.roles('cal-one')[OWNER], 'owner')
        self.assertEqual(self.roles('cal-one')[SERVICE], 'owner')
        self.identities.emails['cy'] = SERVICE.upper()  # even if the service account were mapped to a Person, in another letter case
        self.reconcile()
        self.assertEqual(self.roles('cal-two')[SERVICE], 'owner')
        self.assertNotIn(SERVICE.upper(), self.roles('cal-two'))

    def test_a_writer_added_by_hand_who_is_no_person_is_lowered_to_reader_and_logged_by_hash(self) -> None:
        self.calendar.acl['cal-one']['stranger@example.invalid'] = 'writer'
        self.calendar.acl['cal-one']['viewer@example.invalid'] = 'reader'
        with self.assertLogs('t6.google.sharing', level='INFO') as logs:
            result = self.reconcile()
        self.assertEqual(self.roles('cal-one')['stranger@example.invalid'], 'reader')
        self.assertEqual(self.roles('cal-one')['viewer@example.invalid'], 'reader')
        self.assertEqual(result['unmanaged'], 1)  # the reader is left alone
        self.assertIn({'calendar': short_hash('cal-one'), 'identity': short_hash('stranger@example.invalid'), 'change': 'writer->reader'}, result['fixed'])
        text = '\n'.join(logs.output) + str(result)
        self.assertNotIn('stranger@example.invalid', text)
        self.assertIn(short_hash('stranger@example.invalid'), text)

    def test_owners_and_the_service_account_are_never_lowered(self) -> None:
        self.reconcile()
        self.assertEqual(self.roles('cal-one')[OWNER], 'owner')
        self.assertEqual(self.roles('cal-one')[SERVICE], 'owner')

    def test_an_inactive_person_loses_edit_and_gets_no_new_entry(self) -> None:
        with self.registry._transaction() as db:
            db.execute("UPDATE persons SET active = 0 WHERE id IN ('ana', 'ben')")
        self.calendar.acl['cal-one']['ana@example.invalid'] = 'writer'
        self.reconcile()
        self.assertEqual(self.roles('cal-one')['ana@example.invalid'], 'reader')
        self.assertNotIn('ben@example.invalid', self.roles('cal-one'))

    def test_a_person_without_an_email_is_counted(self) -> None:
        before = self.reconcile()['no_email']  # the operator has no email on file
        self.identities.emails.pop('cy')
        self.assertEqual(self.reconcile()['no_email'], before + 1)

    def test_it_runs_once_a_day_and_each_run_queues_the_next(self) -> None:
        first, second = self.sharing.schedule(self.queue), self.sharing.schedule(self.queue)
        self.assertEqual(first.job_id, second.job_id)
        self.drain()
        self.assertEqual(self.jobs(RECONCILE_KIND), 2)
        self.assertEqual(self.roles('cal-one')['ana@example.invalid'], 'writer')
        self.clock.now += DAY
        self.drain()
        self.assertEqual(self.jobs(RECONCILE_KIND), 3)


class LookupFailureTests(SharingCase):
    def reconcile(self) -> Any:
        return self.sharing.handle_reconcile(_job())

    def test_one_failed_email_lookup_is_counted_and_skipped_and_the_run_goes_on(self) -> None:
        self.calendar.acl['cal-one']['ana@example.invalid'] = 'writer'
        self.identities.fail_next(1)  # the first Person read is ana
        result = self.reconcile()
        self.assertEqual(result['lookup_failed'], 1)
        for calendar in ('cal-one', 'cal-two'):
            self.assertEqual(self.roles(calendar).get('ben@example.invalid'), 'reader')
            self.assertEqual(self.roles(calendar).get('cy@example.invalid'), 'reader')
        self.assertEqual(self.roles('cal-one')['ana@example.invalid'], 'writer')  # unknown this run: left as it was, not lowered as a stranger

    def test_a_run_with_a_failed_lookup_lowers_no_unknown_writer(self) -> None:
        self.calendar.acl['cal-one']['stranger@example.invalid'] = 'writer'
        self.identities.fail_next(1)
        result = self.reconcile()
        self.assertEqual(self.roles('cal-one')['stranger@example.invalid'], 'writer')
        self.assertEqual(result['unmanaged'], 1)
        self.assertEqual(self.reconcile()['lookup_failed'], 0)  # the next run has all emails and lowers it
        self.assertEqual(self.roles('cal-one')['stranger@example.invalid'], 'reader')

    def test_emails_are_read_only_for_active_staff(self) -> None:
        with self.registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES ('cli', 'client', '[]', '[]', NULL, 0, 1)")
            db.execute("INSERT INTO persons VALUES ('gone', 'staff', '[]', '[]', NULL, 0, 0)")
        self.reconcile()
        self.assertEqual(sorted(set(self.identities.lookups)), ['ana', 'ben', 'cy', 'op'])
        self.assertEqual(len(self.identities.lookups), 4)  # once each, not once per calendar

    def test_a_client_or_inactive_person_with_edit_is_still_lowered(self) -> None:
        with self.registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES ('cli', 'client', '[]', '[]', NULL, 0, 1)")
        self.identities.emails['cli'] = 'cli@example.invalid'
        self.calendar.acl['cal-one']['cli@example.invalid'] = 'writer'
        self.reconcile()
        self.assertEqual(self.roles('cal-one')['cli@example.invalid'], 'reader')

    def test_a_grant_job_whose_lookup_fails_still_retries(self) -> None:
        self.identities.fail_next(1)
        self.permission('grant', 'ben')
        self.drain()
        self.assertNotIn('ben@example.invalid', self.roles())
        self.assertEqual(self.jobs(SHARING_KIND), 1)  # the one Person's job is not dropped: it retries


class PersonChangesQueueSharingTests(SharingCase):
    def person_verb(self, verb: str, values: dict[str, Any]) -> Any:
        from t6.verbs import VERBS, Done, Preview, invoke
        entry = next(e for e in VERBS if (e.noun, e.verb) == ('person', verb))
        def call(data: dict[str, Any]) -> Any:
            return invoke(entry, entry.input_model.model_validate(data), actor='op', people=self.people, open_notion=lambda _: None)
        preview = call(values)
        assert isinstance(preview, Preview), preview
        done = call({**values, 'idempotency_key': preview.plan['idempotency_key'], 'confirm': preview.token})
        assert isinstance(done, Done), done
        return done

    def test_reclassifying_a_person_queues_the_sharing_job_and_the_worker_lowers_edit(self) -> None:
        self.calendar.acl['cal-one']['ana@example.invalid'] = 'writer'
        self.person_verb('classify', {'person_id': 'ana', 'kind': 'vendor'})
        self.assertEqual(self.jobs(SHARING_KIND), 1)
        self.drain()
        self.assertEqual(self.roles('cal-one')['ana@example.invalid'], 'reader')

    def test_rebinding_a_person_queues_the_sharing_job(self) -> None:
        self.person_verb('rebind', {'person_id': 'ana', 'page_id': 'a' * 32})
        self.assertEqual(self.jobs(SHARING_KIND), 1)

    def test_a_refused_classify_queues_nothing(self) -> None:
        from t6.verbs import VERBS, Refused, invoke
        entry = next(e for e in VERBS if (e.noun, e.verb) == ('person', 'classify'))
        outcome = invoke(entry, entry.input_model.model_validate({'person_id': 'ana', 'kind': 'robot'}), actor='op', people=self.people, open_notion=lambda _: None)
        self.assertIsInstance(outcome, Refused)
        self.assertEqual(self.jobs(SHARING_KIND), 0)


class NonUserWriterTests(SharingCase):
    def reconcile(self) -> Any:
        return self.sharing.handle_reconcile(_job())

    def test_a_writer_group_domain_or_public_rule_is_lowered_like_a_hand_added_writer_and_logged_by_hash(self) -> None:
        self.calendar.acl['cal-one'].update({'group:staff@example.invalid': 'writer', 'domain:example.invalid': 'writer', 'default': 'writer'})
        with self.assertLogs('t6.google.sharing', 'INFO') as logs:
            result = self.reconcile()
        acl = self.roles('cal-one')
        self.assertEqual((acl['group:staff@example.invalid'], acl['domain:example.invalid'], acl['default']), ('reader', 'reader', 'reader'))
        self.assertEqual(result['unmanaged'], 0)
        text = '\n'.join(logs.output) + str(result)
        self.assertNotIn('staff@example.invalid', text)
        self.assertIn(short_hash('group:staff@example.invalid'), text)
        self.assertEqual(len([f for f in result['fixed'] if f['change'] == 'writer->reader']), 3)

    def test_a_non_writer_group_is_left_and_counted_and_owners_are_never_touched(self) -> None:
        self.calendar.acl['cal-one'].update({'group:readers@example.invalid': 'reader', 'domain:example.invalid': 'owner'})
        result = self.reconcile()
        self.assertEqual(self.roles('cal-one')['group:readers@example.invalid'], 'reader')
        self.assertEqual(self.roles('cal-one')['domain:example.invalid'], 'owner')
        self.assertEqual(result['unmanaged'], 1)
        self.assertEqual(self.roles('cal-one')[OWNER], 'owner')
        self.assertEqual(self.roles('cal-one')[SERVICE], 'owner')


class NoCalendarTests(unittest.TestCase):
    def test_both_jobs_finish_skipped_when_no_calendar_is_configured(self) -> None:
        from t6.google.sharing import sharing_handlers
        handlers = sharing_handlers({}, lambda: MemoryRegistry(), lambda registry: None)
        self.assertEqual(set(handlers), {SHARING_KIND, RECONCILE_KIND})
        for handler in handlers.values():
            result = handler(_job())
            assert isinstance(result, dict)
            self.assertEqual(result['event'], 'skipped')
            self.assertIn('no calendar configured', str(result['reason']))

    def test_the_worker_registers_the_sharing_jobs_with_a_database(self) -> None:
        from t6.worker.__main__ import sharing_calendar_handlers
        self.assertEqual(set(sharing_calendar_handlers({})), {SHARING_KIND, RECONCILE_KIND})


def _job() -> Any:
    from t6.worker.client import WorkerJob
    return WorkerJob(job_id=1, queue='main', kind='x', dedupe_key='k', payload={}, token='t', attempt=1)


if __name__ == '__main__':
    unittest.main()
