"""The daily CRM DM to Lead Owners, with synthetic records and a recording gateway."""
import json
import threading
import unittest
from datetime import date, datetime
from contextlib import redirect_stderr
from io import StringIO
from typing import Any
from unittest.mock import patch

from t6.concern import _insert
from t6.crm.config import CrmRoles, crm_roles
from t6.crm.reminder import CrmReminder, LeadFacts, read_leads
from t6.domain import CrmStage, Project
from t6.domain.studio_time import MANILA
from t6.ids import EntityKind, T6Id
from t6.person import People
from t6.preview import PreviewTokens
from t6.registry import Registered
from t6.worker import Worker, WorkerJob
from t6.worker.runner import Refused
from t6.worker.gateway import GatewayClient
from t6.worker.internal_queue import InternalJobQueue, InternalQueueClient
from test_crm_log import FakeSheet, row
from test_person import MemoryRegistry

TODAY = date(2026, 12, 1)


def at(day: str, hour: int = 9) -> float:
    return datetime.fromisoformat(f'{day}T{hour:02d}:00:00').replace(tzinfo=MANILA).timestamp()


class Gateway:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def send(self, method: str, path: str, headers: object, body: bytes) -> tuple[int, dict[str, Any]]:
        self.calls.append(json.loads(body))
        return 202, {'message_ref': len(self.calls)}


class CrmReminderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = MemoryRegistry()
        self.addCleanup(self.registry.db.close)
        self.now = [at('2026-12-01', 10)]
        self.registry._clock = lambda: self.now[0]
        self.registry._new_id = lambda: str(T6Id.mint(EntityKind.PROJECT))
        for person, telegram in (('ada', 700000001), ('ben', 700000002), ('cyril', 700000003)):
            self.person(person, telegram)
        self.projects: dict[str, LeadFacts] = {}
        self.log: list[tuple[Any, ...]] = []
        self.names = {'ada': 'Ada', 'cyril': 'Cyril'}
        self.queue = InternalJobQueue(self.registry, clock=lambda: self.now[0])
        self.gateway = Gateway()
        self.reminder = CrmReminder(
            People(self.registry, PreviewTokens('test')), self.queue, GatewayClient(self.gateway, 'synthetic-secret'),
            self.sheet(), 'cyril', lambda: list(self.projects.values()), self.names.get, clock=lambda: self.now[0])
        self.worker = Worker(InternalQueueClient(self.queue), 'main', {'crm.reminder': self.reminder.handle})

    def sheet(self) -> Any:
        outer = self

        class Live(FakeSheet):
            def tabs(self) -> Any:
                return FakeSheet(outer.log).tabs()
        return Live([])

    def person(self, person: str, telegram: int | None, active: int = 1) -> None:
        with self.registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES (?, 'staff', '[]', '[]', ?, 0, ?)", (person, telegram, active))

    def lead(self, name: str, stage: CrmStage, registered: str, owners: tuple[str, ...] = ('ada',), *, changed: str | None = None,
             role: bool = True, studio: bool = True) -> str:
        self.now[0] = at(registered)
        found = self.registry.register(actor='setup', idempotency_key=name, notion_page_id=f'page-{name}')
        assert isinstance(found, Registered)
        project = found.project.id
        if changed:
            with self.registry._transaction() as db:
                db.execute("INSERT INTO project_stage_history (project_id, at, actor, field, from_value, to_value, event) VALUES (?, ?, 'ada', 'crm_stage', NULL, ?, ?)",
                           (project, at(changed), stage.value, f'e-{name}'))
        self.projects[name] = LeadFacts(project, name, stage, owners, role, studio)
        self.now[0] = at('2026-12-01', 10)
        return project

    def concern(self, project: str, text: str) -> None:
        with self.registry._transaction() as db:
            _insert(db, f'c-{text}', project, None, None, text, '2026-11-01', 1.0, 1.0, 'test', text)

    def run_day(self) -> None:
        self.reminder.schedule()
        self.assertTrue(self.worker.run_once())

    def sent(self) -> dict[int, str]:
        return {call['to']: call['text'] for call in self.gateway.calls}

    def test_one_dm_per_owner_lists_only_their_cold_leads(self) -> None:
        mine = self.lead('Alpha', CrmStage.NEGOTIATIONS, '2026-11-20')
        self.lead('Beta', CrmStage.LEADS, '2026-11-01', ('ben',))
        self.lead('Gamma', CrmStage.LEADS, '2026-11-01', ('ben',))
        self.lead('Fresh', CrmStage.ONBOARDING, '2026-11-30')
        self.concern(mine, 'Waiting on <client>')
        self.run_day()
        self.assertEqual(len(self.gateway.calls), 2)
        self.assertEqual(self.sent(), {
            700000001: 'Hi Ada,\nCold Leads\nAlpha — Negotiations, 11 days since last contact\n  Concern: Waiting on &lt;client&gt;',
            700000002: 'Hi,\nCold Leads\nBeta — Leads, 30 days since last contact\nGamma — Leads, 30 days since last contact'})
        self.assertEqual(sorted(call['idempotency_key'] for call in self.gateway.calls), ['crm-dm:ada:2026-12-01', 'crm-dm:ben:2026-12-01'])

    def test_thresholds_per_stage(self) -> None:
        for stage, days in ((CrmStage.LEADS, 28), (CrmStage.NEGOTIATIONS, 7), (CrmStage.PROPOSAL_MADE, 3), (CrmStage.ONBOARDING, 2)):
            for age in (days - 1, days):
                day = date.fromordinal(TODAY.toordinal() - age).isoformat()
                self.lead(f'{stage.name}-{age}', stage, day)
        self.run_day()
        listed = [line.split(' — ')[0] for line in self.sent()[700000001].split('\n') if ' — ' in line]
        self.assertEqual(listed, ['LEADS-28', 'NEGOTIATIONS-7', 'PROPOSAL_MADE-3', 'ONBOARDING-2'])

    def test_lost_and_unstaged_leads_are_never_listed(self) -> None:
        self.lead('Quiet', CrmStage.LOST, '2026-01-01')
        self.run_day()
        self.assertEqual(self.gateway.calls, [])

    def test_a_log_row_or_stage_change_resets_the_clock(self) -> None:
        logged = self.lead('Logged', CrmStage.ONBOARDING, '2026-11-01')
        self.lead('Moved', CrmStage.ONBOARDING, '2026-11-01', changed='2026-11-30')
        self.lead('Stale', CrmStage.ONBOARDING, '2026-11-01', changed='2026-11-02')
        self.log.append(row(datetime(2026, 11, 30, 15, 0), f'Logged · {logged}'))
        self.run_day()
        self.assertEqual(self.sent()[700000001], 'Hi Ada,\nCold Leads\nStale — Onboarding, 29 days since last contact')

    def test_blank_owner_and_unreachable_owner_go_to_the_default_owner(self) -> None:
        self.person('dan', None)
        self.lead('Blank', CrmStage.LEADS, '2026-10-01', ())
        self.lead('Unreachable', CrmStage.LEADS, '2026-10-01', ('dan',))
        self.run_day()
        self.assertEqual(list(self.sent()), [700000003])
        self.assertEqual(self.sent()[700000003].count(' — Leads, 61 days'), 2)

    def test_missing_lead_contact_and_studio_are_named(self) -> None:
        self.lead('Bare', CrmStage.LEADS, '2026-10-01', role=False, studio=False)
        self.lead('NoStudio', CrmStage.LEADS, '2026-10-01', studio=False)
        self.run_day()
        text = self.sent()[700000001]
        self.assertIn('Bare — Leads, 61 days since last contact\n  Missing: Lead contact, Studio', text)
        self.assertIn('NoStudio — Leads, 61 days since last contact\n  Missing: Studio', text)

    def test_many_concerns_are_capped(self) -> None:
        project = self.lead('Busy', CrmStage.LEADS, '2026-10-01')
        for number in range(5):
            self.concern(project, f'Issue {number}')
        self.run_day()
        text = self.sent()[700000001]
        self.assertEqual(text.count('Concern:'), 3)
        self.assertIn('and 2 more open Concerns', text)

    def test_nothing_due_sends_nothing_and_a_date_is_sent_once(self) -> None:
        self.run_day()
        self.assertEqual(self.gateway.calls, [])
        self.lead('Old', CrmStage.LEADS, '2026-10-01')
        self.reminder.schedule()  # a restarted worker keeps the date's durable job key
        self.assertFalse(self.worker.run_once())  # 10:00 has not come
        self.now[0] = at('2026-12-02', 10)
        self.assertTrue(self.worker.run_once())
        self.assertEqual([c['idempotency_key'] for c in self.gateway.calls], ['crm-dm:ada:2026-12-02'])
        self.reminder.schedule()
        self.assertFalse(self.worker.run_once())

    def test_a_missed_date_is_skipped(self) -> None:
        self.lead('Old', CrmStage.LEADS, '2026-10-01')
        self.reminder.schedule()
        self.now[0] = at('2026-12-03', 10)
        self.reminder.schedule()
        self.assertTrue(self.worker.run_once())  # 12-01 is obsolete
        self.assertEqual(self.gateway.calls, [])
        self.assertTrue(self.worker.run_once())
        self.assertEqual([c['idempotency_key'] for c in self.gateway.calls], ['crm-dm:ada:2026-12-03'])

    def test_schedule_runs_at_ten_manila(self) -> None:
        self.reminder.schedule()
        self.now[0] = at('2026-12-01', 10) - 60
        self.assertFalse(self.worker.run_once())

    def test_unreachable_default_owner_refuses_so_the_job_is_visible(self) -> None:
        self.person('cyril2', 0)
        self.reminder.default_owner = 'cyril2'
        self.lead('Blank', CrmStage.LEADS, '2026-10-01', ())
        self.reminder.schedule()
        self.assertTrue(self.worker.run_once())
        self.assertEqual(self.gateway.calls, [])
        result = self.queue.result(self.queue.find('crm.reminder')[0].job_id, 'main')
        self.assertTrue(result is None or 'Telegram' in str(result))

    def reminders(self, roles: CrmRoles) -> None:
        self.reminder.roles = roles

    def test_quote_goes_to_the_post_producer_and_billing_to_all_three_in_one_dm(self) -> None:
        self.person('pat', 700000011)
        old = self.lead('Old', CrmStage.LEADS, '2026-10-01')
        self.lead('Fresh', CrmStage.LEADS, '2026-11-30', ())
        self.reminders(CrmRoles(frozenset({700000011}), frozenset({800000001}), frozenset({700000001})))
        self.log += [row(datetime(2026, 10, 2), f'Old · {old}', 'quote', 'https://example.test/q', datetime(2026, 12, 1)),
                     row(datetime(2026, 10, 2), f'Old · {old}', 'billing', None, datetime(2026, 11, 28))]
        self.run_day()
        sent = self.sent()
        self.assertEqual(sorted(sent), [700000001, 700000011, 800000001])
        self.assertEqual(len(self.gateway.calls), 3)  # the Finance Head is also Ada: one DM, cold Lead and reminder
        self.assertEqual(sent[700000011], 'Hi,\nReminders\nBilling due 2026-11-28: Old, note\n'
                                          'Quote due 2026-12-01: Old, note https://example.test/q')
        self.assertEqual(sent[800000001], 'Hi,\nReminders\nBilling due 2026-11-28: Old, note')
        self.assertEqual(sent[700000001], 'Hi Ada,\nCold Leads\nOld — Leads, 60 days since last contact\nReminders\nBilling due 2026-11-28: Old, note')
        self.assertIn('crm-dm:tg:800000001:2026-12-01', [c['idempotency_key'] for c in self.gateway.calls])

    def test_done_or_future_rows_send_nothing_and_a_row_survives_the_stage(self) -> None:
        past = self.lead('Past', CrmStage.LEADS, '2026-11-30')
        self.reminders(CrmRoles(frozenset({700000002}), frozenset(), frozenset()))
        self.log += [row(datetime(2026, 11, 1), f'Past · {past}', 'quote', None, datetime(2026, 11, 20), True),
                     row(datetime(2026, 11, 1), f'Past · {past}', 'quote', None, datetime(2026, 12, 5))]
        self.run_day()
        self.assertEqual(self.gateway.calls, [])
        self.log.append(row(datetime(2026, 11, 1), f'Past · {past}', 'quote', None, datetime(2026, 11, 20)))
        self.projects.clear()  # no longer a Lead: the reminder still comes
        self.now[0] = at('2026-12-02', 10)
        self.reminder.schedule()
        self.assertTrue(self.worker.run_once())
        self.assertEqual(self.sent(), {700000002: 'Hi,\nReminders\nQuote due 2026-11-20: ' + past + ', note'})

    def test_no_role_settings_means_no_reminders(self) -> None:
        project = self.lead('Q', CrmStage.LEADS, '2026-11-30')
        self.log.append(row(datetime(2026, 11, 1), f'Q · {project}', 'billing', None, datetime(2026, 11, 20)))
        self.run_day()
        self.assertEqual(self.gateway.calls, [])

    def job(self) -> WorkerJob:
        return WorkerJob(1, 'main', 'crm.reminder', '2026-12-01', {}, 't', 1)

    def test_an_unreachable_recipient_does_not_stop_the_others_and_the_job_then_fails_naming_them(self) -> None:
        self.person('cyril2', 0)
        self.reminder.default_owner = 'cyril2'
        self.lead('Blank', CrmStage.LEADS, '2026-10-01', ())
        self.lead('Mine', CrmStage.LEADS, '2026-10-01')
        with self.assertRaisesRegex(Refused, 'cyril2'):
            self.reminder.handle(self.job())
        self.assertEqual(list(self.sent()), [700000001])
        with self.assertRaises(Refused):
            self.reminder.handle(self.job())  # a retry keeps the same idempotency keys, so the gateway drops a repeat
        self.assertEqual({call['idempotency_key'] for call in self.gateway.calls}, {'crm-dm:ada:2026-12-01'})

    def test_a_reminder_names_a_project_that_is_no_longer_a_lead_and_falls_back_to_the_id(self) -> None:
        running = self.lead('Running', CrmStage.LEADS, '2026-11-30')
        unread = self.lead('Unread', CrmStage.LEADS, '2026-11-30')
        self.projects.clear()  # both are past the CRM now
        self.reminder.project_name = {running: 'Running Show'}.get
        self.reminders(CrmRoles(frozenset({700000002}), frozenset(), frozenset()))
        self.log += [row(datetime(2026, 11, 1), f'Running \u00b7 {running}', 'quote', None, datetime(2026, 11, 20)),
                     row(datetime(2026, 11, 1), f'Unread \u00b7 {unread}', 'quote', None, datetime(2026, 11, 21))]
        self.run_day()
        self.assertEqual(self.sent(), {700000002: f'Hi,\nReminders\nQuote due 2026-11-20: Running Show, note\nQuote due 2026-11-21: {unread}, note'})

    def test_a_snoozed_billing_holder_does_not_give_the_post_producer_a_second_copy(self) -> None:
        from t6.crm.snooze import Snoozes
        self.person('pat', 700000011)
        self.person('fin', 700000012)
        project = self.lead('Q', CrmStage.LEADS, '2026-11-30', ())
        self.reminders(CrmRoles(frozenset({700000011}), frozenset(), frozenset({700000012})))
        self.log.append(row(datetime(2026, 11, 1), f'Q \u00b7 {project}', 'billing', None, datetime(2026, 11, 20)))
        Snoozes(self.registry).snooze('fin', '2026-12-05')
        digest = self.reminder.digest(TODAY)
        self.assertEqual(list(digest), ['pat'])
        self.assertEqual([item.lines for item in digest['pat']], [(f'Billing due 2026-11-20: Q, note',)])


class CrmRolesTests(unittest.TestCase):
    def test_roles_read_telegram_ids(self) -> None:
        self.assertEqual(crm_roles({}), CrmRoles(frozenset(), frozenset(), frozenset()))
        roles = crm_roles({'T6_CRM_POST_PRODUCER_TELEGRAM_IDS': ' 11, 22 ,', 'T6_CRM_ACCOUNTANT_TELEGRAM_IDS': '33',
                           'T6_CRM_FINANCE_HEAD_TELEGRAM_IDS': '44'})
        self.assertEqual(roles, CrmRoles(frozenset({11, 22}), frozenset({33}), frozenset({44})))
        self.assertEqual(roles.billing, frozenset({11, 22, 33, 44}))
        with self.assertRaises(ValueError):
            crm_roles({'T6_CRM_ACCOUNTANT_TELEGRAM_IDS': 'abc'})


class ReadLeadsTests(unittest.TestCase):
    def test_stage_comes_from_history_else_status_and_owners_map_to_persons(self) -> None:
        registry = MemoryRegistry()
        self.addCleanup(registry.db.close)
        registry._new_id = lambda: str(T6Id.mint(EntityKind.PROJECT))
        ada = T6Id.mint(EntityKind.PERSON)
        with registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES (?, 'staff', '[]', '[]', 700000001, 0, 1)", (str(ada),))
        registry.bind_entity('notion', 'contact-ada', ada, actor='setup')
        ids = {}
        for page in ('moved', 'status', 'lost', 'running', 'unregistered'):
            if page != 'unregistered':
                found = registry.register(actor='setup', idempotency_key=page, notion_page_id=page)
                assert isinstance(found, Registered)
                ids[page] = found.project.id
        with registry._transaction() as db:
            db.execute("INSERT INTO project_stage_history (project_id, at, actor, field, from_value, to_value, event) VALUES (?, 1.0, 'ada', 'crm_stage', NULL, 'Onboarding', 'e1')", (ids['moved'],))
        projects = [
            Project(page_id='moved', name='Moved', status='Leads', lead_owner_ids=('contact-ada',), studio_id='s', delegate_producer_ids=('d',)),
            Project(page_id='status', name='From Status', status='🤝 Proposal Made'),
            Project(page_id='lost', name='Lost', status='Lost'),
            Project(page_id='running', name='Running', status='In-Progress'),
            Project(page_id='unregistered', name='Unregistered', status='Leads'),
        ]
        found_leads = read_leads(registry, projects)
        self.assertEqual([(lead.name, lead.stage, lead.owner_ids, lead.has_lead_contact, lead.has_studio) for lead in found_leads], [
            ('Moved', CrmStage.ONBOARDING, (str(ada),), True, True),
            ('From Status', CrmStage.PROPOSAL_MADE, (), False, False)])


class StartupTests(unittest.TestCase):
    def test_worker_schedules_one_job_when_configured_and_none_without(self) -> None:
        import tempfile
        from pathlib import Path
        from t6.registry.sqlite import SqliteProjectRegistry
        from t6.worker.__main__ import main
        from test_worker_main import ENVIRON
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        registry = SqliteProjectRegistry(Path(folder.name) / 't6.sqlite')
        with registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES ('cyril', 'staff', '[]', '[]', 700000003, 0, 1)")
        stop = threading.Event()
        stop.set()
        base = {**ENVIRON, 'T6_DATABASE': str(Path(folder.name) / 't6.sqlite'), 'T6_API_URL': 'https://synthetic.invalid',
                'T6_GATEWAY_URL': 'https://synthetic.invalid', 'T6_GATEWAY_SECRET': 'synthetic-secret',
                'T6_NOTION_TOKEN': 'synthetic', 'T6_NOTION_PROJECTS_DATA_SOURCE': 'synthetic-projects'}
        self.assertEqual(main(base, {'main': {}}, stop=stop), 0)
        self.assertEqual(InternalJobQueue(registry).find('crm.reminder'), [])
        env = {**base, 'T6_CRM_SHEET_ID': 'synthetic-crm', 'T6_CRM_DEFAULT_OWNER_PERSON_ID': 'cyril'}
        with patch('t6.crm.config.crm_source', lambda _: FakeSheet([])):
            for _ in range(2):
                self.assertEqual(main(env, {'main': {}}, stop=stop), 0)
        self.assertEqual(len(InternalJobQueue(registry).find('crm.reminder')), 1)

    def worker_env(self, owner: str) -> tuple[dict[str, str], Any]:
        import tempfile
        from pathlib import Path
        from t6.registry.sqlite import SqliteProjectRegistry
        from test_worker_main import ENVIRON
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        registry = SqliteProjectRegistry(Path(folder.name) / 't6.sqlite')
        with registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES ('mute', 'staff', '[]', '[]', NULL, 0, 1)")
        return {**ENVIRON, 'T6_DATABASE': str(Path(folder.name) / 't6.sqlite'), 'T6_API_URL': 'https://synthetic.invalid',
                'T6_GATEWAY_URL': 'https://synthetic.invalid', 'T6_GATEWAY_SECRET': 'synthetic-secret',
                'T6_NOTION_TOKEN': 'synthetic', 'T6_NOTION_PROJECTS_DATA_SOURCE': 'synthetic-projects',
                'T6_CRM_SHEET_ID': 'synthetic-crm', 'T6_CRM_DEFAULT_OWNER_PERSON_ID': owner}, registry

    def test_a_default_owner_who_cannot_be_messaged_stops_the_worker_at_startup(self) -> None:
        from t6.worker.__main__ import main
        stop = threading.Event()
        stop.set()
        for owner in ('nobody', 'mute'):
            env, _ = self.worker_env(owner)
            err = StringIO()
            with patch('t6.crm.config.crm_source', lambda _: FakeSheet([])), redirect_stderr(err):
                self.assertEqual(main(env, {'main': {}}, stop=stop), 2, owner)
            self.assertIn('T6_CRM_DEFAULT_OWNER_PERSON_ID', err.getvalue())

    def test_a_sheet_without_a_key_turns_the_features_off_with_a_warning(self) -> None:
        from t6.worker.__main__ import main
        stop = threading.Event()
        stop.set()
        env, registry = self.worker_env('mute')
        err = StringIO()
        with redirect_stderr(err):
            self.assertEqual(main(env, {'main': {}}, stop=stop), 0)
        self.assertIn('GOOGLE_APPLICATION_CREDENTIALS', err.getvalue())
        self.assertEqual(InternalJobQueue(registry).find('crm.reminder'), [])

