"""The 21:00 Manila autoscan, the PM Notes history and `project notes-history` (#638)."""
import json
import tempfile
import unittest
from datetime import datetime, time
from typing import Any
from unittest.mock import patch

from t6.domain.studio_time import MANILA
from t6.ids import EntityKind, T6Id
from t6.preview import PreviewTokens
from t6.registry import Registered
from t6.verbs import Done
from test_person import MemoryRegistry

DAY = 86400.0


def manila(text: str) -> float:
    return datetime.fromisoformat(text).replace(tzinfo=MANILA).timestamp()


class Stop(Exception):
    pass


class ScheduleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = MemoryRegistry()
        self.addCleanup(self.registry.db.close)
        self.runs: list[float] = []

    def run_loop(self, start: str, wakes: int, completed: str | None = None, succeed: bool = True, minutes: float = 10) -> list[float]:
        """Run the schedule from `start` until it has slept `wakes` times; returns the slots it ran for."""
        from t6.server.autoscan import record_completed, run_forever
        if completed is not None:
            record_completed(self.registry, manila(completed))
        clock = [manila(start)]
        slept = [0]

        def sleep(seconds: float) -> None:
            slept[0] += 1
            if slept[0] > wakes:
                raise Stop
            clock[0] += seconds

        def run(slot: float) -> bool:
            self.runs.append(slot)
            clock[0] += minutes * 60
            return succeed
        with self.assertRaises(Stop):
            run_forever(run, self.registry, time(21, 0), clock=lambda: clock[0], sleep=sleep)
        return self.runs

    def test_the_schedule_is_off_unless_configured_and_the_time_is_configurable(self) -> None:
        from t6.server.autoscan import autoscan_at
        self.assertIsNone(autoscan_at({}))
        self.assertIsNone(autoscan_at({'T6_CONCERN_AUTOSCAN': '0'}))
        self.assertEqual(autoscan_at({'T6_CONCERN_AUTOSCAN': '1'}), time(21, 0))
        self.assertEqual(autoscan_at({'T6_CONCERN_AUTOSCAN': 'on', 'T6_CONCERN_AUTOSCAN_AT': '20:30'}), time(20, 30))

    def test_start_does_not_run_and_21_00_manila_does(self) -> None:
        runs = self.run_loop('2026-11-24T10:00', wakes=1, completed='2026-11-23T21:10')
        self.assertEqual(runs, [manila('2026-11-24T21:00')])

    def test_each_day_runs_once_at_21_00(self) -> None:
        runs = self.run_loop('2026-11-24T10:00', wakes=2, completed='2026-11-23T21:10')
        self.assertEqual(runs, [manila('2026-11-24T21:00'), manila('2026-11-25T21:00')])

    def test_a_missed_21_00_is_caught_up_once_at_start(self) -> None:
        runs = self.run_loop('2026-11-25T08:00', wakes=1, completed='2026-11-23T21:10')
        self.assertEqual(runs, [manila('2026-11-24T21:00'), manila('2026-11-25T21:00')])  # the catch-up serves the missed slot once; the next run is tonight

    def test_a_restart_after_the_catch_up_does_not_run_it_again(self) -> None:
        self.run_loop('2026-11-25T08:00', wakes=0, completed='2026-11-23T21:10')
        self.runs.clear()
        self.assertEqual(self.run_loop('2026-11-25T09:00', wakes=0), [])

    def test_a_catch_up_that_ends_after_the_next_21_00_still_runs_that_21_00(self) -> None:
        runs = self.run_loop('2026-11-23T20:30', wakes=1, completed='2026-11-21T21:10', minutes=60)
        self.assertEqual(runs, [manila('2026-11-22T21:00'), manila('2026-11-23T21:00'), manila('2026-11-24T21:00')])

    def test_the_first_start_after_the_upgrade_runs_nothing_and_waits_for_21_00(self) -> None:
        runs = self.run_loop('2026-11-25T10:00', wakes=1)
        self.assertEqual(runs, [manila('2026-11-25T21:00')])

    def test_a_failed_run_is_not_retried_until_the_next_slot(self) -> None:
        runs = self.run_loop('2026-11-25T08:00', wakes=1, completed='2026-11-23T21:10', succeed=False)
        self.assertEqual(runs, [manila('2026-11-24T21:00'), manila('2026-11-25T21:00')])
        from t6.server.autoscan import last_completed
        self.assertEqual(last_completed(self.registry), manila('2026-11-23T21:10'))  # only a completed run is stored


class HistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = MemoryRegistry()
        self.addCleanup(self.registry.db.close)
        self.now = manila('2026-11-23T21:00')
        self.registry._clock = lambda: self.now
        self.registry._new_id = lambda: str(T6Id.mint(EntityKind.PROJECT))
        result = self.registry.register(actor='setup', idempotency_key='setup', notion_page_id='project')
        assert isinstance(result, Registered)
        self.project = result.project.id
        with self.registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES ('t6n_coordinator', 'staff', '[]', '[]', 101, 0, 1)")
            db.execute("INSERT INTO persons VALUES ('t6n_closer', 'staff', '[]', '[]', 102, 1, 1)")
            db.execute("INSERT INTO persons VALUES ('t6n_other', 'staff', '[]', '[]', 103, 0, 1)")
            db.execute("UPDATE projects SET rotation = 'in' WHERE id = ?", (self.project,))

    def api(self) -> Any:
        from t6.person import People
        from t6.privacy import Registry
        from t6.server import ServerConfig, WebhookApp
        from t6.server.queue import JobQueue
        from t6.server.verbs import VerbAPI
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        queue = JobQueue(tmp.name + '/queue')
        return VerbAPI(People(self.registry, PreviewTokens('secret')), queue, {'gateway': 'gateway'}, lambda _: None,
                       WebhookApp(queue, ServerConfig(), registry=self.registry), llm_service=object(),
                       privacy_registry=Registry())

    def run_once(self, api: Any, lines: list[dict[str, str]]) -> str:
        import contextlib
        import io
        err = io.StringIO()
        answer = {'choice': 'new', 'quote': 'Waiting on client', 'waiting_on': 'client'}
        with patch.object(api, '_coordinators', return_value={'t6n_coordinator'}), patch.object(api, '_typed_read', return_value=Done(result={'lines': lines})), \
                patch('t6.concern_task.classify', return_value=answer), contextlib.redirect_stderr(err):
            self.assertTrue(api.autoscan(self.now))
        return err.getvalue()

    def history(self, api: Any, telegram_id: int, project: str | None = None) -> dict[str, Any]:
        from t6.server.app import Request
        headers = {'Authorization': 'Bearer gateway', 'X-Telegram-User-Id': str(telegram_id)}
        body = json.dumps({'project': project or self.project}).encode()
        with patch.object(api, '_coordinators', side_effect=lambda p: {'t6n_coordinator'} if p == self.project else set()):
            return dict(api(Request('POST', '/v1/project/notes-history', headers, body)).body)

    def test_history_across_three_runs_and_the_run_logs_counts_only(self) -> None:
        api = self.api()
        first = [{'as_of': '2026-11-23', 'text': 'Waiting on client'}, {'as_of': '2026-11-23', 'text': 'Grade booked'}]
        log = self.run_once(api, first)
        self.assertEqual(log, 't6.server: concern scan archived=1 projects=1 skipped=0 raised=1 continued=0 announced=0 applied=0\n')
        self.now += DAY
        edited = [{'as_of': '2026-11-23', 'text': 'Waiting on client'}, {'as_of': '2026-11-23', 'text': 'Grade booked Friday'}]
        self.run_once(api, edited)
        self.now += DAY
        self.run_once(api, edited)
        lines = self.history(api, 102)['result']['lines']
        self.assertEqual(lines, [
            {'as_of': '2026-11-23', 'text': 'Grade booked Friday', 'first_seen': '2026-11-24', 'last_seen': '2026-11-25', 'removed': None},
            {'as_of': '2026-11-23', 'text': 'Grade booked', 'first_seen': '2026-11-23', 'last_seen': '2026-11-23', 'removed': '2026-11-24'},
            {'as_of': '2026-11-23', 'text': 'Waiting on client', 'first_seen': '2026-11-23', 'last_seen': '2026-11-25', 'removed': None},
        ])

    def test_history_is_newest_first(self) -> None:
        api = self.api()
        self.run_once(api, [{'as_of': '2026-11-16', 'text': 'Older line'}, {'as_of': '2026-11-23', 'text': 'Newer line'}])
        self.assertEqual([line['text'] for line in self.history(api, 101)['result']['lines']], ['Newer line', 'Older line'])

    def test_notes_history_is_for_master_brief_persons_and_the_projects_coordinators_only(self) -> None:
        api = self.api()
        self.run_once(api, [{'as_of': '2026-11-23', 'text': 'Waiting on client'}])
        self.assertEqual(len(self.history(api, 102)['result']['lines']), 1)  # a Person with master_brief on
        self.assertEqual(len(self.history(api, 101)['result']['lines']), 1)  # the Project's coordinator
        refused = self.history(api, 103)
        self.assertEqual((refused['outcome'], refused['rule']), ('refused', 'coordinator'))
        unknown = self.history(api, 102, project=str(T6Id.mint(EntityKind.PROJECT)))
        self.assertEqual((unknown['outcome'], unknown['rule']), ('refused', 'project'))


if __name__ == '__main__':
    unittest.main()
