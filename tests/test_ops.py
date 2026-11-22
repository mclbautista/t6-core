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
        job = self.queue.claim("main", 60, ["registry.sync"])
        assert job is not None
        self.assertEqual(job.kind, "registry.sync")
        self.assertIsNone(self.queue.claim("main", 60, ["registry.sync"]))  # once, not twice

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
        db.execute("DROP TRIGGER audit_no_delete")
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


    def test_history_can_be_reported_without_failing_health(self) -> None:
        project = self.new("ABC", "in")
        self.send(
            "POST",
            f"/registry/projects/{project['id']}/sync",
            {"revision": project["revision"], "ok": False, "detail": "boom", "attempt": 5},
            secret="worker-secret",
        )
        report = ops.health(self.live(), now=self.clock(), count_history=False)
        self.assertIs(report["ok"], True)
        self.assertEqual(report["problems"], [])
        self.assertIn("failed after retries", str(report["history"]))

    def add_internal_jobs(self, *states: tuple[str, str]) -> None:
        with sqlite3.connect(self.path) as db:
            for number, (kind, state) in enumerate(states):
                db.execute("INSERT INTO internal_jobs (queue, kind, dedupe_key, payload, run_at, state, created_at) VALUES ('main', ?, ?, '{}', 1e12, ?, 0)", (kind, f"k{number}", state))  # run_at far ahead: a queued row here is not waiting

    def test_health_counts_dead_internal_jobs_by_kind(self) -> None:
        self.add_internal_jobs(("booking.calendar_sync", "dead"), ("booking.calendar_sync", "dead"), ("registry.sync", "dead"), ("registry.sync", "done"), ("registry.sync", "queued"))
        report = ops.health(self.live(), now=self.clock())
        self.assertEqual(report["dead_internal_jobs"], {"booking.calendar_sync": 2, "registry.sync": 1})
        self.assertIs(report["ok"], False)
        self.assertIn("3 internal job(s) are dead", str(report["problems"]))
        self.assertIn("booking.calendar_sync: 2", str(report["problems"]))
        history = ops.health(self.live(), now=self.clock(), count_history=False)
        self.assertIs(history["ok"], True)
        self.assertEqual(history["dead_internal_jobs"], {"booking.calendar_sync": 2, "registry.sync": 1})

    def test_health_reports_no_dead_internal_jobs_as_an_empty_count(self) -> None:
        self.add_internal_jobs(("registry.sync", "done"))
        report = ops.health(self.live(), now=self.clock())
        self.assertEqual(report["dead_internal_jobs"], {})
        self.assertIs(report["ok"], True)

    def add_waiting(self, queue_jobs: tuple[tuple[str, str, float, float | None], ...] = (), internal: tuple[tuple[str, str, float, float | None], ...] = ()) -> None:
        """Rows of (kind, state, run_at, lease_until) in the queue file and in the internal job table."""
        with sqlite3.connect(self.live()["queue"]) as db:
            for number, (kind, state, run_at, lease) in enumerate(queue_jobs):
                db.execute("INSERT INTO jobs (queue, kind, dedupe_key, payload, run_at, state, lease_until, created_at) VALUES ('main', ?, ?, '{}', ?, ?, ?, 0)", (kind, f"q{number}", run_at, state, lease))
        with sqlite3.connect(self.path) as db:
            for number, (kind, state, run_at, lease) in enumerate(internal):
                db.execute("INSERT INTO internal_jobs (queue, kind, dedupe_key, payload, run_at, state, lease_until, created_at) VALUES ('main', ?, ?, '{}', ?, ?, ?, 0)", (kind, f"i{number}", run_at, state, lease))

    def test_health_shows_due_jobs_that_waited_too_long_by_kind_with_the_oldest_age(self) -> None:
        now = self.clock()
        self.add_waiting(
            queue_jobs=(("nobody.claims", "pending", now - 3600, None), ("nobody.claims", "pending", now - 1800, None)),
            internal=(("orphan.kind", "queued", now - 7200, None),),
        )
        report = ops.health(self.live(), now=now)
        self.assertEqual(report["waiting_jobs"], {"queue": {"nobody.claims": {"count": 2, "oldest_seconds": 3600.0}},
                                                  "internal": {"orphan.kind": {"count": 1, "oldest_seconds": 7200.0}}})
        self.assertIs(report["ok"], False)
        self.assertIn("nobody.claims", str(report["problems"]))
        self.assertIn("orphan.kind", str(report["problems"]))

    def test_jobs_not_yet_due_or_not_waiting_long_or_under_a_live_lease_are_not_waiting(self) -> None:
        now = self.clock()
        self.add_waiting(
            queue_jobs=(("later", "pending", now + 3600, None), ("fresh", "pending", now - 60, None), ("busy", "claimed", now - 7200, now + 30)),
            internal=(("later", "queued", now + 3600, None), ("fresh", "queued", now - 60, None), ("busy", "claimed", now - 7200, now + 30), ("finished", "done", now - 7200, None)),
        )
        report = ops.health(self.live(), now=now)
        self.assertEqual(report["waiting_jobs"], {"queue": {}, "internal": {}})
        self.assertIs(report["ok"], True)

    def test_a_claimed_job_whose_lease_ran_out_is_waiting(self) -> None:
        now = self.clock()
        self.add_waiting(internal=(("stalled", "claimed", now - 7200, now - 3000),))
        self.assertEqual(ops.health(self.live(), now=now)["waiting_jobs"]["internal"], {"stalled": {"count": 1, "oldest_seconds": 7200.0}})  # type: ignore[index]

    def test_the_threshold_is_fifteen_minutes_by_default_and_can_be_changed(self) -> None:
        now = self.clock()
        self.add_waiting(internal=(("slow", "queued", now - 600, None),))
        self.assertEqual(ops.health(self.live(), now=now)["waiting_jobs"]["internal"], {})  # type: ignore[index]
        self.assertEqual(ops.health(self.live(), now=now, waiting_limit=300)["waiting_jobs"]["internal"], {"slow": {"count": 1, "oldest_seconds": 600.0}})  # type: ignore[index]
        self.assertEqual(ops.WAITING_LIMIT_SECONDS, 900)

    def test_waiting_jobs_are_a_current_state_so_ignore_history_does_not_hide_them(self) -> None:
        now = self.clock()
        self.add_waiting(internal=(("orphan.kind", "queued", now - 7200, None),))
        report = ops.health(self.live(), now=now, count_history=False)
        self.assertIs(report["ok"], False)
        self.assertIn("orphan.kind", str(report["problems"]))
        self.assertNotIn("orphan.kind", str(report["history"]))

    def test_an_edge_server_with_no_registry_still_reports_its_queue_file(self) -> None:
        now = self.clock()
        self.add_waiting(queue_jobs=(("nobody.claims", "pending", now - 3600, None),))
        paths = dict(self.live())
        paths["registry"] = Path(self.directory.name) / "absent.sqlite"
        report = ops.health(paths, now=now, required=("queue",))
        self.assertEqual(report["waiting_jobs"], {"queue": {"nobody.claims": {"count": 1, "oldest_seconds": 3600.0}}, "internal": {}})
        self.assertIs(report["ok"], False)

    def test_an_edge_server_needs_no_registry(self) -> None:
        paths = dict(self.live())
        paths["registry"] = Path(self.directory.name) / "absent.sqlite"
        self.assertIs(ops.health(paths, now=self.clock())["ok"], False)
        report = ops.health(paths, now=self.clock(), required=("queue",))
        self.assertIs(report["ok"], True)


class CommandTests(OpsTestCase):
    def test_the_commands_use_the_servers_environment_variables(self) -> None:
        import os
        from unittest import mock

        self.workflow()
        live = self.live()
        env = {"T6_REGISTRY_DB": str(live["registry"]), "T6_QUEUE_DB": str(live["queue"]), "T6_OPERATOR_DB": str(live["operator"])}
        backup = Path(self.directory.name) / "backup"
        with mock.patch.dict(os.environ, env), mock.patch("t6.ops.time.time", side_effect=self.clock), mock.patch("t6.client.Client.health", return_value={"version": "test", "schema_version": 1}):
            self.assertEqual(cli_main(["admin", "backup", "--into", str(backup)]), 0)
            self.assertEqual(cli_main(["health"]), 0)
            self.assertEqual(cli_main(["admin", "restore", "--from", str(backup)]), 2)  # the databases exist: refused
        with mock.patch.dict(os.environ, env), mock.patch("t6.ops.time.time", side_effect=self.clock), mock.patch("t6.client.Client.health", side_effect=AssertionError("must not call the API")):
            self.assertEqual(cli_main(["health", "--local"]), 0)
            self.assertEqual(cli_main(["health", "--local", "--ignore-history"]), 0)
            with mock.patch.dict(os.environ, {"T6_SERVER_MODE": "edge", "T6_REGISTRY_DB": str(Path(self.directory.name) / "none.sqlite")}):
                self.assertEqual(cli_main(["health", "--local"]), 0)
        target = self.paths("restored")
        env = {"T6_REGISTRY_DB": str(target["registry"]), "T6_QUEUE_DB": str(target["queue"]), "T6_OPERATOR_DB": str(target["operator"])}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(cli_main(["admin", "restore", "--from", str(backup)]), 0)
