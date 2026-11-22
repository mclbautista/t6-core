"""`bay scan report` and `bay scan list` through the HTTP client, the fake Notion and a real registry (#376)."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

import httpx

from t6.bay import BayStore
from t6.bay_scan import FORMAT_VERSION, BayScanReport
from t6.client import APIError, Client
from t6.notion.generated import BaysRow
from t6.notion.testing import FakeNotion, rich_text, title
from t6.person import People
from t6.preview import PreviewTokens
from t6.registry.sqlite import SqliteProjectRegistry
from t6.server.http import make_server
from t6.server.queue import JobQueue
from t6.server.verbs import VerbAPI
from t6.verbs import BayScanListInput, BayScanReportInput, Done, Refused, catalogue

BAYS = "bays"
NOW = datetime(2026, 11, 22, 3, 0, tzinfo=timezone.utc)


def report(serial: str = "SERIAL-A", scanned_at: datetime = NOW, **changes: Any) -> BayScanReport:
    body: dict[str, Any] = dict(
        format_version=FORMAT_VERSION, serial=serial, scanner_version="1", scanned_at=scanned_at, resolve_version="20.3.1", status="ok",
        local_copies=[dict(project_uuid="uuid-1", project_name="Synthetic One", folder_path="Group", last_modified=1_700_000_000, timeline_count=2)],
        errors=[])
    body.update(changes)
    return BayScanReport.model_validate(body)


class BayScanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = NOW.timestamp()
        self.notion = FakeNotion()
        self.notion.add_snapshot_data_source(BAYS, "bays")
        self.registry = SqliteProjectRegistry(self.tmp.name + "/registry", clock=lambda: self.now)
        self.pages = {name: self.notion.add_page(BAYS, {"Name": title(name), "Serial Number": rich_text(serial)})
                      for name, serial in (("Bay A", "SERIAL-A"), ("Bay B", "SERIAL-B"), ("Bay C", None), ("Bay D", "SERIAL-D"), ("Bay E", "SERIAL-D"))}
        # Bay A, B and D/E are registered; Bay C has no serial and no Bay.
        rows = [BaysRow.from_page(self.pages[name]) for name in ("Bay A", "Bay B", "Bay D", "Bay E")]
        store = BayStore(self.registry)
        digest = store.migrate(rows, actor="setup")["digest"]
        self.entries = store.migrate(rows, actor="setup", apply=True, digest=digest)["entries"]
        self.bay: dict[str, str] = {entry["page_id"]: entry["t6_id"] for entry in self.entries}
        grants = {"bay-scanner": frozenset({"bay_scan"}), "reports": frozenset({"unmasked"}), "munki": frozenset({"fleet"})}
        secrets = {"scanner-secret": "bay-scanner", "reports-secret": "reports", "munki-secret": "munki", "gateway-secret": "gateway"}
        self.api = VerbAPI(People(self.registry, PreviewTokens("key"), grants), JobQueue(self.tmp.name + "/queue"), secrets,
                           lambda _: self.notion.client(), data_sources={BAYS: BAYS})
        env = patch.dict(os.environ, {"T6_NOTION_TOKEN": "fake", "T6_PREVIEW_SIGNING_KEY": "key"})
        env.start()
        self.addCleanup(env.stop)
        self.server = make_server(("127.0.0.1", 0), self.api)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(self.server.shutdown)
        url = f"http://127.0.0.1:{self.server.server_port}"
        self.scanner, self.reports, self.munki, self.gateway = (Client(url, secret) for secret in ("scanner-secret", "reports-secret", "munki-secret", "gateway-secret"))
        self.url = url

    def bay_id(self, name: str) -> str:
        return self.bay[str(self.pages[name]["id"]).replace("-", "")]

    def stored_rows(self) -> list[Any]:
        with self.registry._transaction() as db:
            return db.execute("SELECT * FROM bay_scan_reports").fetchall()

    def post(self, value: BayScanReport) -> Any:
        return self.scanner.bay_scan_report(BayScanReportInput(report=value))

    def listed(self, **inputs: Any) -> dict[str, dict[str, Any]]:
        outcome = self.reports.bay_scan_list(BayScanListInput(**inputs))
        assert isinstance(outcome, Done), outcome
        return {entry["bay_id"]: entry for entry in outcome.result["bays"]}

    def test_a_report_is_recorded_against_the_bay_its_serial_maps_to(self) -> None:
        outcome = self.post(report("SERIAL-A"))
        self.assertIsInstance(outcome, Done)
        assert isinstance(outcome, Done)
        self.assertEqual(outcome.result, {"bay_id": self.bay_id("Bay A"), "stored": True, "reason": "newer"})
        bays = self.listed()
        stored = bays[self.bay_id("Bay A")]
        self.assertEqual((stored["state"], stored["serial"], stored["status"], stored["resolve_version"]), ("fresh", "SERIAL-A", "ok", "20.3.1"))
        self.assertEqual(stored["local_copies"][0]["project_name"], "Synthetic One")
        self.assertEqual(bays[self.bay_id("Bay B")]["state"], "no_report")
        self.assertNotIn("local_copies", bays[self.bay_id("Bay B")])

    def test_the_serial_matches_without_case_or_edge_spaces(self) -> None:
        outcome = self.post(report(" serial-a "))
        assert isinstance(outcome, Done)
        self.assertEqual(outcome.result["bay_id"], self.bay_id("Bay A"))

    def test_posting_the_same_report_again_changes_nothing(self) -> None:
        self.post(report())
        before = self.stored_rows()
        self.now += 60
        again = self.post(report())
        assert isinstance(again, Done)
        self.assertEqual((again.result["stored"], again.result["reason"]), (False, "unchanged"))
        self.assertEqual(self.stored_rows(), before)
        with self.registry._transaction() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM audit WHERE action = 'bay_scan_report'").fetchone()[0], 1)

    def test_a_newer_report_replaces_the_older_and_an_older_one_is_ignored(self) -> None:
        self.post(report())
        newer = self.post(report(scanned_at=NOW + timedelta(days=1), local_copies=[]))
        assert isinstance(newer, Done)
        self.assertTrue(newer.result["stored"])
        self.assertEqual(self.listed()[self.bay_id("Bay A")]["local_copies"], [])
        older = self.post(report(scanned_at=NOW - timedelta(days=1)))
        assert isinstance(older, Done)
        self.assertEqual((older.result["stored"], older.result["reason"]), (False, "older than the stored report"))
        self.assertEqual(self.listed()[self.bay_id("Bay A")]["local_copies"], [])

    def test_a_report_older_than_three_days_shows_as_stale(self) -> None:
        self.post(report())
        self.now = (NOW + timedelta(days=3)).timestamp()
        self.assertEqual(self.listed()[self.bay_id("Bay A")]["state"], "fresh")
        self.now = (NOW + timedelta(days=3, seconds=1)).timestamp()
        self.assertEqual(self.listed()[self.bay_id("Bay A")]["state"], "stale")

    def test_a_machine_that_maps_to_no_bay_is_refused_and_nothing_is_stored(self) -> None:
        for serial, reason in (("SERIAL-UNKNOWN", "No Bays row has this serial"), ("SERIAL-D", "More than one Bays row has this serial")):
            refused = self.post(report(serial))
            self.assertIsInstance(refused, Refused)
            assert isinstance(refused, Refused)
            self.assertEqual((refused.rule, refused.reason), ("no_bay", reason))
        # A row that is not registered as a Bay: the page exists in Notion but was never migrated.
        self.notion.add_page(BAYS, {"Name": title("Bay F"), "Serial Number": rich_text("SERIAL-F")})
        refused = self.post(report("SERIAL-F"))
        assert isinstance(refused, Refused)
        self.assertEqual((refused.rule, refused.reason), ("no_bay", "The Bays row for this serial has no registered Bay"))
        with self.registry._transaction() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM bay_scan_reports").fetchone()[0], 0)

    def test_a_wrong_version_or_malformed_report_is_refused_and_nothing_is_written(self) -> None:
        refused = self.post(report(format_version=2))
        assert isinstance(refused, Refused)
        self.assertEqual(refused.rule, "format_version")
        malformed = json.loads(report().model_dump_json())
        del malformed["scanned_at"]
        response = httpx.post(self.url + "/v1/bay/scan/report", json={"report": malformed}, headers={"Authorization": "Bearer scanner-secret"})
        self.assertEqual(response.status_code, 422)
        skipped = json.loads(report(status="skipped_resolve_open", local_copies=[]).model_dump_json())
        skipped["local_copies"] = report().model_dump(mode="json")["local_copies"]
        response = httpx.post(self.url + "/v1/bay/scan/report", json={"report": skipped}, headers={"Authorization": "Bearer scanner-secret"})
        self.assertEqual(response.status_code, 422)
        with self.registry._transaction() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM bay_scan_reports").fetchone()[0], 0)

    def test_a_skipped_scan_is_stored_as_skipped_not_as_no_copies(self) -> None:
        self.post(report(status="skipped_resolve_open", local_copies=[]))
        stored = self.listed()[self.bay_id("Bay A")]
        self.assertEqual(stored["status"], "skipped_resolve_open")

    def test_only_the_bay_scanner_service_may_post_and_it_may_do_nothing_else(self) -> None:
        refused = self.munki.bay_scan_report(BayScanReportInput(report=report()))
        assert isinstance(refused, Refused)
        self.assertEqual((refused.rule, refused.reason), ("permission", "needs bay_scan"))
        from t6.verbs import BayListInput
        other = self.scanner.bay_list(BayListInput())
        assert isinstance(other, Refused)
        self.assertEqual(other.rule, "service")
        listing = self.scanner.bay_scan_list(BayScanListInput())
        assert isinstance(listing, Refused)
        self.assertEqual(listing.rule, "service")

    def test_a_reader_without_unmasked_gets_no_project_names(self) -> None:
        self.post(report())
        masked = self.gateway.bay_scan_list(BayScanListInput())
        assert isinstance(masked, Refused)
        self.assertEqual(masked.rule, "masking")

    def test_one_bay_can_be_listed_by_id_and_an_unknown_one_is_refused(self) -> None:
        self.post(report())
        one = self.listed(bay=self.bay_id("Bay A"))
        self.assertEqual(list(one), [self.bay_id("Bay A")])
        refused = self.reports.bay_scan_list(BayScanListInput(bay="t6b_missing"))
        assert isinstance(refused, Refused)
        self.assertEqual(refused.rule, "bay")

    def test_the_verbs_are_in_the_command_catalogue(self) -> None:
        verbs = {(entry["noun"], entry["verb"]): entry for entry in catalogue()}
        self.assertEqual((verbs[("bay", "scan report")]["role"], verbs[("bay", "scan report")]["read_only"]), ("bay_scan", False))
        self.assertEqual((verbs[("bay", "scan list")]["role"], verbs[("bay", "scan list")]["read_only"]), ("reader", True))


if __name__ == "__main__":
    unittest.main()
