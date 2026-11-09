"""`python -m t6.worker`: settings, refusal to start, and the poll loop."""

from collections.abc import Mapping
import contextlib
import io
import threading
import unittest

from t6.worker import QueueClient, WorkerJob
from t6.worker.__main__ import main
from t6.worker.runner import Handler
from test_server_app import WORKER_SECRET, InProcess, ServerTestCase

ENVIRON = {
    "T6_SERVER_URL": "http://server.invalid:8081",
    "T6_WORKER_SECRET": WORKER_SECRET,
    "T6_WORKER_QUEUE": "main",
}


class RecordingStop(threading.Event):
    """A stop flag whose wait returns at once, so a test never sleeps."""

    def __init__(self) -> None:
        super().__init__()
        self.waits: list[float | None] = []

    def wait(self, timeout: float | None = None) -> bool:
        self.waits.append(timeout)
        self.set()
        return True


def run_main(
    environ: Mapping[str, str], handlers: Mapping[str, Mapping[str, object]], **kwargs: object
) -> tuple[int, str]:
    errors = io.StringIO()
    with contextlib.redirect_stderr(errors):
        code = main(environ, handlers, **kwargs)  # type: ignore[arg-type]
    return code, errors.getvalue()


def noop(job: WorkerJob) -> None:
    return None


class StartupTests(unittest.TestCase):
    def test_missing_variable_exits_non_zero_and_names_it(self) -> None:
        for name in ENVIRON:
            with self.subTest(name=name):
                environ = {key: value for key, value in ENVIRON.items() if key != name}
                code, message = run_main(environ, {"main": {"x": noop}})
                self.assertNotEqual(code, 0)
                self.assertIn(name, message)

    def test_no_handler_for_the_queue_exits_non_zero_and_says_so(self) -> None:
        tables: list[dict[str, dict[str, Handler]]] = [{}, {"main": {}}, {"frameio": {"x": noop}}]
        for handlers in tables:
            with self.subTest(handlers=handlers):
                code, message = run_main(ENVIRON, handlers)
                self.assertNotEqual(code, 0)
                self.assertIn("no handler is registered for queue 'main'", message)

    def test_the_default_handler_table_is_empty_so_the_worker_refuses_to_start(self) -> None:
        code, message = run_main(ENVIRON, None)  # type: ignore[arg-type]
        self.assertNotEqual(code, 0)
        self.assertIn("no handler", message)

    def test_bad_poll_interval_exits_non_zero(self) -> None:
        for value in ("soon", "0", "-1"):
            with self.subTest(value=value):
                code, message = run_main({**ENVIRON, "T6_WORKER_POLL_SECONDS": value}, {"main": {"x": noop}})
                self.assertNotEqual(code, 0)
                self.assertIn("T6_WORKER_POLL_SECONDS", message)


class LoopTests(ServerTestCase):
    def test_one_iteration_runs_a_job_then_sleeps_on_the_empty_poll_and_stops(self) -> None:
        submitter = QueueClient(InProcess(self.app), WORKER_SECRET)
        job_id = submitter.submit("main", "echo", "k1", {"n": 1})
        seen: list[WorkerJob] = []

        def echo(job: WorkerJob) -> dict[str, int]:
            seen.append(job)
            return {"n": 2}

        stop = RecordingStop()
        code, _ = run_main(
            {**ENVIRON, "T6_WORKER_POLL_SECONDS": "7"},
            {"main": {"echo": echo}},
            transport=InProcess(self.app),
            stop=stop,
        )
        self.assertEqual(code, 0)
        self.assertEqual([job.payload for job in seen], [{"n": 1}])
        self.assertEqual(submitter.result("main", job_id), ("ok", {"n": 2}))
        self.assertEqual(stop.waits, [7.0])

    def test_a_stop_before_the_first_poll_claims_nothing(self) -> None:
        submitter = QueueClient(InProcess(self.app), WORKER_SECRET)
        job_id = submitter.submit("main", "echo", "k1")
        stop = threading.Event()
        stop.set()
        code, _ = run_main(ENVIRON, {"main": {"echo": noop}}, transport=InProcess(self.app), stop=stop)
        self.assertEqual(code, 0)
        self.assertIsNone(submitter.result("main", job_id))


if __name__ == "__main__":
    unittest.main()
