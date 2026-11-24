"""CRM snooze: the `crm snooze` and `crm unsnooze` verbs and the rerouting of a snoozed person's CRM DM items."""
import os
import unittest
from datetime import date
from typing import Any
from unittest.mock import patch

from t6.crm.config import CrmRoles
from t6.crm.reminder import CrmReminder, LeadFacts
from t6.domain import CrmStage
from t6.ids import EntityKind, T6Id
from t6.person import People
from t6.preview import PreviewTokens
from t6.registry import Registered
from t6.verbs import CrmSnoozeInput, CrmUnsnoozeInput, Done, Refused, crm_snooze, crm_unsnooze, invoke, VERBS
from t6.worker import Worker
from t6.worker.gateway import GatewayClient
from t6.worker.internal_queue import InternalJobQueue, InternalQueueClient
from test_crm_reminder import Gateway, at
from test_crm_log import FakeSheet
from test_person import MemoryRegistry


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = MemoryRegistry()
        self.addCleanup(self.registry.db.close)
        self.now = [at('2026-12-01', 10)]
        self.registry._clock = lambda: self.now[0]
        self.registry._new_id = lambda: str(T6Id.mint(EntityKind.PROJECT))
        for person, telegram in (('ada', 700000001), ('ben', 700000002), ('cyril', 700000003), ('pam', 700000004), ('pat', 700000005)):
            with self.registry._transaction() as db:
                db.execute("INSERT INTO persons VALUES (?, 'staff', '[]', '[]', ?, 0, 1)", (person, telegram))
        self.people = People(self.registry, PreviewTokens('test'))

    def run_verb(self, verb: str, actor: str, until: str | None = None) -> Any:
        entry = next(v for v in VERBS if (v.noun, v.verb) == ('crm', verb))
        inputs = CrmSnoozeInput(until=until) if verb == 'snooze' else CrmUnsnoozeInput()
        with patch.dict(os.environ, {}):
            return invoke(entry, inputs, actor=actor, people=self.people, open_notion=lambda *_: None)

    def audits(self, action: str) -> list[tuple[str, str]]:
        with self.registry._transaction() as db:
            return [(r[0], r[1]) for r in db.execute('SELECT project_id, actor FROM audit WHERE action = ?', (action,))]


class SnoozeVerbTests(Base):
    def test_snooze_through_the_end_date_returns_the_day_after(self) -> None:
        done = self.run_verb('snooze', 'ada', '2026-12-10')
        self.assertIsInstance(done, Done)
        self.assertEqual(done.result['until'], '2026-12-10')
        self.assertEqual(done.result['returns_on'], '2026-12-11')
        self.assertIn('2026-12-11', done.result['message'])
        self.assertEqual(self.audits('crm_snooze'), [('ada', 'ada')])

    def test_missing_date_is_refused_in_plain_words(self) -> None:
        for until in (None, '', '  '):
            refused = self.run_verb('snooze', 'ada', until)
            self.assertIsInstance(refused, Refused)
            self.assertIn('end date', refused.reason)
            self.assertIn('2026-12-31', refused.reason)
        self.assertEqual(self.audits('crm_snooze'), [])

    def test_more_than_30_days_is_refused_and_30_is_allowed(self) -> None:
        refused = self.run_verb('snooze', 'ada', '2027-01-01')
        self.assertIsInstance(refused, Refused)
        self.assertEqual(refused.reason, 'Snooze is at most 30 days. The latest end date today is 2026-12-31.')
        self.assertIsInstance(self.run_verb('snooze', 'ada', '2026-12-31'), Done)

    def test_unreadable_and_past_dates_are_refused(self) -> None:
        self.assertIn('could not read', self.run_verb('snooze', 'ada', 'next week').reason)
        self.assertIn('already passed', self.run_verb('snooze', 'ada', '2026-11-30').reason)
        self.assertIsInstance(self.run_verb('snooze', 'ada', '2026-12-01'), Done)  # today: snoozed for today only

    def test_the_30_days_count_from_today_in_manila(self) -> None:
        self.now[0] = at('2026-12-01', 23) + 3600  # 00:00 on 12-02 in Manila
        self.assertIsInstance(self.run_verb('snooze', 'ada', '2027-01-01'), Done)

    def test_snoozing_again_replaces_the_date_and_unsnooze_clears_it(self) -> None:
        self.run_verb('snooze', 'ada', '2026-12-10')
        self.run_verb('snooze', 'ada', '2026-12-20')
        done = self.run_verb('unsnooze', 'ada')
        self.assertEqual(done.result['was_snoozed'], True)
        self.assertEqual(self.audits('crm_unsnooze'), [('ada', 'ada')])
        again = self.run_verb('unsnooze', 'ada')
        self.assertEqual(again.result['was_snoozed'], False)
        self.assertEqual(self.audits('crm_unsnooze'), [('ada', 'ada')])  # nothing changed, nothing audited
        with self.registry._transaction() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM crm_snooze').fetchone(), (0,))

    def test_only_an_active_staff_person_snoozes_and_only_themselves(self) -> None:
        self.assertIsInstance(self.run_verb('snooze', 'gateway', '2026-12-10'), Refused)
        self.assertIsInstance(self.run_verb('snooze', 'nobody', '2026-12-10'), Refused)
        with self.registry._transaction() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM crm_snooze').fetchone(), (0,))

    def test_handlers_are_registered_as_writes(self) -> None:
        entries = {v.verb: v for v in VERBS if v.noun == 'crm'}
        self.assertIs(entries['snooze'].handler, crm_snooze)
        self.assertIs(entries['unsnooze'].handler, crm_unsnooze)
        self.assertFalse(entries['snooze'].read_only or entries['unsnooze'].read_only)


class RoutingTests(Base):
    def setUp(self) -> None:
        super().setUp()
        self.projects: dict[str, LeadFacts] = {}
        self.names = {'ada': 'Ada', 'ben': 'Ben', 'cyril': 'Cyril', 'pam': 'Pam'}
        self.queue = InternalJobQueue(self.registry, clock=lambda: self.now[0])
        self.gateway = Gateway()
        self.reminder = CrmReminder(
            self.people, self.queue, GatewayClient(self.gateway, 'synthetic-secret'), FakeSheet([]), 'cyril',
            lambda: list(self.projects.values()), self.names.get, clock=lambda: self.now[0], roles=CrmRoles(post_producer=frozenset({700000004, 700000005})))
        self.worker = Worker(InternalQueueClient(self.queue), 'main', {'crm.reminder': self.reminder.handle})

    def lead(self, name: str, owners: tuple[str, ...]) -> None:
        self.now[0] = at('2026-10-01')
        found = self.registry.register(actor='setup', idempotency_key=name, notion_page_id=f'page-{name}')
        assert isinstance(found, Registered)
        self.projects[name] = LeadFacts(found.project.id, name, CrmStage.LEADS, owners, True, True)
        self.now[0] = at('2026-12-01', 10)

    def run_day(self) -> dict[int, str]:
        self.reminder.schedule()
        self.assertTrue(self.worker.run_once())
        return {call['to']: call['text'] for call in self.gateway.calls}

    def snooze(self, person: str, until: str) -> None:
        self.assertIsInstance(self.run_verb('snooze', person, until), Done)

    def test_a_snoozed_owners_items_go_to_the_post_producers_with_a_note(self) -> None:
        self.lead('Alpha', ('ada',))
        self.lead('Beta', ('ben',))
        self.snooze('ada', '2026-12-05')
        sent = self.run_day()
        self.assertEqual(sorted(sent), [700000002, 700000004, 700000005])
        self.assertIn('Alpha — Leads, 61 days since last contact\n  Covering for Ada (snoozed until 2026-12-05)', sent[700000004])
        self.assertNotIn('Alpha', sent[700000002])
        self.assertEqual(sent[700000004].split("\n", 1)[1], sent[700000005].split("\n", 1)[1])

    def test_a_snoozed_post_producer_is_skipped_and_with_all_snoozed_the_default_owner_gets_it(self) -> None:
        self.lead('Alpha', ('ada',))
        self.snooze('ada', '2026-12-05')
        self.snooze('pam', '2026-12-05')
        self.assertEqual(sorted(self.run_day()), [700000005])
        self.gateway.calls.clear()
        self.now[0] = at('2026-12-02', 10)
        self.snooze('pat', '2026-12-05')
        self.assertEqual(list(self.run_day()), [700000003])  # everyone upstream is snoozed: Cyril still gets them

    def test_a_snoozed_default_owners_items_go_to_the_post_producer(self) -> None:
        self.lead('Blank', ())
        self.snooze('cyril', '2026-12-05')
        sent = self.run_day()
        self.assertEqual(sorted(sent), [700000004, 700000005])
        self.assertIn('Covering for Cyril (snoozed until 2026-12-05)', sent[700000004])

    def test_a_snoozed_default_owner_with_no_post_producer_keeps_the_items(self) -> None:
        self.reminder.roles = CrmRoles()
        self.lead('Blank', ())
        self.snooze('cyril', '2026-12-05')
        self.assertEqual(list(self.run_day()), [700000003])

    def test_without_post_producers_a_snoozed_owner_goes_to_the_default_owner(self) -> None:
        self.reminder.roles = CrmRoles()
        self.lead('Alpha', ('ada',))
        self.snooze('ada', '2026-12-05')
        self.assertEqual(list(self.run_day()), [700000003])

    def test_a_snoozed_post_producers_own_items_go_to_the_default_owner_or_other_post_producer(self) -> None:
        self.lead('Mine', ('pam',))
        self.snooze('pam', '2026-12-05')
        self.assertEqual(sorted(self.run_day()), [700000005])

    def test_two_snoozed_owners_of_one_lead_are_named_and_listed_once(self) -> None:
        self.lead('Shared', ('ada', 'ben'))
        self.snooze('ada', '2026-12-05')
        self.snooze('ben', '2026-12-06')
        text = self.run_day()[700000004]
        self.assertEqual(text.count('Shared — Leads'), 1)
        self.assertIn('Covering for Ada (snoozed until 2026-12-05), Ben (snoozed until 2026-12-06)', text)

    def test_items_return_after_the_end_date_and_after_unsnooze(self) -> None:
        self.lead('Alpha', ('ada',))
        self.snooze('ada', '2026-12-02')
        self.now[0] = at('2026-12-02', 10)
        self.assertNotIn(700000001, self.run_day())  # snoozed through the 2nd
        self.gateway.calls.clear()
        self.now[0] = at('2026-12-03', 10)
        self.assertIn(700000001, self.run_day())
        self.gateway.calls.clear()
        self.snooze('ada', '2026-12-20')
        self.now[0] = at('2026-12-04', 10)
        self.assertEqual(self.run_verb('unsnooze', 'ada').result['was_snoozed'], True)
        self.assertIn(700000001, self.run_day())


if __name__ == '__main__':
    unittest.main()
