"""The drift report (#380): the comparison on fabricated reports and inventories, then through the API with a fake server inventory."""
from __future__ import annotations

import os
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch

from media_seed import link_media
from t6.bay_drift import MATCHED_BY, ServerProject, copy_state, drift, from_resolve_project
from t6.bay import BayStore
from t6.client import Client
from t6.notion.generated import BaysRow
from t6.notion.testing import FakeNotion, rich_text, title
from t6.person import People
from t6.preview import PreviewTokens
from t6.registry import Registered
from t6.registry.sqlite import SqliteProjectRegistry
from t6.resolve.reader import ResolveProject
from t6.server.http import make_server
from t6.server.queue import JobQueue
from t6.server.verbs import VerbAPI
from t6.verbs import BayDriftInput, BayScanReportInput, Done, Refused

from test_bay_scan import report

NOW = datetime(2026, 11, 22, 3, 0, tzinfo=timezone.utc)


def server(name: str, folder: str = "", modified: int | None = 100, timelines: int | None = 2) -> ServerProject:
    return ServerProject(external_id="db/" + (folder + "/" if folder else "") + name, folder_path=folder, name=name, last_modified=modified, timeline_count=timelines)


def scan(bay: str, state: str = "fresh", status: str = "ok", copies: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    entry: dict[str, Any] = {"bay_id": bay, "state": state}
    if state != "no_report":
        entry.update(status=status, local_copies=copies or [])
    return entry


def local(name: str, folder: str = "", modified: int = 100, timelines: int = 2) -> dict[str, Any]:
    return {"project_uuid": "u-" + name, "project_name": name, "folder_path": folder, "last_modified": modified, "timeline_count": timelines}


class CompareTests(unittest.TestCase):
    def test_every_state_of_a_copy(self) -> None:
        on_server = server("A", modified=100, timelines=2)
        self.assertEqual(copy_state(local("A"), on_server, False), "equal")
        self.assertEqual(copy_state(local("A", modified=101), on_server, False), "local_newer")
        self.assertEqual(copy_state(local("A", modified=99), on_server, False), "local_older")
        self.assertEqual(copy_state(local("A", timelines=3), on_server, False), "differs")
        self.assertEqual(copy_state(local("A"), None, False), "not_on_server")
        self.assertEqual(copy_state(local("A"), on_server, True), "report_stale")
        self.assertEqual(copy_state(local("A"), server("A", modified=None, timelines=None), False), "cannot_compare")

    def test_a_report_of_match_differ_not_on_server_and_stale_cases(self) -> None:
        inventory = [server("Alpha", "Group"), server("Beta"), server("Gamma")]
        scans = [
            scan("t6b_1", copies=[local("Alpha", "Group"), local("Beta", modified=150), local("Only Here")]),
            scan("t6b_2", state="stale", copies=[local("Alpha", "Group")]),
        ]
        result = drift(scans, inventory, {"db/Group/Alpha": "t6p_alpha"})
        self.assertEqual(result["matched_by"], MATCHED_BY)
        self.assertIn("not confirmed", result["note"])
        by_name = {row["name"]: row for row in result["projects"]}
        self.assertEqual([(c["bay_id"], c["state"]) for c in by_name["Alpha"]["copies"]], [("t6b_1", "equal"), ("t6b_2", "report_stale")])
        self.assertEqual(by_name["Alpha"]["project"], "t6p_alpha")
        self.assertEqual([c["state"] for c in by_name["Beta"]["copies"]], ["local_newer"])
        self.assertEqual(by_name["Gamma"]["copies"], [])  # on the server with no local copies, listed as such
        self.assertEqual([(c["bay_id"], c["state"]) for c in by_name["Only Here"]["copies"]], [("t6b_1", "not_on_server")])
        self.assertFalse(by_name["Only Here"]["on_server"])
        self.assertTrue(by_name["Alpha"]["on_server"])

    def test_a_bay_with_no_report_is_unknown_never_no_copies(self) -> None:
        result = drift([scan("t6b_1", copies=[local("Alpha")]), scan("t6b_2", state="no_report"), scan("t6b_3", status="skipped_resolve_open")],
                       [server("Alpha"), server("Beta")], {})
        self.assertEqual({row["name"]: row["unknown_bays"] for row in result["projects"]}, {"Alpha": ["t6b_2", "t6b_3"], "Beta": ["t6b_2", "t6b_3"]})
        self.assertEqual({bay["bay_id"]: bay["state"] for bay in result["bays"]}, {"t6b_1": "fresh", "t6b_2": "no_report", "t6b_3": "skipped_resolve_open"})

    def test_one_project_only(self) -> None:
        result = drift([scan("t6b_1", copies=[local("Alpha"), local("Other")])], [server("Alpha"), server("Beta")], {}, only="db/Alpha")
        self.assertEqual([row["name"] for row in result["projects"]], ["Alpha"])

    def test_a_resolve_project_has_no_times_so_it_cannot_be_compared(self) -> None:
        project = ResolveProject(database="db", folder_path=("Group",), name="Alpha", internal_id="uuid-1")
        converted = from_resolve_project(project)
        self.assertEqual((converted.external_id, converted.folder_path, converted.uuid), ("db/Group/Alpha", "Group", "uuid-1"))
        self.assertEqual(copy_state(local("Alpha", "Group"), converted, False), "cannot_compare")

    def test_the_drift_code_writes_nothing(self) -> None:
        import inspect
        import t6.bay_drift as module
        source = inspect.getsource(module)
        for forbidden in ("INSERT", "UPDATE", "DELETE", ".write(", "open("):
            self.assertNotIn(forbidden, source)


class DriftVerbTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = NOW.timestamp()
        notion = FakeNotion()
        notion.add_snapshot_data_source("bays", "bays")
        pages = [notion.add_page("bays", {"Name": title(name), "Serial Number": rich_text(serial)}) for name, serial in (("Bay A", "SERIAL-A"), ("Bay B", "SERIAL-B"))]
        self.registry = SqliteProjectRegistry(self.tmp.name + "/registry", clock=lambda: self.now)
        store = BayStore(self.registry)
        rows = [BaysRow.from_page(page) for page in pages]
        digest = store.migrate(rows, actor="setup")["digest"]
        self.bays = {e["page_id"]: e["t6_id"] for e in store.migrate(rows, actor="setup", apply=True, digest=digest)["entries"]}
        registered = self.registry.register(actor="setup", idempotency_key="p1")
        assert isinstance(registered, Registered)
        self.project = registered.project.id
        link_media(self.registry, self.project, "db/Group/Alpha", kind="resolve-project", role="working")
        self.inventory: list[ServerProject] = [server("Alpha", "Group", modified=100, timelines=2), server("Beta")]
        self.configured = True
        grants = {"bay-scanner": frozenset({"bay_scan"}), "reports": frozenset({"unmasked"})}
        self.api = VerbAPI(People(self.registry, PreviewTokens("key"), grants), JobQueue(self.tmp.name + "/queue"),
                           {"scanner-secret": "bay-scanner", "reports-secret": "reports"}, lambda _: notion.client(), data_sources={"bays": "bays"},
                           server_projects=lambda: self.inventory)
        env = patch.dict(os.environ, {"T6_NOTION_TOKEN": "fake", "T6_PREVIEW_SIGNING_KEY": "key"})
        env.start()
        self.addCleanup(env.stop)
        httpd = make_server(("127.0.0.1", 0), self.api)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(httpd.shutdown)
        url = f"http://127.0.0.1:{httpd.server_port}"
        self.scanner, self.reports = Client(url, "scanner-secret"), Client(url, "reports-secret")

    def test_the_verb_reports_per_project_and_changes_nothing(self) -> None:
        copies = [dict(project_uuid="u1", project_name="Alpha", folder_path="Group", last_modified=200, timeline_count=2)]
        posted = self.scanner.bay_scan_report(BayScanReportInput(report=report("SERIAL-A", NOW, local_copies=copies)))
        assert isinstance(posted, Done)
        with self.registry._transaction() as db:
            before = (db.execute("SELECT count(*) FROM audit").fetchone()[0], db.execute("SELECT * FROM bay_scan_reports").fetchall())
        outcome = self.reports.bay_drift(BayDriftInput())
        assert isinstance(outcome, Done), outcome
        rows = {row["name"]: row for row in outcome.result["projects"]}
        self.assertEqual([(c["bay_id"], c["state"]) for c in rows["Alpha"]["copies"]], [(posted.result["bay_id"], "local_newer")])
        self.assertEqual(rows["Alpha"]["project"], self.project)
        self.assertEqual(rows["Beta"]["copies"], [])
        self.assertEqual(rows["Beta"]["unknown_bays"], [self.bays[next(k for k in self.bays if self.bays[k] != posted.result["bay_id"])]])
        with self.registry._transaction() as db:
            self.assertEqual((db.execute("SELECT count(*) FROM audit").fetchone()[0], db.execute("SELECT * FROM bay_scan_reports").fetchall()), before)

    def test_one_project_by_its_resolve_media_location(self) -> None:
        outcome = self.reports.bay_drift(BayDriftInput(project=self.project))
        assert isinstance(outcome, Done), outcome
        self.assertEqual([row["name"] for row in outcome.result["projects"]], ["Alpha"])
        refused = self.reports.bay_drift(BayDriftInput(project="t6p_none"))
        assert isinstance(refused, Refused)
        self.assertEqual(refused.rule, "project")

    def test_without_a_server_inventory_the_verb_says_so(self) -> None:
        self.api.server_projects = None
        refused = self.reports.bay_drift(BayDriftInput())
        assert isinstance(refused, Refused)
        self.assertEqual((refused.rule, refused.reason), ("config", "The project server inventory is not configured"))

    def test_the_bay_scanner_may_not_read_the_drift(self) -> None:
        refused = self.scanner.bay_drift(BayDriftInput())
        assert isinstance(refused, Refused)
        self.assertEqual(refused.rule, "service")


if __name__ == "__main__":
    unittest.main()
