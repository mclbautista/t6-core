"""serve() keeps a worker alive while its queue is unreachable."""
from collections.abc import Callable
import threading
import unittest
from urllib.error import URLError

from t6.worker.__main__ import INTERNAL_POLL_SECONDS, serve
from t6.worker.client import QueueRejected, WorkerJob
from t6.worker.runner import Worker


class FlakyWorker:
    def __init__(self, failures: list[BaseException], stop: threading.Event) -> None:
        self.failures, self.stop, self.calls = failures, stop, 0

    def run_once(self) -> bool:
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        self.stop.set()
        return False


class RecordingStop(threading.Event):
    def __init__(self) -> None:
        super().__init__()
        self.waits: list[float] = []

    def wait(self, timeout: float | None = None) -> bool:
        self.waits.append(float(timeout or 0))
        return self.is_set()


class ServeTests(unittest.TestCase):
    def test_an_unreachable_queue_is_retried_with_growing_waits(self) -> None:
        stop = RecordingStop()
        worker = FlakyWorker([URLError(ConnectionRefusedError(111, "refused")), ConnectionRefusedError(111, "refused"),
                              TimeoutError("timed out")], stop)
        serve(worker, stop, 2.0)  # type: ignore[arg-type]
        self.assertEqual(worker.calls, 4)
        self.assertEqual(stop.waits[:3], [2.0, 4.0, 8.0])

    def test_the_wait_is_capped(self) -> None:
        stop = RecordingStop()
        worker = FlakyWorker([OSError("down")] * 8, stop)
        serve(worker, stop, 10.0)  # type: ignore[arg-type]
        self.assertEqual(max(stop.waits), 60.0)

    def test_a_rejected_secret_still_stops_the_worker(self) -> None:
        stop = RecordingStop()
        worker = FlakyWorker([QueueRejected(401, "bad secret")], stop)
        with self.assertRaises(QueueRejected):
            serve(worker, stop, 2.0)  # type: ignore[arg-type]


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class ClockStop(threading.Event):
    """A stop event whose waits move a fake clock instead of sleeping; `on_wait` runs after each wait."""

    def __init__(self, clock: FakeClock) -> None:
        super().__init__()
        self.clock = clock
        self.on_wait: list[Callable[[], None]] = []

    def wait(self, timeout: float | None = None) -> bool:
        self.clock.now += float(timeout or 0)
        for action in list(self.on_wait):
            action()
        return self.is_set()


class FakeQueue:
    def __init__(self, jobs: list[WorkerJob] | None = None) -> None:
        self.jobs, self.claims = list(jobs or []), 0
        self.acked: list[WorkerJob] = []
        self.error: BaseException | None = None

    def claim(self, queue, lease_seconds=60.0, kinds=None):  # type: ignore[no-untyped-def]
        self.claims += 1
        if self.error is not None:
            raise self.error
        return self.jobs.pop(0) if self.jobs else None

    def ack(self, job, result=None):  # type: ignore[no-untyped-def]
        self.acked.append(job)

    def fail(self, job, reason, retry_in=None):  # type: ignore[no-untyped-def]
        raise AssertionError(reason)

    def extend(self, job, lease_seconds=60.0):  # type: ignore[no-untyped-def]
        pass


def job(number: int) -> WorkerJob:
    return WorkerJob(number, "main", "x", f"k{number}", {}, "token", 1)


class InternalChecksTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.stop = ClockStop(self.clock)
        self.remote, self.internal = FakeQueue(), FakeQueue()
        self.worker = Worker(self.remote, "main", {"x": lambda j: None}, internal_client=self.internal)

    def run_serve(self, poll: float = 5.0, until: float = 12.0) -> None:
        def end() -> None:
            if self.clock.now >= until:
                self.stop.set()

        self.stop.on_wait.append(end)
        serve(self.worker, self.stop, poll, clock=self.clock)

    def test_a_job_submitted_during_the_wait_runs_within_one_internal_poll(self) -> None:
        ran_at: list[float] = []
        self.worker = Worker(self.remote, "main", {"x": lambda j: ran_at.append(self.clock.now)}, internal_client=self.internal)

        def submit() -> None:
            if self.clock.now == 1.0 and not self.internal.jobs and not ran_at:
                self.internal.jobs.append(job(1))

        self.stop.on_wait.append(submit)
        self.run_serve()
        self.assertEqual(len(ran_at), 1)
        self.assertLessEqual(ran_at[0] - 1.0, INTERNAL_POLL_SECONDS)

    def test_the_remote_queue_is_asked_once_per_poll_seconds(self) -> None:
        self.run_serve(poll=5.0, until=12.0)
        self.assertEqual(self.remote.claims, 3)  # at 0, 5 and 10
        self.assertGreaterEqual(self.internal.claims, 40)

    def test_several_internal_jobs_all_run_during_one_wait(self) -> None:
        def submit() -> None:
            if self.clock.now == 1.0 and not self.internal.acked:
                self.internal.jobs.extend([job(1), job(2), job(3)])

        self.stop.on_wait.append(submit)
        self.run_serve(until=4.0)
        self.assertEqual([j.job_id for j in self.internal.acked], [1, 2, 3])
        self.assertEqual(self.remote.claims, 1)

    def test_without_an_internal_client_the_wait_is_one_poll_seconds_sleep(self) -> None:
        self.worker = Worker(self.remote, "main", {"x": lambda j: None})
        self.run_serve(poll=5.0, until=11.0)
        self.assertEqual(self.remote.claims, 3)

    def test_stop_during_the_wait_ends_serve_at_once(self) -> None:
        self.stop.on_wait.append(self.stop.set)
        serve(self.worker, self.stop, 5.0, clock=self.clock)
        self.assertLess(self.clock.now, 1.0)

    def test_an_internal_table_error_does_not_end_the_worker(self) -> None:
        self.internal.error = OSError("disk")
        self.run_serve(until=12.0)
        self.assertEqual(self.remote.claims, 3)
        self.assertEqual(self.internal.claims, 6)  # per wait: the poll in run_once and one failed short check, then the plain wait

    def test_run_internal_once_without_an_internal_client_claims_nothing(self) -> None:
        worker = Worker(self.remote, "main", {"x": lambda j: None})
        self.assertFalse(worker.run_internal_once())
        self.assertEqual(self.remote.claims, 0)

    def test_run_internal_once_runs_a_job_from_the_internal_client_only(self) -> None:
        self.internal.jobs.append(job(1))
        self.remote.jobs.append(job(9))
        self.assertTrue(self.worker.run_internal_once())
        self.assertEqual([j.job_id for j in self.internal.acked], [1])
        self.assertEqual(self.remote.claims, 0)
        self.assertFalse(self.worker.run_internal_once())


if __name__ == "__main__":
    unittest.main()
