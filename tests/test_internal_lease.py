import tempfile
import time
import unittest
from pathlib import Path

from t6.server.testing import FakeClock
from t6.worker.internal_queue import InternalJobQueue, InternalQueueClient
from t6.worker.runner import Worker
from t6.worker.client import WorkerJob


class InternalLeaseTests(unittest.TestCase):
    def test_expired_claim_cannot_ack_or_fail_before_reclaim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            clock = FakeClock()
            queue = InternalJobQueue(Path(directory) / "db", clock)
            queue.submit("main", "x", "one")
            claim = queue.claim("main", 1)
            assert claim is not None
            clock.advance(1)
            self.assertFalse(queue.ack(claim.job_id, claim.token))
            self.assertFalse(queue.fail(claim.job_id, claim.token, "late"))
            self.assertIsNotNone(queue.claim("main", 1))

    def test_running_handler_renews_lease(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            queue = InternalJobQueue(Path(directory) / "db")
            submitted = queue.submit("main", "x", "one")
            def handler(job: WorkerJob) -> str:
                time.sleep(0.3)
                self.assertIsNone(queue.claim("main", 0.15))
                return "done"
            worker = Worker(InternalQueueClient(queue), "main", {"x": handler}, lease_seconds=0.15)
            self.assertTrue(worker.run_once())
            result = queue.result(submitted.job_id, "main")
            assert result is not None
            self.assertEqual((result.status, result.value), ("ok", "done"))
