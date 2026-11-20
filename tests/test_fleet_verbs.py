"""`machine list`, `machine sync` and `machine edit` through the HTTP client and the fake Notion."""
import json
import os
import tempfile
import threading
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import httpx

from t6.client import Client
from t6.domain import Ring
from t6.notion.testing import FakeNotion, checkbox, date, rich_text, select, title
from t6.person import People
from t6.preview import PreviewTokens
from t6.registry.sqlite import SqliteProjectRegistry
from t6.server.http import make_server
from t6.server.queue import JobQueue
from t6.server.verbs import VerbAPI
from t6.verbs import CameraEnrichInput, Done, MachineEditInput, MachineEditItem, MachineFactsInput, MachineListInput, MachineSyncInput, Preview, Refused

BAYS = "bays"


def fresh() -> datetime:
    return datetime.now(UTC)


class MachineVerbTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.notion = FakeNotion()
        self.notion.add_snapshot_data_source(BAYS, "bays")
        self.notion.add_snapshot_data_source("cameras", "cameras")
        self.camera = self.notion.add_page("cameras", {"Name": title("Camera One")})
        registry = SqliteProjectRegistry(self.tmp.name + "/registry")
        grants = {"munki": frozenset({"fleet"}), "watchdog": frozenset({"bays_edit"})}
        self.api = VerbAPI(
            People(registry, PreviewTokens("key"), grants), JobQueue(self.tmp.name + "/queue"),
            {"munki-secret": "munki", "reports-secret": "reports", "editor-secret": "watchdog"}, lambda _: self.notion.client(),
            data_sources={BAYS: BAYS, "cameras": "cameras"},
        )
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
        self.munki, self.reports = Client(url, "munki-secret"), Client(url, "reports-secret")
        self.editor = Client(url, "editor-secret")
        self.registry = registry

    def bay(self, name: str = "Bay 1", host: str | None = "Bay-1", **facts: str | None) -> str:
        columns = {"Serial Number": "serial", "Munki Manifest": "manifest", "Ring": "ring", "OS Version": "os"}
        properties = {"Name": title(name), "Hostname": rich_text(host)}
        for notion_name, key in columns.items():
            if key in facts:
                properties[notion_name] = select(facts[key]) if key == "ring" else rich_text(facts[key])
        return str(self.notion.add_page(BAYS, properties)["id"])

    def row(self, page_id: str, prop: str) -> object:
        return self.notion.page(page_id)["properties"][prop]

    def preview(self, *machines: MachineFactsInput) -> Preview:
        preview = self.munki.machine_sync(MachineSyncInput(machines=list(machines)))
        assert isinstance(preview, Preview), preview
        return preview

    def apply(self, *machines: MachineFactsInput) -> Done:
        preview = self.preview(*machines)
        done = self.munki.machine_sync(MachineSyncInput(machines=list(machines), confirm=preview.token))
        assert isinstance(done, Done), done
        return done

    def edit(self, *items: tuple[list[str], dict]) -> MachineEditInput:
        return MachineEditInput(machines=[MachineEditItem(names=names, values=values) for names, values in items])

    def edit_preview(self, inputs: MachineEditInput) -> Preview:
        preview = self.editor.machine_edit(inputs)
        assert isinstance(preview, Preview), preview
        return preview

    def edit_apply(self, inputs: MachineEditInput) -> Done:
        done = self.editor.machine_edit(inputs.model_copy(update={"confirm": self.edit_preview(inputs).token}))
        assert isinstance(done, Done), done
        return done

    def patches(self) -> int:
        return sum(1 for r in self.notion.requests if r.method == "PATCH")

    def test_machine_list_reads_the_bays_as_machines_for_a_reader(self) -> None:
        self.bay(serial="S1", manifest="Bay-1", ring="pilot")
        for client in (self.munki, self.reports):
            result = client.machine_list(MachineListInput())
            assert isinstance(result, Done)
            (machine,) = result.result["machines"]
            self.assertEqual((machine["name"], machine["hostname"], machine["serial_number"], machine["ring"]), ("Bay 1", "Bay-1", "S1", "pilot"))

    def test_the_preview_shows_the_row_and_each_field_old_and_new_and_writes_nothing(self) -> None:
        page = self.bay(serial="OLD", manifest="Bay-1")
        preview = self.preview(MachineFactsInput(names=["Bay-1"], serial_number="NEW", munki_manifest="Bay-1", ring=Ring.PILOT, observed_at=fresh()))
        (item,) = preview.plan["machines"]
        self.assertEqual((item["status"], item["page_id"]), ("matched", page))
        self.assertEqual(item["changes"], [{"fact": "serial_number", "old": "OLD", "new": "NEW"}, {"fact": "ring", "old": None, "new": "pilot"}])
        self.assertEqual(self.patches(), 0)

    def test_confirm_writes_the_facts_and_a_second_run_changes_nothing(self) -> None:
        page = self.bay(serial="OLD")
        machine = MachineFactsInput(names=["Bay-1"], serial_number="NEW", ring=Ring.PILOT, observed_at=fresh())
        done = self.apply(machine)
        (item,) = done.result["machines"]
        self.assertEqual((item["status"], item["written"]), ("matched", "saved"))
        self.assertEqual(self.row(page, "Serial Number")["rich_text"][0]["plain_text"], "NEW")  # type: ignore[index]
        before = self.patches()
        again = self.munki.machine_sync(MachineSyncInput(machines=[machine]))
        assert isinstance(again, Preview)
        self.assertEqual(again.plan["machines"][0]["changes"], [])
        self.assertEqual(self.patches(), before)

    def test_unmatched_and_ambiguous_machines_are_skipped_with_their_reason(self) -> None:
        self.bay("Bay 1", "Bay-1")
        self.bay("Bay 1 copy", "bay-1")
        preview = self.preview(MachineFactsInput(names=["Bay-1"], serial_number="S1"), MachineFactsInput(names=["Nowhere"], serial_number="S2"))
        self.assertEqual([m["status"] for m in preview.plan["machines"]], ["ambiguous", "unmatched"])
        self.assertTrue(all(m["page_id"] is None and m["changes"] == [] for m in preview.plan["machines"]))

    def test_two_machines_on_one_row_are_both_ambiguous(self) -> None:
        self.bay("Bay 1", "Bay-1")
        preview = self.preview(MachineFactsInput(names=["Bay-1"], serial_number="S1"), MachineFactsInput(names=["bay 1"], serial_number="S2"))
        self.assertEqual([m["status"] for m in preview.plan["machines"]], ["ambiguous", "ambiguous"])

    def test_unknown_facts_are_never_cleared(self) -> None:
        page = self.bay(serial="KEEP", manifest="Bay-1", ring="pilot")
        done = self.apply(MachineFactsInput(names=["Bay-1"], serial_number="", observed_at=fresh()))
        (item,) = done.result["machines"]
        self.assertEqual(item["changes"], [])
        self.assertEqual(self.patches(), 0)
        self.assertEqual(self.row(page, "Serial Number")["rich_text"][0]["plain_text"], "KEEP")  # type: ignore[index]
        self.assertIn({"fact": "serial_number", "reason": "unknown"}, item["skipped"])

    def test_stale_observed_facts_are_skipped_and_say_why(self) -> None:
        page = self.bay()
        old = fresh() - timedelta(hours=2)
        done = self.apply(MachineFactsInput(names=["Bay-1"], serial_number="S1", os_version="26.1", observed_at=old))
        (item,) = done.result["machines"]
        self.assertEqual([c["fact"] for c in item["changes"]], ["serial_number"])
        self.assertIn({"fact": "os_version", "reason": "stale"}, item["skipped"])
        self.assertNotIn("OS Version", self.notion.page(page)["properties"])

    def test_a_row_edited_after_it_was_read_is_skipped_as_changed(self) -> None:
        page = self.bay(serial="OLD")
        original = self.notion._handle
        queries: list[httpx.Request] = []

        def edit_after_query(request: httpx.Request) -> httpx.Response:
            response = original(request)
            if request.url.path.endswith("/query"):
                queries.append(request)
                if len(queries) == 2:  # the confirm's query, not the preview's
                    self.notion._pages[page]["properties"]["Name"] = title("Renamed")
            return response

        self.notion.transport = httpx.MockTransport(edit_after_query)
        preview = self.preview(MachineFactsInput(names=["Bay-1"], serial_number="NEW"))
        done = self.munki.machine_sync(MachineSyncInput(machines=[MachineFactsInput(names=["Bay-1"], serial_number="NEW")], confirm=preview.token))
        assert isinstance(done, Done)
        self.assertEqual(done.result["machines"][0]["status"], "changed")
        self.assertEqual(self.patches(), 0)

    def test_a_preview_token_works_once_and_only_for_its_machines(self) -> None:
        self.bay()
        machine = MachineFactsInput(names=["Bay-1"], serial_number="S1")
        preview = self.preview(machine)
        other = self.munki.machine_sync(MachineSyncInput(machines=[MachineFactsInput(names=["Bay-1"], serial_number="S2")], confirm=preview.token))
        assert isinstance(other, Refused)
        self.assertEqual(other.rule, "token_mismatch")
        inputs = MachineSyncInput(machines=[machine], confirm=preview.token)
        self.assertIsInstance(self.munki.machine_sync(inputs), Done)
        used = self.munki.machine_sync(inputs)
        assert isinstance(used, Refused)
        self.assertEqual(used.rule, "token_used")

    def test_a_service_without_fleet_is_refused_on_sync(self) -> None:
        self.bay()
        refused = self.reports.machine_sync(MachineSyncInput(machines=[MachineFactsInput(names=["Bay-1"], serial_number="S1")]))
        assert isinstance(refused, Refused)
        self.assertEqual((refused.rule, refused.reason), ("permission", "needs fleet"))

    def test_fleet_alone_does_not_open_other_write_verbs(self) -> None:
        refused = self.munki.camera_enrich(CameraEnrichInput(page_id=self.camera["id"], fields={"name": "Updated"}))
        assert isinstance(refused, Refused)
        self.assertEqual(refused.reason, "needs operator")

    def test_edit_previews_old_and_new_per_row_and_writes_nothing(self) -> None:
        page = self.notion.add_page(BAYS, {"Name": title("Bay 1"), "Hostname": rich_text("Bay-1"), "Username": rich_text("old")})
        preview = self.edit_preview(self.edit((["Bay-1"], {"username": "new", "has_paragon": True})))
        (item,) = preview.plan["machines"]
        self.assertEqual((item["status"], item["page_id"]), ("matched", str(page["id"])))
        self.assertEqual(item["changes"], [{"fact": "username", "old": "old", "new": "new"}, {"fact": "has_paragon", "old": None, "new": True}])
        self.assertEqual(self.patches(), 0)

    def test_edit_confirm_writes_audits_and_a_second_run_changes_nothing(self) -> None:
        page = str(self.notion.add_page(BAYS, {"Name": title("Bay 1"), "Hostname": rich_text("Bay-1"), "Username": rich_text("old")})["id"])
        inputs = self.edit((["Bay-1"], {"username": "new", "has_paragon": True, "lan_ip_address": "10.0.0.5"}))
        done = self.edit_apply(inputs)
        self.assertEqual(done.result["machines"][0]["written"], "saved")
        self.assertEqual(self.row(page, "Username")["rich_text"][0]["plain_text"], "new")  # type: ignore[index]
        self.assertEqual(self.row(page, "Has Paragon")["checkbox"], True)  # type: ignore[index]
        with self.registry._transaction() as db:
            (row,) = db.execute("SELECT project_id, actor, before, after FROM audit WHERE action = 'machine_edit'").fetchall()
            self.assertEqual(db.execute("SELECT COUNT(*) FROM activity WHERE action = 'machine_edit'").fetchone()[0], 1)
        self.assertEqual((row[0], row[1]), (page, "watchdog"))
        self.assertEqual(json.loads(row[2]), {"username": "old", "has_paragon": None, "lan_ip_address": None})
        self.assertEqual(json.loads(row[3]), {"username": "new", "has_paragon": True, "lan_ip_address": "10.0.0.5"})
        before = self.patches()
        again = self.edit_preview(inputs)
        self.assertEqual(again.plan["machines"][0]["changes"], [])
        self.assertEqual(self.patches(), before)

    def test_edit_refuses_a_property_not_on_the_list_and_the_whole_call_fails(self) -> None:
        self.bay("Bay 1", "Bay-1")
        self.bay("Bay 2", "Bay-2")
        for values in ({"serial_number": "S1"}, {"name": "Renamed"}, {"ring": "pilot"}, {"related_to_tasks_property": []}):
            refused = self.editor.machine_edit(self.edit((["Bay-1"], {"username": "ok"}), (["Bay-2"], values)))
            assert isinstance(refused, Refused)
            self.assertEqual(refused.rule, "input")
            self.assertIn("not editable", refused.reason)
        self.assertEqual(self.patches(), 0)

    def test_edit_refuses_a_value_of_the_wrong_type_with_the_reason(self) -> None:
        self.bay("Bay 1", "Bay-1")
        for values in ({"has_paragon": "yes"}, {"username": True}, {"username": 5}):
            refused = self.editor.machine_edit(self.edit((["Bay-1"], values)))
            assert isinstance(refused, Refused)
            self.assertIn("takes a", refused.reason)

    def test_edit_blank_and_left_out_values_mean_no_change(self) -> None:
        page = self.notion.add_page(BAYS, {"Name": title("Bay 1"), "Hostname": rich_text("Bay-1"), "Username": rich_text("keep"), "Has Paragon": checkbox(True)})
        done = self.edit_apply(self.edit((["Bay-1"], {"username": "", "adobe_email": None, "has_paragon": None})))
        self.assertEqual(done.result["machines"][0]["changes"], [])
        self.assertEqual(self.patches(), 0)
        self.assertEqual(self.row(str(page["id"]), "Username")["rich_text"][0]["plain_text"], "keep")  # type: ignore[index]

    def test_edit_skips_unmatched_and_ambiguous_rows_and_writes_nothing_for_them(self) -> None:
        self.bay("Bay 1", "Bay-1")
        self.bay("Bay 1 copy", "bay-1")
        done = self.edit_apply(self.edit((["Bay-1"], {"username": "a"}), (["Nowhere"], {"username": "b"})))
        self.assertEqual([m["status"] for m in done.result["machines"]], ["ambiguous", "unmatched"])
        self.assertEqual(self.patches(), 0)

    def test_edit_needs_bays_edit_not_fleet_or_reader(self) -> None:
        self.bay()
        inputs = self.edit((["Bay-1"], {"username": "a"}))
        for client in (self.munki, self.reports):
            refused = client.machine_edit(inputs)
            assert isinstance(refused, Refused)
            self.assertEqual((refused.rule, refused.reason), ("permission", "needs bays_edit"))
        refused = self.editor.machine_sync(MachineSyncInput(machines=[MachineFactsInput(names=["Bay-1"], serial_number="S1")]))
        assert isinstance(refused, Refused)
        self.assertEqual(refused.reason, "needs fleet")

    def test_edit_preview_token_works_once_and_only_for_its_edits(self) -> None:
        self.bay()
        inputs = self.edit((["Bay-1"], {"username": "a"}))
        token = self.edit_preview(inputs).token
        other = self.editor.machine_edit(self.edit((["Bay-1"], {"username": "b"})).model_copy(update={"confirm": token}))
        assert isinstance(other, Refused)
        self.assertEqual(other.rule, "token_mismatch")
        confirmed = inputs.model_copy(update={"confirm": token})
        self.assertIsInstance(self.editor.machine_edit(confirmed), Done)
        used = self.editor.machine_edit(confirmed)
        assert isinstance(used, Refused)
        self.assertEqual(used.rule, "token_used")

    def test_a_ring_that_is_not_a_ring_is_refused_at_the_boundary(self) -> None:
        with self.assertRaises(ValueError):
            MachineFactsInput.model_validate({"names": ["Bay-1"], "ring": "canary"})
        with self.assertRaises(ValueError):
            MachineFactsInput(names=["Bay-1"], last_check_in=datetime(2026, 11, 14, 4, 0))


if __name__ == "__main__":
    unittest.main()
