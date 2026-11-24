"""The daily CRM DM to Lead Owners, with synthetic records and a recording gateway."""
import json
import threading
import unittest
from datetime import date, datetime
from typing import Any
from unittest.mock import patch

from t6.concern import _insert
from t6.crm.reminder import CrmReminder, LeadFacts, read_leads
from t6.domain import CrmStage, Project
from t6.domain.studio_time import MANILA
from t6.ids import EntityKind, T6Id
from t6.person import People
from t6.preview import PreviewTokens
from t6.registry import Registered
from t6.worker import Worker
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

    def test_missing_client_role_and_studio_are_named(self) -> None:
        self.lead('Bare', CrmStage.LEADS, '2026-10-01', role=False, studio=False)
        self.lead('NoStudio', CrmStage.LEADS, '2026-10-01', studio=False)
        self.run_day()
        text = self.sent()[700000001]
        self.assertIn('Bare — Leads, 61 days since last contact\n  Missing: client role, Studio', text)
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
        self.assertEqual([(lead.name, lead.stage, lead.owner_ids, lead.has_client_role, lead.has_studio) for lead in found_leads], [
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
