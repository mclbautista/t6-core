"""Backup, restore and health: identity, reservations, audit, pending actions and sync work survive."""

from pathlib import Path
import shutil
import sqlite3
from typing import Any

from test_operator import OPERATOR, PAGE_A, PAGE_B, OperatorTestCase

from t6 import ops
from t6.cli import main as cli_main
from t6.operator import ActionStore, OperatorService
from t6.registry import SqliteProjectRegistry
from t6.server import JobQueue, WebhookApp


class OpsTestCase(OperatorTestCase):
    def paths(self, name: str) -> dict[str, Path]:
        base = Path(self.directory.name) / name
        return {"registry": base / "registry.sqlite", "queue": base / "queue.sqlite", "operator": base / "operator.sqlite"}

    def live(self) -> dict[str, Path]:
        base = Path(self.directory.name)
        return {"registry": self.path, "queue": base / "q.sqlite3", "operator": self.path.with_name("operator.sqlite3")}

    def boot(self, paths: dict[str, Path]) -> None:
        """Start the server on these files, as a restart on a restored volume does."""
        self.queue = JobQueue(paths["queue"], self.clock)
        self.registry = SqliteProjectRegistry(paths["registry"], self.clock)
        self.store = ActionStore(str(paths["operator"]), self.clock)
        service = OperatorService(self.registry, self.store, self.config.operator_ids, clock=self.clock)
        self.app = WebhookApp(self.queue, self.config, self.clock, registry=self.registry, operator=service)

    def workflow(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """A out, B reuses A's code, A's return is refused; work left pending throughout."""
        a = self.new("ABC", "in", page=PAGE_A)
        self.assertEqual(self.rotate(a, "out").status, 200)
        b = self.new("ABC", "in", page=PAGE_B)
        self.assertEqual(self.rotate(a, "in").status, 409)
        return a, b

    def snapshot(self) -> list[Any]:
        return [
            (p["id"], p["code"], p["rotation"], p["revision"], [(x["page_id"], x["status"]) for x in p["bindings"]])
            for p in sorted(self.all_projects(), key=lambda p: p["id"])
        ]


class BackupRestoreTests(OpsTestCase):
    def test_restore_keeps_identity_reservations_audit_pending_sync_and_pending_cards(self) -> None:
        a, b = self.workflow()
        token = self.offer(page_id="cccccccccccccccccccccccccccccccc", label="Pending")  # never delivered
        before = self.snapshot()
        audit = {p["id"]: len(self.registry.audit(p["id"])) for p in self.all_projects()}
        unsynced = len(self.registry.pending_sync())
        self.assertGreater(unsynced, 0)

        manifest = ops.backup(self.live(), Path(self.directory.name) / "backup")
        self.assertEqual(set(manifest["files"]), {"registry", "queue", "operator"})  # type: ignore[call-overload]
        self.new("LATE", "in")  # accepted after the backup: not in the restore

        target = self.paths("restored")
        restored = ops.restore(Path(self.directory.name) / "backup", target)
        self.assertEqual(restored["registry"]["pending_sync"], unsynced)
        self.boot(target)

        self.assertEqual(self.snapshot(), before)  # same IDs, codes, bindings, revisions
        self.assertEqual({p["id"]: len(self.registry.audit(p["id"])) for p in self.all_projects()}, audit)
        self.assertEqual(self.rotate({"id": a["id"]}, "in").status, 409)  # B still holds ABC
        self.assertEqual(self.read({"id": b["id"]})["rotation"], "in")
        self.assertEqual(self.registry.get_by_page(PAGE_A).id, a["id"])  # type: ignore[union-attr]
        self.assertEqual(len(self.registry.pending_sync()), unsynced)

        # The card that was waiting is sent now, once, and works.
        self.drain()
        self.assertEqual(self.gateway.sends, 1)
        self.assertIn("Pending", self.card()["text"])
        self.assertEqual(self.tap(self.payload(1, "Skip")).body["outcome"], "skipped")
        self.assertEqual(token, next(iter(self.gateway.keys)).split(":")[1])

    def test_unconfirmed_notion_syncs_are_queued_again_after_a_restore(self) -> None:
        self.new("ABC", "in")
        manifest_dir = Path(self.directory.name) / "backup"
        ops.backup(self.live(), manifest_dir)
        target = self.paths("restored")
        ops.restore(manifest_dir, target)
        # A queue from before the job existed:
        target["queue"].unlink()
        self.boot(target)
        job = self.queue.claim("main", 60)
        assert job is not None
        self.assertEqual(job.kind, "registry.sync")
        self.assertIsNone(self.queue.claim("main", 60))  # once, not twice

    def test_restore_refuses_to_overwrite_and_a_damaged_backup_is_refused(self) -> None:
        self.workflow()
        good = Path(self.directory.name) / "backup"
        ops.backup(self.live(), good)
        with self.assertRaisesRegex(ops.OpsError, "never overwrites"):
            ops.restore(good, self.live())
        with self.assertRaisesRegex(ops.OpsError, "not empty"):
            ops.backup(self.live(), good)

        bad = Path(self.directory.name) / "bad"
        shutil.copytree(good, bad)
        db = sqlite3.connect(bad / "registry.sqlite")
        db.execute("DELETE FROM audit")
        db.commit()
        db.close()
        target = self.paths("never")
        with self.assertRaisesRegex(ops.OpsError, "checksum"):
            ops.restore(bad, target)
        self.assertFalse(any(path.exists() for path in target.values()))  # nothing was written

    def test_backup_is_safe_while_the_server_is_writing(self) -> None:
        self.workflow()
        backup = Path(self.directory.name) / "backup"
        ops.backup(self.live(), backup)
        self.new("MORE", "in")
        restored = ops.restore(backup, self.paths("restored"))
        self.assertEqual(restored["registry"]["projects"], len(self.all_projects()) - 1)


class HealthTests(OpsTestCase):
    def test_health_reports_sync_lag_and_persistent_failures(self) -> None:
        project = self.new("ABC", "in")
        self.clock.advance(120)
        report = ops.health(self.live(), now=self.clock())
        self.assertEqual((report["ok"], report["sync_lag_seconds"]), (True, 120.0))

        self.send(
            "POST",
            f"/registry/projects/{project['id']}/sync",
            {"revision": project["revision"], "ok": False, "detail": "boom", "attempt": 5},
            secret="worker-secret",
        )
        report = ops.health(self.live(), now=self.clock())
        self.assertIs(report["ok"], False)
        self.assertEqual(report["sync_persistent_failures"], 1)
        self.assertIn("failed after retries", str(report["problems"]))


class CommandTests(OpsTestCase):
    def test_the_commands_use_the_servers_environment_variables(self) -> None:
        import os
        from unittest import mock

        self.workflow()
        live = self.live()
        env = {"T6_REGISTRY_DB": str(live["registry"]), "T6_QUEUE_DB": str(live["queue"]), "T6_OPERATOR_DB": str(live["operator"])}
        backup = Path(self.directory.name) / "backup"
        with mock.patch.dict(os.environ, env):
            self.assertEqual(cli_main(["backup", "--into", str(backup)]), 0)
            self.assertEqual(cli_main(["health"]), 0)
            self.assertEqual(cli_main(["restore", "--from", str(backup)]), 2)  # the databases exist: refused
        target = self.paths("restored")
        env = {"T6_REGISTRY_DB": str(target["registry"]), "T6_QUEUE_DB": str(target["queue"]), "T6_OPERATOR_DB": str(target["operator"])}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(cli_main(["restore", "--from", str(backup)]), 0)
