"""Project Start from the `/project` card (#540): an operator starts a Project from Telegram, and the Approval card follows.

Fake Notion, in-memory t6 database. Every name below is invented.
"""
import json
import tempfile
import unittest
from typing import Any

from t6.ids import EntityKind, T6Id
from t6.notion.generated import ProjectsRow
from t6.notion.testing import FakeNotion, project_properties
from t6.operator import ActionStore, OperatorService, TapResult
from t6.person import People
from t6.preview import PreviewTokens
from t6.project_start import ProjectStartStore
from t6.registry import Registered, Rotation
from t6.worker import Worker
from t6.worker.internal_queue import InternalJobQueue, InternalQueueClient
from test_person import MemoryRegistry

OPERATOR, STAFF = 42, 43
MESSAGE = 777


class OperatorProjectStartTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = MemoryRegistry()
        self.addCleanup(self.registry.db.close)
        self.registry._new_id = lambda: str(T6Id.mint(EntityKind.PROJECT))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.people = People(self.registry, PreviewTokens('test'))
        with self.registry._transaction() as db:
            for who, telegram in (('t6n_' + 'a' * 32, OPERATOR), ('t6n_' + 'b' * 32, STAFF)):
                db.execute("INSERT INTO persons VALUES (?, 'staff', '[]', '[]', ?, 0, 1)", (who, telegram))
            db.execute("INSERT INTO person_permissions VALUES (?, 'operator')", ('t6n_' + 'a' * 32,))
        self.fake = FakeNotion()
        self.fake.add_snapshot_data_source('projects', 'projects')
        self.client = self.fake.client()
        self.addCleanup(self.client.close)
        page = str(self.fake.add_page('projects', project_properties(name='Show A', project_type='Feature Film'))['id'])
        made = self.registry.register(actor='setup', idempotency_key='reg', code='EX01', rotation=Rotation.IN, notion_page_id=page)
        assert isinstance(made, Registered)
        self.project = made.project.id
        self.service = OperatorService(self.registry, ActionStore(tmp.name + '/actions'), (OPERATOR,), people=self.people)
        self.store = ProjectStartStore(self.registry, self.people.tokens, lambda p: ProjectsRow.from_page(self.client.retrieve_page(p)).project_type,
                                       lambda p: ProjectsRow.from_page(self.client.retrieve_page(p)).name)
        self.queue = InternalJobQueue(self.registry, clock=self.registry._clock)
        self.keys = 0

    def key(self) -> str:
        self.keys += 1
        return f'key-{self.keys}'

    def open_card(self) -> str:
        result = self.service.command('EX01', operator=OPERATOR, chat=OPERATOR, key=self.key())
        assert result.action is not None
        self.service.delivered(result.action.token, 1, MESSAGE)
        return result.action.token

    def buttons(self, token: str) -> list[str]:
        card = self.service.card(token)
        assert card is not None
        return [b.text for row in card.buttons for b in row]

    def press(self, token: str, text: str, who: int = OPERATOR) -> TapResult:
        card = self.service.card(token)
        assert card is not None
        for row in card.buttons:
            for button in row:
                if button.text.startswith(text):
                    return self.service.tap(button.payload, operator=who, chat=card.chat, message_id=MESSAGE, tap_key=self.key())
        raise AssertionError(f'no button {text!r} in {self.buttons(token)}')

    def plan_job_runs(self) -> None:
        worker = Worker(InternalQueueClient(self.queue), 'main', {'project_start.preview': lambda job: self.store.run_preview(job.payload)})
        while worker.run_once():
            pass

    def test_an_operator_sees_the_button_and_it_queues_one_plan_job(self) -> None:
        token = self.open_card()
        self.assertIn('Start Project', self.buttons(token))
        self.assertEqual(self.press(token, 'Start Project').outcome, 'start_queued')
        (job,) = self.queue.find('project_start.preview')
        self.assertIn(f'"project": "{self.project}"', json.dumps(job.payload['inputs']))
        self.assertEqual(self.buttons(token)[0], 'Check plan')

    def test_checking_before_the_worker_ran_says_pending_and_after_shows_the_plan(self) -> None:
        token = self.open_card()
        self.press(token, 'Start Project')
        self.assertEqual(self.press(token, 'Check plan').outcome, 'start_pending')
        self.plan_job_runs()
        self.assertEqual(self.press(token, 'Check plan').outcome, 'start_ready')
        card = self.service.card(token)
        assert card is not None
        self.assertIn('frameio', card.text)
        self.assertIn('Confirm start', self.buttons(token))

    def test_confirming_requests_the_approval_and_the_approval_card_follows(self) -> None:
        token = self.open_card()
        self.press(token, 'Start Project')
        self.plan_job_runs()
        self.press(token, 'Check plan')
        self.assertEqual(self.press(token, 'Confirm start').outcome, 'start_requested')
        with self.registry._transaction() as db:
            (approval,) = db.execute('SELECT id, gate, state FROM approvals').fetchall()
        self.assertEqual(approval[1:], ('project start', 'requested'))
        self.assertEqual([a.kind for a in self.service.recover_approvals()], ['approval'])

    def test_a_project_that_cannot_start_shows_the_reason_and_starts_nothing(self) -> None:
        token = self.open_card()
        self.press(token, 'Start Project')
        self.plan_job_runs()
        self.press(token, 'Check plan')
        self.press(token, 'Confirm start')
        again = self.open_card()
        self.press(again, 'Start Project')
        self.plan_job_runs()
        self.assertEqual(self.press(again, 'Check plan').outcome, 'start_refused')
        self.assertNotIn('Confirm start', self.buttons(again))

    def test_a_person_without_the_operator_permission_cannot_start(self) -> None:
        token = self.open_card()
        card = self.service.card(token)
        assert card is not None
        payload = next(b.payload for row in card.buttons for b in row if b.text == 'Start Project')
        answer = self.service.tap(payload, operator=STAFF, chat=card.chat, message_id=MESSAGE, tap_key=self.key())
        self.assertEqual(answer.outcome, 'rejected')
        self.assertEqual(self.queue.find('project_start.preview'), [])


if __name__ == '__main__':
    unittest.main()
