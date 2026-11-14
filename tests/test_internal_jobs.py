from typing import Any
import sqlite3
import tempfile
import unittest
from pathlib import Path

from t6.server.testing import FakeClock
from t6.worker import QueueClient, Worker
from t6.worker.internal_queue import InternalJobQueue, InternalQueueClient
from test_server_app import InProcess, ServerTestCase, WORKER_SECRET


class InternalJobsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 't6.sqlite'
        self.clock = FakeClock()
        self.queue = InternalJobQueue(self.path, self.clock, max_attempts=2)

    def test_submit_dedupes_by_kind_and_retains_original(self) -> None:
        first = self.queue.submit('main', 'transcribe', 'key', {'n': 1})
        duplicate = self.queue.submit('other', 'transcribe', 'key', {'n': 2})
        self.assertEqual(first.job_id, duplicate.job_id)
        self.assertFalse(duplicate.created)
        self.assertNotEqual(first.job_id, self.queue.submit('main', 'other', 'key').job_id)
        claim = self.queue.claim('main', 60)
        assert claim is not None
        self.assertEqual(claim.payload, {'n': 1})

    def test_lease_renewal_expiry_and_completion(self) -> None:
        job = self.queue.submit('main', 'x', 'k')
        first = self.queue.claim('main', 60)
        assert first is not None
        self.clock.advance(50)
        self.assertTrue(self.queue.extend(first.job_id, first.token, 60))
        self.clock.advance(50)
        self.assertIsNone(self.queue.claim('main', 60))
        self.clock.advance(10)
        second = self.queue.claim('main', 60)
        assert second is not None
        self.assertEqual(second.attempt, 2)
        self.assertFalse(self.queue.ack(first.job_id, first.token))
        self.assertTrue(self.queue.ack(second.job_id, second.token, {'ok': True}))
        result = self.queue.result(job.job_id, 'main')
        assert result is not None
        self.assertEqual(result.value, {'ok': True})

    def test_failure_backoff_and_dead_limit(self) -> None:
        job = self.queue.submit('main', 'x', 'k')
        claim = self.queue.claim('main', 10)
        assert claim is not None
        self.assertTrue(self.queue.fail(claim.job_id, claim.token, 'error', retry_in=1))
        self.assertIsNone(self.queue.claim('main', 10))
        self.clock.advance(30)
        claim = self.queue.claim('main', 10)
        assert claim is not None
        self.assertTrue(self.queue.fail(claim.job_id, claim.token, 'again', retry_in=1))
        result = self.queue.result(job.job_id, 'main')
        assert result is not None
        self.assertEqual(result.value, 'again')
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT state FROM internal_jobs').fetchone()[0], 'dead')

    def test_expired_final_lease_goes_dead(self) -> None:
        job = self.queue.submit('main', 'x', 'k')
        for _ in range(2):
            self.assertIsNotNone(self.queue.claim('main', 10))
            self.clock.advance(10)
        self.assertIsNone(self.queue.claim('main', 10))
        result = self.queue.result(job.job_id, 'main')
        assert result is not None
        self.assertEqual(result.status, 'failed')


class MixedWorkerTests(ServerTestCase):
    def test_worker_polls_both_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            local = InternalJobQueue(Path(directory) / 't6.sqlite', self.clock)
            remote = QueueClient(InProcess(self.app), WORKER_SECRET)
            remote.submit('main', 'x', 'remote', {'source': 'remote'})
            local.submit('main', 'x', 'local', {'source': 'local'})
            seen = []
            worker = Worker(remote, 'main', {'x': lambda job: seen.append(job.payload['source'])}, internal_client=InternalQueueClient(local))
            self.assertTrue(worker.run_once())
            self.assertTrue(worker.run_once())
            self.assertEqual(seen, ['remote', 'local'])

    def test_remote_outage_does_not_block_internal_work(self) -> None:
        class Down:
            def send(self, *args: Any) -> Any:
                raise OSError('offline')
        with tempfile.TemporaryDirectory() as directory:
            local = InternalJobQueue(Path(directory) / 't6.sqlite', self.clock)
            local.submit('main', 'x', 'local')
            seen = []
            worker = Worker(QueueClient(Down(), WORKER_SECRET), 'main', {'x': lambda job: seen.append(job.kind)}, internal_client=InternalQueueClient(local))
            self.assertTrue(worker.run_once())
            self.assertEqual(seen, ['x'])
