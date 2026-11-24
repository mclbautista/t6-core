"""`python -m t6.worker`: settings, refusal to start, and the poll loop."""

from collections.abc import Mapping
import contextlib
import io
import threading
import unittest

from t6.domain.events import JsonValue
from t6.worker import QueueClient, WorkerJob

from t6.worker.__main__ import WorkerSettings, configured_handlers, main
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

    def test_the_main_worker_rebuilds_the_privacy_registry_only_when_the_file_and_data_sources_are_set(self) -> None:
        import tempfile
        from pathlib import Path

        settings = WorkerSettings("http://server.invalid:8081", WORKER_SECRET, "main")
        with tempfile.TemporaryDirectory() as folder:
            sources = Path(folder) / "sources.json"
            sources.write_text('{"studios": "ds-studios", "projects": "ds-projects"}', encoding="utf-8")
            base = {"T6_NOTION_TOKEN": "token"}
            full = {**base, "T6_READ_PRIVACY_REGISTRY": str(Path(folder) / "privacy_registry.json"), "T6_READ_DATA_SOURCES": str(sources)}
            transport = InProcess(self.app)
            self.assertIn("privacy_registry.rebuild", configured_handlers(full, settings, transport).get("main", {}))
            self.assertNotIn("privacy_registry.rebuild", configured_handlers(base, settings, transport).get("main", {}))

    def test_the_main_worker_with_the_t6_database_handles_share_views(self) -> None:
        import tempfile
        from pathlib import Path

        submitter = QueueClient(InProcess(self.app), WORKER_SECRET)
        body: dict[str, JsonValue] = {"type": "share.viewed", "resource": {"id": "share-nobody-delivered", "type": "share"}}
        job_id = submitter.submit("main", "frameio.share_viewed", "edge:/webhook/frameio/shares:event-1", body)
        with tempfile.TemporaryDirectory() as folder:
            code, _ = run_main(
                {**ENVIRON, "T6_API_URL": "http://api.invalid", "T6_DATABASE": str(Path(folder) / "t6.sqlite")},
                None,  # type: ignore[arg-type]
                transport=InProcess(self.app),
                stop=RecordingStop(),
            )
        self.assertEqual(code, 0)
        self.assertEqual(submitter.result("main", job_id), ("ok", {"recorded": False}))

    def test_a_stop_before_the_first_poll_claims_nothing(self) -> None:
        submitter = QueueClient(InProcess(self.app), WORKER_SECRET)
        job_id = submitter.submit("main", "echo", "k1")
        stop = threading.Event()
        stop.set()
        code, _ = run_main(ENVIRON, {"main": {"echo": noop}}, transport=InProcess(self.app), stop=stop)
        self.assertEqual(code, 0)
        self.assertIsNone(submitter.result("main", job_id))

    def test_the_main_worker_registers_chat_link_reminder_unavailable_without_gateway(self) -> None:
        import tempfile
        from pathlib import Path
        from t6.registry.sqlite import SqliteProjectRegistry
        from t6.worker.internal_queue import InternalJobQueue

        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "t6.sqlite"
            registry = SqliteProjectRegistry(db_path)
            with registry._transaction() as db:
                db.execute("INSERT INTO approvals VALUES ('t6a_1', 'chat link', 'Show [S1] t6p_1: chat -100, topic 12', 't6n_1', '\"permission:operator\"', 'requested', NULL, NULL, NULL)")
            queue = InternalJobQueue(registry, clock=lambda: 100.0)
            submitted = queue.submit("main", "chat_link_reminder", "t6a_1", {"approval": "t6a_1"}, run_at=0.0)
            code, _ = run_main(
                {**ENVIRON, "T6_API_URL": "http://api.invalid", "T6_DATABASE": str(db_path)},
                None,  # type: ignore[arg-type]
                transport=InProcess(self.app),
                stop=RecordingStop(),
            )
            self.assertEqual(code, 0)
            job = queue.get(submitted.job_id)
            assert job is not None
            self.assertEqual(job.attempts, 1)
            self.assertEqual(job.state, "queued")

    def test_the_main_worker_with_gateway_wires_chat_link_reminder(self) -> None:
        import json
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from t6.registry.sqlite import SqliteProjectRegistry
        from t6.worker.internal_queue import InternalJobQueue

        sent: list[dict[str, object]] = []

        class GatewayTransport:
            def send(self, method: str, path: str, headers: Mapping[str, str], body: bytes) -> tuple[int, dict[str, object]]:
                if path == "/notify":
                    sent.append(json.loads(body))
                    return 202, {"message_ref": 1}
                if path == "/subscriptions":
                    return 200, {}
                if path.startswith("/updates"):
                    return 200, {"items": []}
                return 404, {}

        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "t6.sqlite"
            registry = SqliteProjectRegistry(db_path)
            with registry._transaction() as db:
                db.execute("INSERT INTO approvals VALUES ('t6a_1', 'chat link', 'Show [S1] t6p_1: chat -100, topic 12', 't6n_1', '\"permission:operator\"', 'requested', NULL, NULL, NULL)")
                db.execute("INSERT INTO bindings (system, page_id, project_id, bound_at, active) VALUES ('notion', ?, 't6n_1', 1.0, 1)", ("c" * 32,))
            queue = InternalJobQueue(registry, clock=lambda: 100.0)
            submitted = queue.submit("main", "chat_link_reminder", "t6a_1", {"approval": "t6a_1"}, run_at=0.0)
            with patch("t6.worker.__main__.HttpTransport", return_value=GatewayTransport()), patch(
                "t6.notion.reads.ContactReader.get",
                return_value=type("Contact", (), {"name": "Jane Doe"})(),
            ):
                code, _ = run_main(
                    {
                        **ENVIRON,
                        "T6_API_URL": "http://api.invalid",
                        "T6_DATABASE": str(db_path),
                        "T6_GATEWAY_URL": "http://gateway.invalid",
                        "T6_GATEWAY_SECRET": "gw-secret",
                        "T6_REVIEW_CHAT": "-999",
                        "T6_OPERATOR_IDS": "900",
                        "T6_NOTION_TOKEN": "notion-token",
                    },
                    None,  # type: ignore[arg-type]
                    transport=InProcess(self.app),
                    stop=RecordingStop(),
                )
            from t6.server.queue import JobResult
            self.assertEqual(code, 0)
            self.assertEqual(queue.result(submitted.job_id, "main"), JobResult("ok", True))
            self.assertEqual(len(sent), 1)
            self.assertEqual(sent[0]["to"], 900)
            self.assertIn("Jane Doe (t6n_1)", str(sent[0]["text"]))


if __name__ == "__main__":
    unittest.main()
