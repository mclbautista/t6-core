"""Queue behaviour through its public interface, on a fake clock."""

from pathlib import Path
import tempfile
import unittest

from t6.server import JobQueue
from t6.server.testing import FakeClock


class QueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "queue.sqlite3"
        self.clock = FakeClock()
        self.queue = JobQueue(self.path, self.clock)

    def test_same_dedupe_key_makes_one_job(self) -> None:
        first = self.queue.submit("main", "tap", "tap:1", {"a": 1})
        second = self.queue.submit("main", "tap", "tap:1", {"a": 1})
        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.job_id, second.job_id)
        self.assertIsNotNone(self.queue.claim("main", 60))
        self.assertIsNone(self.queue.claim("main", 60))

    def test_same_key_on_another_queue_is_another_job(self) -> None:
        a = self.queue.submit("main", "x", "k")
        b = self.queue.submit("frameio", "x", "k")
        self.assertNotEqual(a.job_id, b.job_id)

    def test_lease_expires_and_job_is_claimed_again(self) -> None:
        self.queue.submit("main", "x", "k", {"n": 1})
        first = self.queue.claim("main", 60)
        assert first is not None
        self.assertIsNone(self.queue.claim("main", 60))  # leased
        self.clock.advance(61)  # the worker died
        second = self.queue.claim("main", 60)
        assert second is not None
        self.assertEqual(second.job_id, first.job_id)
        self.assertEqual(second.attempt, 2)
        self.assertNotEqual(second.token, first.token)
        # The dead worker's late ack does not finish the job; the new holder's does.
        self.assertFalse(self.queue.ack(first.job_id, first.token))
        self.assertTrue(self.queue.ack(second.job_id, second.token))

    def test_extend_keeps_a_lease_alive(self) -> None:
        self.queue.submit("main", "x", "k")
        claim = self.queue.claim("main", 60)
        assert claim is not None
        self.clock.advance(50)
        self.assertTrue(self.queue.extend(claim.job_id, claim.token, 60))
        self.clock.advance(50)
        self.assertIsNone(self.queue.claim("main", 60))

    def test_job_that_keeps_losing_its_lease_is_failed(self) -> None:
        queue = JobQueue(self.path, self.clock, max_attempts=2)
        made = queue.submit("main", "x", "poison")
        for _ in range(2):
            self.assertIsNotNone(queue.claim("main", 10))
            self.clock.advance(11)
        self.assertIsNone(queue.claim("main", 10))
        result = queue.result(made.job_id, "main")
        assert result is not None
        self.assertEqual(result.status, "failed")

    def test_fail_with_retry_waits_then_runs_again(self) -> None:
        self.queue.submit("main", "x", "k")
        claim = self.queue.claim("main", 60)
        assert claim is not None
        self.assertTrue(self.queue.fail(claim.job_id, claim.token, "boom", retry_in=30))
        self.assertIsNone(self.queue.claim("main", 60))
        self.clock.advance(31)
        self.assertIsNotNone(self.queue.claim("main", 60))

    def test_queues_are_separate(self) -> None:
        self.queue.submit("frameio", "x", "k")
        self.assertIsNone(self.queue.claim("main", 60))
        self.assertIsNotNone(self.queue.claim("frameio", 60))

    def test_result_is_readable_by_the_submitter(self) -> None:
        made = self.queue.submit("frameio", "frameio.request", "req-1", {"want": "links"})
        self.assertIsNone(self.queue.result(made.job_id, "frameio"))
        claim = self.queue.claim("frameio", 60)
        assert claim is not None
        self.queue.ack(claim.job_id, claim.token, {"links": ["a"]})
        result = self.queue.result(made.job_id, "frameio")
        assert result is not None
        self.assertEqual((result.status, result.value), ("ok", {"links": ["a"]}))
        self.assertIsNone(self.queue.result(made.job_id, "main"))  # not this queue's job

    def test_job_not_yet_due_is_not_claimed(self) -> None:
        self.queue.submit("main", "x", "k", run_at=self.clock() + 100)
        self.assertIsNone(self.queue.claim("main", 60))
        self.clock.advance(100)
        self.assertIsNotNone(self.queue.claim("main", 60))

    def test_jobs_and_leases_survive_a_restart(self) -> None:
        self.queue.submit("main", "x", "k")
        claim = self.queue.claim("main", 60)
        assert claim is not None
        restarted = JobQueue(self.path, self.clock)
        self.assertIsNone(restarted.claim("main", 60))
        self.assertTrue(restarted.ack(claim.job_id, claim.token))


class TimerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "queue.sqlite3"
        self.clock = FakeClock()
        self.queue = JobQueue(self.path, self.clock)

    def test_timer_fires_after_the_quiet_period_with_a_count(self) -> None:
        self.queue.arm_timer("main", "review:p1", "frameio.review_done", 900, {"p": "p1"})
        self.clock.advance(600)
        self.queue.arm_timer("main", "review:p1", "frameio.review_done", 900, {"p": "p1"})
        self.clock.advance(600)  # 1200 s after the first, 600 after the second
        self.assertIsNone(self.queue.claim("main", 60))
        self.clock.advance(300)
        claim = self.queue.claim("main", 60)
        assert claim is not None
        self.assertEqual(claim.kind, "frameio.review_done")
        self.assertEqual(dict(claim.payload), {"p": "p1", "count": 2})

    def test_timer_survives_a_server_restart(self) -> None:
        self.queue.arm_timer("main", "review:p1", "frameio.review_done", 900)
        self.clock.advance(500)
        restarted = JobQueue(self.path, self.clock)
        self.assertIsNone(restarted.claim("main", 60))
        self.clock.advance(401)
        again = JobQueue(self.path, self.clock)  # a second restart, after it is due
        self.assertIsNotNone(again.claim("main", 60))

    def test_timer_fires_once_and_can_start_again(self) -> None:
        self.queue.arm_timer("main", "k", "x", 10)
        self.clock.advance(10)
        self.assertEqual(self.queue.promote_timers(), 1)
        self.assertEqual(self.queue.promote_timers(), 0)
        self.queue.arm_timer("main", "k", "x", 10)
        self.clock.advance(10)
        self.assertEqual(self.queue.promote_timers(), 1)
        self.assertIsNotNone(self.queue.claim("main", 60))
        self.assertIsNotNone(self.queue.claim("main", 60))

    def test_redelivered_event_does_not_reset_the_timer(self) -> None:
        self.assertTrue(self.queue.arm_timer("main", "k", "x", 100, event_key="c1"))
        self.clock.advance(60)
        self.assertFalse(self.queue.arm_timer("main", "k", "x", 100, event_key="c1"))
        self.clock.advance(41)
        claim = self.queue.claim("main", 60)
        assert claim is not None
        self.assertEqual(claim.payload["count"], 1)


if __name__ == "__main__":
    unittest.main()
