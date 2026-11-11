"""Diagnostic worker exercises the queue without consuming real work."""

from pathlib import Path
import tempfile
import threading
import unittest

from t6.registry import SqliteProjectRegistry
from t6.server import JobQueue, ServerConfig, WebhookApp
from t6.server.testing import FakeClock
from t6.worker import QueueClient, QueueRejected
from t6.worker.__main__ import main
from test_server_app import InProcess


class StopOnIdle(threading.Event):
    def wait(self, timeout: float | None = None) -> bool:
        self.set()
        return True


class DiagnosticQueueTests(unittest.TestCase):
    def test_diagnostic_secret_cannot_be_shared_with_another_role(self) -> None:
        for other in ("T6_WORKER_SECRET", "T6_FRAMEIO_WORKER_SECRET", "T6_REGISTRY_SECRET", "T6_REGISTRY_READ_SECRET"):
            with self.subTest(other=other), self.assertRaisesRegex(ValueError, "T6_DIAGNOSTIC_SECRET"):
                ServerConfig.from_env({"T6_DIAGNOSTIC_SECRET": "shared", other: "shared"})

    def test_diagnostic_poll_does_not_dispatch_registry_work(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            clock = FakeClock()
            queue = JobQueue(Path(directory) / "queue.sqlite", clock)
            registry = SqliteProjectRegistry(Path(directory) / "registry.sqlite", clock)
            app = WebhookApp(queue, ServerConfig.from_env({"T6_DIAGNOSTIC_SECRET": "secret"}), clock, registry=registry)
            registry.register(actor="test", idempotency_key="register", notion_page_id="0123456789abcdef0123456789abcdef")
            self.assertTrue(registry.undispatched_sync())
            self.assertEqual(main({"T6_SERVER_URL": "http://unused", "T6_WORKER_SECRET": "secret", "T6_WORKER_QUEUE": "diagnostic"}, transport=InProcess(app), stop=StopOnIdle()), 0)
            self.assertTrue(registry.undispatched_sync())

    def test_diagnostic_poll_does_not_promote_real_queue_timers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            clock = FakeClock()
            queue = JobQueue(Path(directory) / "queue.sqlite", clock)
            queue.arm_timer("main", "review", "frameio.review_done", 0)
            app = WebhookApp(queue, ServerConfig.from_env({"T6_DIAGNOSTIC_SECRET": "secret"}), clock)
            self.assertEqual(main({"T6_SERVER_URL": "http://unused", "T6_WORKER_SECRET": "secret", "T6_WORKER_QUEUE": "diagnostic"}, transport=InProcess(app), stop=StopOnIdle()), 0)
            self.assertEqual(queue.promote_timers(), 1)

    def test_diagnostic_worker_completes_echo_and_leaves_real_jobs_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            clock = FakeClock()
            queue = JobQueue(Path(directory) / "queue.sqlite", clock)
            config = ServerConfig.from_env({"T6_DIAGNOSTIC_SECRET": "diagnostic-secret"})
            transport = InProcess(WebhookApp(queue, config, clock))
            client = QueueClient(transport, "diagnostic-secret")
            main_job = queue.submit("main", "notion.page_changed", "main-job")
            frameio_job = queue.submit("frameio", "frameio.request", "frameio-job")
            diagnostic_job = client.submit("diagnostic", "diagnostic.echo", "probe", {"message": "hello"})
            code = main({"T6_SERVER_URL": "http://unused", "T6_WORKER_SECRET": "diagnostic-secret", "T6_WORKER_QUEUE": "diagnostic"}, transport=transport, stop=StopOnIdle())
            self.assertEqual(code, 0)
            self.assertEqual(client.result("diagnostic", diagnostic_job), ("ok", {"message": "hello"}))
            for name, job in (("main", main_job), ("frameio", frameio_job)):
                self.assertIsNone(queue.result(job.job_id, name))
                claim = queue.claim(name, 60)
                assert claim is not None
                self.assertEqual((claim.job_id, claim.attempt), (job.job_id, 1))
                with self.assertRaises(QueueRejected):
                    client.claim(name)
