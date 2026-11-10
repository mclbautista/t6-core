"""Project registry through the private service: register, read back, restart, read again."""

from collections.abc import Mapping
import json
from pathlib import Path
import tempfile
from typing import Any
import unittest

from t6.domain import Project
from t6.registry import PROJECT_ID_PREFIX, SqliteProjectRegistry
from t6.server import Grant, JobQueue, Request, Response, ServerConfig, WebhookApp
from t6.server.testing import FakeClock

WRITER_SECRET = "registry-writer-secret"
READER_SECRET = "registry-reader-secret"
WORKER_SECRET = "worker-secret"
PAGE = "0123456789abcdef0123456789abcdef"
PAGE_DASHED = "01234567-89AB-CDEF-0123-456789ABCDEF"


class RegistryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.clock = FakeClock()
        self.path = Path(self.directory.name) / "registry.sqlite3"
        self.queue = JobQueue(Path(self.directory.name) / "q.sqlite3", self.clock)
        self.config = ServerConfig(
            grants=(
                Grant(WORKER_SECRET, claim=frozenset({"main"}), submit=frozenset({"main"})),
                Grant(WRITER_SECRET, registry_read=True, registry_write=True, actor="writer"),
                Grant(READER_SECRET, registry_read=True, actor="reader"),
            )
        )
        self.start()

    def start(self, **registry_options: Any) -> None:
        """Open the registry file and build the app, as a server start or restart does."""
        self.registry = SqliteProjectRegistry(self.path, self.clock, **registry_options)
        self.app = WebhookApp(self.queue, self.config, self.clock, registry=self.registry)

    def send(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any] | None = None,
        *,
        secret: str = WRITER_SECRET,
        key: str | None = None,
    ) -> Response:
        headers = {"Authorization": f"Bearer {secret}"} if secret else {}
        if key is not None:
            headers["Idempotency-Key"] = key
        raw = b"" if body is None else json.dumps(body).encode()
        return self.app.tailnet(Request(method, path, headers, raw))

    def register(self, key: str = "k1", page: str | None = None, **options: Any) -> Response:
        body = {} if page is None else {"notion_page_id": page}
        return self.send("POST", "/registry/projects", body, key=key, **options)

    def project(self, answer: Response) -> dict[str, Any]:
        found = answer.body["project"]
        assert isinstance(found, dict)
        return found


class RegisterAndReadTests(RegistryTestCase):
    def test_registers_without_a_code_and_reads_it_back_by_id(self) -> None:
        made = self.register()
        self.assertEqual(made.status, 201)
        self.assertIs(made.body["created"], True)
        project = self.project(made)
        self.assertTrue(project["id"].startswith(PROJECT_ID_PREFIX))
        self.assertEqual(project["revision"], 1)
        self.assertIsNone(project["code"])
        self.assertEqual(project["rotation"], "out")
        self.assertEqual(project["bindings"], [])

        read = self.send("GET", f"/registry/projects/{project['id']}")
        self.assertEqual(read.status, 200)
        self.assertEqual(self.project(read), project)

    def test_the_same_project_is_read_after_a_restart(self) -> None:
        project = self.project(self.register(page=PAGE))
        self.start()
        read = self.send("GET", f"/registry/projects/{project['id']}")
        self.assertEqual(self.project(read), project)

    def test_each_registration_is_a_separate_project(self) -> None:
        first = self.project(self.register("k1"))
        second = self.project(self.register("k2"))
        self.assertNotEqual(first["id"], second["id"])

    def test_an_unknown_id_is_not_found(self) -> None:
        self.assertEqual(self.send("GET", f"/registry/projects/{PROJECT_ID_PREFIX}nope").status, 404)

    def test_registration_records_the_bound_page(self) -> None:
        project = self.project(self.register(page=PAGE_DASHED))
        self.assertEqual(
            project["bindings"],
            [{"system": "notion", "page_id": PAGE, "bound_at": self.clock(), "status": "active"}],
        )


class ReplayTests(RegistryTestCase):
    def test_a_repeated_key_returns_the_original_project(self) -> None:
        first = self.register("k1", PAGE)
        self.clock.advance(60)
        again = self.register("k1", PAGE)
        self.assertEqual(again.status, 200)
        self.assertIs(again.body["created"], False)
        self.assertEqual(self.project(again), self.project(first))
        self.assertEqual(len(self.registry.pending_sync()), 1)
        self.assertEqual(len(self.registry.audit(self.project(first)["id"])), 2)

    def test_a_repeated_key_still_returns_the_original_after_a_restart(self) -> None:
        first = self.project(self.register("k1"))
        self.start()
        self.assertEqual(self.project(self.register("k1")), first)

    def test_a_key_reused_for_a_different_request_is_refused(self) -> None:
        self.register("k1")
        clash = self.register("k1", PAGE)
        self.assertEqual(clash.status, 409)
        self.assertEqual(clash.body["error"], "idempotency_key_reused")
        self.assertEqual(len(self.registry.pending_sync()), 1)


class BindingTests(RegistryTestCase):
    def test_a_page_binds_to_at_most_one_project(self) -> None:
        owner = self.project(self.register("k1", PAGE))
        clash = self.register("k2", PAGE_DASHED)
        self.assertEqual(clash.status, 409)
        self.assertEqual(clash.body, {"error": "page_already_bound", "project_id": owner["id"]})
        # The refused request left nothing behind.
        self.assertEqual(len(self.registry.pending_sync()), 1)
        self.assertEqual(self.project(self.send("GET", f"/registry/projects/{owner['id']}")), owner)

    def test_a_failed_registration_leaves_nothing_and_can_be_retried(self) -> None:
        taken = self.project(self.register("k1"))["id"]
        self.start(new_id=lambda: taken)  # the next ID collides, so the write fails part-way
        with self.assertRaises(Exception):
            self.register("k2", PAGE)
        self.assertEqual(len(self.registry.pending_sync()), 1)
        self.assertEqual(len(self.registry.audit(taken)), 1)

        self.start()
        retried = self.register("k2", PAGE)
        self.assertEqual(retried.status, 201)
        self.assertNotEqual(self.project(retried)["id"], taken)


class AuditAndSyncTests(RegistryTestCase):
    def test_registration_and_binding_are_audited_with_actor_and_time(self) -> None:
        project = self.project(self.register(page=PAGE))
        answer = self.send("GET", f"/registry/projects/{project['id']}/audit")
        self.assertEqual(answer.status, 200)
        entries: list[dict[str, Any]] = json.loads(json.dumps(answer.body["entries"]))
        self.assertEqual([entry["action"] for entry in entries], ["registered", "page_bound"])
        for entry in entries:
            self.assertEqual(entry["actor"], "writer")
            self.assertEqual(entry["at"], self.clock())
            self.assertEqual(entry["project_id"], project["id"])

    def test_an_accepted_registration_keeps_its_sync_intent_across_a_restart(self) -> None:
        project = self.project(self.register())
        self.assertEqual(project["sync"], "pending")
        self.start()
        (intent,) = self.registry.pending_sync()
        self.assertEqual((intent.project_id, intent.revision), (project["id"], 1))


class AccessTests(RegistryTestCase):
    def test_the_public_listener_does_not_serve_the_registry(self) -> None:
        headers = {"Authorization": f"Bearer {WRITER_SECRET}", "Idempotency-Key": "k1"}
        answer = self.app.public(Request("POST", "/registry/projects", headers, b"{}"))
        self.assertEqual(answer.status, 404)
        self.assertEqual(self.registry.pending_sync(), ())

    def test_a_missing_or_wrong_secret_is_refused(self) -> None:
        self.assertEqual(self.register(secret="").status, 401)
        self.assertEqual(self.register(secret="wrong").status, 401)
        self.assertEqual(self.send("GET", f"/registry/projects/{PROJECT_ID_PREFIX}x", secret="wrong").status, 401)

    def test_queue_access_grants_no_registry_access(self) -> None:
        project = self.project(self.register())
        self.assertEqual(self.register("k2", secret=WORKER_SECRET).status, 403)
        self.assertEqual(self.send("GET", f"/registry/projects/{project['id']}", secret=WORKER_SECRET).status, 403)
        self.assertEqual(
            self.send("GET", f"/registry/projects/{project['id']}/audit", secret=WORKER_SECRET).status, 403
        )

    def test_a_read_grant_reads_but_does_not_register(self) -> None:
        project = self.project(self.register())
        self.assertEqual(self.register("k2", secret=READER_SECRET).status, 403)
        self.assertEqual(self.send("GET", f"/registry/projects/{project['id']}", secret=READER_SECRET).status, 200)
        self.assertEqual(len(self.registry.pending_sync()), 1)

    def test_bad_requests_register_nothing(self) -> None:
        self.assertEqual(self.send("POST", "/registry/projects", {}).status, 400)  # no idempotency key
        self.assertEqual(self.send("POST", "/registry/projects", {}, key=" ").status, 400)
        self.assertEqual(self.send("POST", "/registry/projects", None, key="k1").status, 400)  # no JSON body
        self.assertEqual(self.register("k1", page=" ").status, 400)
        self.assertEqual(self.send("POST", "/registry/projects", {"notion_page_id": 7}, key="k1").status, 400)
        self.assertEqual(self.send("GET", "/registry/projects").status, 405)
        self.assertEqual(self.send("POST", f"/registry/projects/{PROJECT_ID_PREFIX}x", {}, key="k1").status, 405)
        self.assertEqual(self.send("GET", "/registry/other").status, 404)
        self.assertEqual(self.registry.pending_sync(), ())

    def test_a_server_without_a_registry_answers_503(self) -> None:
        app = WebhookApp(self.queue, self.config, self.clock)
        answer = app.tailnet(Request("POST", "/registry/projects", {"Authorization": f"Bearer {WRITER_SECRET}"}, b"{}"))
        self.assertEqual(answer.status, 503)

    def test_registry_secrets_come_from_the_environment(self) -> None:
        config = ServerConfig.from_env(
            {"T6_WORKER_SECRET": "w", "T6_REGISTRY_SECRET": "rw", "T6_REGISTRY_READ_SECRET": "ro"}
        )
        by_secret = {grant.secret: grant for grant in config.grants}
        self.assertEqual((by_secret["rw"].registry_read, by_secret["rw"].registry_write), (True, True))
        self.assertEqual((by_secret["ro"].registry_read, by_secret["ro"].registry_write), (True, False))
        self.assertEqual((by_secret["w"].registry_read, by_secret["w"].registry_write), (False, False))
        self.assertTrue(by_secret["rw"].actor and by_secret["ro"].actor)


class DomainIdentityTests(unittest.TestCase):
    def test_a_project_carries_its_t6_id_beside_the_page_id(self) -> None:
        legacy = Project(page_id="p1", name="Show")
        registered = Project(page_id="p1", name="Show", t6_id=f"{PROJECT_ID_PREFIX}abc")
        self.assertIsNone(legacy.t6_id)
        self.assertEqual(registered.t6_id, f"{PROJECT_ID_PREFIX}abc")
        self.assertEqual(legacy, registered)  # equality stays on the page ID until the migration


if __name__ == "__main__":
    unittest.main()
