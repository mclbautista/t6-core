"""serve() keeps a worker alive while its queue is unreachable."""
import threading
import unittest
from urllib.error import URLError

from t6.worker.__main__ import serve
from t6.worker.client import QueueRejected


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


if __name__ == "__main__":
    unittest.main()
