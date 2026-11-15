"""A worker claims only the job kinds it has handlers for, so a job it cannot run is not lost (#360).

Both queues are real: the edge queue behind the server's queue routes, and the internal job table.
"""
from pathlib import Path
import tempfile
import unittest

from test_server_app import InProcess

from t6.domain.events import JobKind
from t6.server import Grant, JobQueue, ServerConfig, WebhookApp
from t6.server.testing import FakeClock
from t6.worker import QueueClient, Worker
from t6.worker.client import WorkerJob
from t6.worker.internal_queue import InternalJobQueue, InternalQueueClient
from t6.worker.runner import Handler

SECRET = "worker-secret"
KIND = JobKind.FRAMEIO_FILE_VERSIONED.value


class WorkerKindTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.clock = FakeClock()
        main = frozenset({"main"})
        self.edge = JobQueue(Path(directory.name) / "edge.sqlite3", self.clock)
        config = ServerConfig(edge_routes=(), grants=(Grant(SECRET, claim=main, submit=main),))
        self.client = QueueClient(InProcess(WebhookApp(self.edge, config, self.clock)), SECRET)
        self.inside = InternalJobQueue(Path(directory.name) / "t6.sqlite3", self.clock)

    def worker(self, handlers: dict[str, Handler]) -> Worker:
        return Worker(self.client, "main", handlers, internal_client=InternalQueueClient(self.inside))

    def test_a_job_with_no_handler_on_the_edge_queue_is_left_for_a_worker_that_has_one(self) -> None:
        self.edge.submit("main", KIND, "evt-1", {"event": 1})
        seen: list[WorkerJob] = []
        self.assertFalse(self.worker({"other": lambda job: None}).run_once())
        self.assertFalse(self.worker({"other": lambda job: None}).run_once())
        self.assertTrue(self.worker({KIND: seen.append}).run_once())
        (job,) = seen
        self.assertEqual((job.kind, job.attempt), (KIND, 1))  # the first claim: no try was used

    def test_a_job_with_no_handler_in_the_internal_table_is_left_for_a_worker_that_has_one(self) -> None:
        made = self.inside.submit("main", KIND, "evt-2", {"event": 2})
        self.assertFalse(self.worker({"other": lambda job: None}).run_once())
        self.assertIsNone(self.inside.result(made.job_id, "main"))  # neither failed nor dead
        seen: list[WorkerJob] = []
        self.assertTrue(self.worker({KIND: seen.append}).run_once())
        self.assertEqual([job.attempt for job in seen], [1])

    def test_a_job_it_cannot_run_does_not_hide_one_behind_it(self) -> None:
        self.edge.submit("main", KIND, "evt-3")
        self.edge.submit("main", "tap", "tap-1")
        seen: list[WorkerJob] = []
        self.assertTrue(self.worker({"tap": seen.append}).run_once())
        self.assertEqual([job.kind for job in seen], ["tap"])
