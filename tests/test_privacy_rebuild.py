"""The `privacy_registry.rebuild` job replaces privacy_registry.json so masked reads and LLM calls hide a new Project at once (#539)."""
import json
import os
import stat
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from t6.domain.studio_time import MANILA
from t6.notion.generated import StudiosRow
from t6.notion.source import NotionSource
from t6.notion.testing import FakeNotion, multi_select, title
from t6.privacy import Session
from t6.privacy_build import Rebuild, next_rebuild_day, schedule_daily_rebuild
from t6.registry.sqlite import SqliteProjectRegistry
from t6.server.verbs import PrivacyList, read_config_from_env
from t6.worker import Worker
from t6.worker.internal_queue import InternalJobQueue, InternalQueueClient


class RebuildTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.output = self.dir / 'privacy_registry.json'
        self.fake = FakeNotion()
        self.fake.add_snapshot_data_source('ds-studios', 'studios')
        self.fake.add_snapshot_data_source('ds-projects', 'projects')
        self.fake.add_page('ds-studios', {StudiosRow.notion_name('name'): title('Acme'), StudiosRow.notion_name('aliases'): multi_select()})
        self.fake.add_page('ds-projects', {'Name': title('Show One')})
        self.rebuild = Rebuild(NotionSource.from_client(self.fake.client()), {'studios': 'ds-studios', 'projects': 'ds-projects'}, self.output)

    def test_it_writes_the_file_and_returns_counts_only(self) -> None:
        result = self.rebuild(object())
        self.assertEqual(result, {'people': 1, 'projects': 1})
        self.assertNotIn('Show One', json.dumps(result))
        self.assertEqual(json.loads(self.output.read_text(encoding='utf-8'))['projects'], [{'name': 'Show One'}])

    def test_the_file_is_private_to_its_owner_even_with_a_loose_umask(self) -> None:
        old = os.umask(0o022)
        self.addCleanup(os.umask, old)
        self.rebuild(object())
        self.rebuild(object())  # a second rebuild replaces it and keeps the mode
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o600)

    def test_a_project_added_later_is_masked_after_the_next_rebuild_with_no_restart(self) -> None:
        self.rebuild(object())
        _, registry = read_config_from_env({'T6_READ_PRIVACY_REGISTRY': str(self.output)})
        assert registry is not None
        names = PrivacyList(str(self.output), registry, None)
        self.assertEqual(Session(names.current()).mask('Show A airs Friday').text, 'Show A airs Friday')
        self.fake.add_page('ds-projects', {'Name': title('Show A')})
        self.rebuild(object())
        self.assertEqual(Session(names.current()).mask('Show A airs Friday').text, '[project 1] airs Friday')
        self.assertFalse(self.output.with_name(self.output.name + '.tmp').exists())

    def test_next_rebuild_day_uses_0400_manila_cutoff(self) -> None:
        self.assertEqual(next_rebuild_day(datetime.fromisoformat('2026-11-25T03:59:59+08:00')), date(2026, 11, 25))
        self.assertEqual(next_rebuild_day(datetime.fromisoformat('2026-11-25T04:00:00+08:00')), date(2026, 11, 26))
        self.assertEqual(next_rebuild_day(datetime.fromisoformat('2026-11-24T19:59:00+00:00')), date(2026, 11, 25))
        self.assertEqual(next_rebuild_day(datetime.fromisoformat('2026-11-24T20:00:00+00:00')), date(2026, 11, 26))

    def test_schedule_daily_rebuild_submits_once_per_manila_day_at_0400(self) -> None:
        registry = SqliteProjectRegistry(self.dir / 't6.sqlite')
        now = datetime.fromisoformat('2026-11-25T03:30:00+08:00')
        queue = InternalJobQueue(registry, clock=now.timestamp)
        schedule_daily_rebuild(queue, now=lambda: now)
        schedule_daily_rebuild(queue, now=lambda: now)
        jobs = queue.find('privacy_registry.rebuild')
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].dedupe_key, 'privacy_registry.rebuild:daily:2026-11-25')
        with registry._transaction() as db:
            run_at = db.execute('SELECT run_at FROM internal_jobs WHERE id = ?', (jobs[0].job_id,)).fetchone()[0]
        self.assertEqual(run_at, datetime(2026, 11, 25, 4, 0, tzinfo=MANILA).timestamp())

    def test_daily_rebuild_runs_at_0400_manila_and_reschedules_next_day(self) -> None:
        registry = SqliteProjectRegistry(self.dir / 't6.sqlite')
        current = [datetime.fromisoformat('2026-11-25T03:30:00+08:00')]
        queue = InternalJobQueue(registry, clock=lambda: current[0].timestamp())
        rebuild = Rebuild(
            NotionSource.from_client(self.fake.client()),
            {'studios': 'ds-studios', 'projects': 'ds-projects'},
            self.output,
            queue=queue,
            now=lambda: current[0],
        )
        worker = Worker(InternalQueueClient(queue), 'main', {'privacy_registry.rebuild': rebuild})

        schedule_daily_rebuild(queue, now=lambda: current[0])
        self.assertFalse(worker.run_once())

        current[0] = datetime.fromisoformat('2026-11-25T04:00:00+08:00')
        self.assertTrue(worker.run_once())
        self.assertEqual(json.loads(self.output.read_text(encoding='utf-8'))['projects'], [{'name': 'Show One'}])

        jobs = queue.find('privacy_registry.rebuild')
        self.assertEqual([j.dedupe_key for j in jobs], ['privacy_registry.rebuild:daily:2026-11-25', 'privacy_registry.rebuild:daily:2026-11-26'])
        self.assertEqual([j.state for j in jobs], ['done', 'queued'])
        with registry._transaction() as db:
            next_run_at = db.execute('SELECT run_at FROM internal_jobs WHERE id = ?', (jobs[1].job_id,)).fetchone()[0]
        self.assertEqual(next_run_at, datetime(2026, 11, 26, 4, 0, tzinfo=MANILA).timestamp())

        self.fake.add_page('ds-projects', {'Name': title('Show Two')})
        current[0] = datetime.fromisoformat('2026-11-26T04:00:00+08:00')
        self.assertTrue(worker.run_once())
        self.assertEqual(len(json.loads(self.output.read_text(encoding='utf-8'))['projects']), 2)
        self.assertEqual(
            [j.dedupe_key for j in queue.find('privacy_registry.rebuild')],
            ['privacy_registry.rebuild:daily:2026-11-25', 'privacy_registry.rebuild:daily:2026-11-26', 'privacy_registry.rebuild:daily:2026-11-27'],
        )


if __name__ == '__main__':
    unittest.main()
