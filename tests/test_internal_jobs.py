from typing import Any
import sqlite3
from contextlib import closing
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
        with closing(sqlite3.connect(self.path)) as db, db:
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


class JobLookupTests(unittest.TestCase):
    """`get`, `find`, `finish` and `settle`: what callers need so they never read the jobs table themselves."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.now = [100.0]
        self.queue = InternalJobQueue(directory.name + '/db', clock=lambda: self.now[0], max_attempts=1)

    def test_get_returns_the_job_only_for_the_kind_and_queue_asked_for(self) -> None:
        job = self.queue.submit('q', 'a.kind', 'k1', {'who': 'x'})
        record = self.queue.get(job.job_id, kind='a.kind', queue='q')
        assert record is not None
        self.assertEqual((record.payload, record.state, record.lease_token, record.result), ({'who': 'x'}, 'queued', None, None))
        self.assertIsNone(self.queue.get(job.job_id, kind='other'))
        self.assertIsNone(self.queue.get(job.job_id, queue='other'))
        self.assertIsNone(self.queue.get(999))

    def test_find_narrows_by_state_later_id_and_payload(self) -> None:
        first = self.queue.submit('q', 'a.kind', 'k1', {'t': '1'}).job_id
        second = self.queue.submit('q', 'a.kind', 'k2', {'t': '2'}).job_id
        self.queue.submit('q', 'b.kind', 'k3', {'t': '1'})
        self.assertEqual([j.job_id for j in self.queue.find('a.kind')], [first, second])
        self.assertEqual([j.job_id for j in self.queue.find('a.kind', payload={'t': '2'})], [second])
        self.assertEqual([j.job_id for j in self.queue.find('a.kind', after=first)], [second])
        self.assertEqual(self.queue.find('a.kind', states=['dead']), [])

    def test_finish_stores_the_result_as_given_and_settle_is_once_only_for_a_dead_job(self) -> None:
        job = self.queue.submit('q', 'a.kind', 'k1').job_id
        self.queue.finish(job, {'outcome': 'done'})
        record = self.queue.get(job)
        assert record is not None
        self.assertEqual((record.state, record.result), ('done', {'outcome': 'done'}))
        self.assertFalse(self.queue.settle(job))  # not dead
        dead = self.queue.submit('q', 'a.kind', 'k2').job_id
        self.queue.claim('q', 10)  # the finished job is not claimable, so this takes `dead`
        self.now[0] += 20
        self.queue.claim('q', 10)  # its lease expired at the last allowed attempt: dead
        self.assertEqual([j.job_id for j in self.queue.find('a.kind', states=['dead'])], [dead])
        self.assertTrue(self.queue.settle(dead))
        self.assertFalse(self.queue.settle(dead))


class OneWriterOfInternalJobs(unittest.TestCase):
    def test_only_the_internal_job_queue_inserts_into_the_job_table(self) -> None:
        from pathlib import Path
        root = Path(__file__).parents[1] / 'src' / 't6'
        writers = sorted(str(path.relative_to(root)) for path in root.rglob('*.py') if 'INSERT INTO internal_jobs' in path.read_text(encoding='utf-8'))
        self.assertEqual(writers, ['worker/internal_queue.py'])
