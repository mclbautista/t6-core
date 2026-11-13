"""Properties and options are addressed by Notion ID, so a rename needs no code change."""

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from t6.cli import main
from t6.notion.model import NotionModel, OptionId, notion
from t6.notion.schema import check_schema, drift_markdown, fix_snapshot, refresh_snapshot
from t6.notion.testing import FakeNotion, rich_text, select, title

SOURCE = "ds-shows"
SNAPSHOT = {
    "databases": {
        "shows": {
            "title": "Shows",
            "properties": [
                {"name": "Name", "type": "title", "id": "title"},
                {"name": "Notes", "type": "rich_text", "id": "nt"},
                {"name": "Stage", "type": "select", "id": "stg", "options": ["o1"]},
            ],
        }
    }
}


@dataclass(frozen=True, slots=True, kw_only=True)
class ShowsRow(NotionModel):
    name: str | None = notion("Name", "title", id="title")
    notes: str | None = notion("Notes", "rich_text", id="nt")
    stage: str | None = notion("Stage", "select", id="stg")


def live_shows() -> FakeNotion:
    fake = FakeNotion()
    fake.add_data_source(
        SOURCE,
        {
            "Name": {"id": "title", "name": "Name", "type": "title", "title": {}},
            "Notes": {"id": "nt", "name": "Notes", "type": "rich_text", "rich_text": {}},
            "Stage": {
                "id": "stg",
                "name": "Stage",
                "type": "select",
                "select": {"options": [{"id": "o1", "name": "Edit"}]},
            },
        },
    )
    return fake


class RenameTests(unittest.TestCase):
    def test_reads_and_writes_survive_a_property_and_option_rename(self) -> None:
        fake = live_shows()
        page = fake.add_page(SOURCE, {"Name": title("Show"), "Notes": rich_text("hi"), "Stage": select("Edit")})
        fake.rename_property(SOURCE, "Notes", "Production notes")
        fake.rename_option(SOURCE, "Stage", "Edit", "Editing")
        with fake.client() as client:
            row = ShowsRow.from_page(client.retrieve_page(page["id"]))
            self.assertEqual((row.notes, row.stage), ("hi", "Editing"))
            body = ShowsRow.to_properties(notes="updated", stage=OptionId("o1"))
            self.assertEqual(set(body), {"nt", "stg"})
            self.assertEqual(body["stg"], {"select": {"id": "o1"}})
            updated = ShowsRow.from_page(client.update_page(page["id"], {"properties": body}))
        self.assertEqual((updated.notes, updated.stage), ("updated", "Editing"))
        self.assertEqual(fake.page(page["id"])["properties"]["Production notes"]["rich_text"][0]["plain_text"], "updated")

    def test_a_plain_option_name_is_still_written_by_name(self) -> None:
        fake = live_shows()
        page = fake.add_page(SOURCE, {"Name": title("Show")})
        with fake.client() as client:
            row = ShowsRow.from_page(client.update_page(page["id"], {"properties": ShowsRow.to_properties(stage="Grade")}))
        self.assertEqual(row.stage, "Grade")

    def test_a_property_with_no_id_is_read_and_written_by_name(self) -> None:
        @dataclass(frozen=True, slots=True, kw_only=True)
        class Legacy(NotionModel):
            notes: str | None = notion("Notes", "rich_text")

        self.assertEqual(Legacy.to_properties(notes="x").keys(), {"Notes"})
        self.assertEqual(Legacy.from_page({"id": "p", "properties": {"Notes": rich_text("a")}}).notes, "a")


class SchemaCheckIdTests(unittest.TestCase):
    def report(self, fake: FakeNotion) -> dict[str, object]:
        with fake.client() as client:
            return check_schema(client, SNAPSHOT, {"shows": SOURCE})

    def test_a_rename_is_reported_as_a_rename_and_is_not_drift(self) -> None:
        fake = live_shows()
        fake.rename_property(SOURCE, "Notes", "Production notes")
        report = self.report(fake)
        shows = report["databases"]["shows"]  # type: ignore[index]
        self.assertTrue(report["ok"])
        self.assertEqual(shows["status"], "ok")
        self.assertEqual(shows["renamed"], [{"from": "Notes", "to": "Production notes"}])

    def test_a_deleted_or_new_property_is_drift(self) -> None:
        fake = live_shows()
        fake.remove_property(SOURCE, "Notes")
        fake.add_property(SOURCE, "Rack", "rich_text")
        report = self.report(fake)
        shows = report["databases"]["shows"]  # type: ignore[index]
        self.assertFalse(report["ok"])
        self.assertEqual(shows["removed"], [{"name": "Notes", "type": "rich_text"}])
        self.assertEqual(shows["added"], [{"name": "Rack", "type": "rich_text"}])


class FixSnapshotTests(unittest.TestCase):
    def run_fix(self, fake: FakeNotion) -> tuple[int, str, Path]:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        base = Path(directory.name)
        (base / "ds.json").write_text(json.dumps({"shows": SOURCE}), encoding="utf-8")
        snapshot = base / "snapshot.json"
        snapshot.write_text(json.dumps(SNAPSHOT), encoding="utf-8")
        out = io.StringIO()
        with (
            mock.patch.dict(os.environ, {"T6_NOTION_TOKEN": "secret"}, clear=True),
            redirect_stdout(out),
            redirect_stderr(io.StringIO()),
        ):
            status = main(
                ["schema-check", "--data-sources", str(base / "ds.json"), "--snapshot", str(snapshot), "--fix-snapshot"],
                open_notion=lambda _token: fake.client(),
            )
        return status, out.getvalue(), snapshot

    def test_a_rename_updates_the_name_and_keeps_the_id(self) -> None:
        fake = live_shows()
        fake.rename_property(SOURCE, "Notes", "Production notes")
        status, out, snapshot = self.run_fix(fake)
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(out)["snapshot_fixed"], str(snapshot))
        properties = json.loads(snapshot.read_text(encoding="utf-8"))["databases"]["shows"]["properties"]
        self.assertEqual(properties[1], {"name": "Production notes", "type": "rich_text", "id": "nt"})
        with fake.client() as client:
            self.assertTrue(check_schema(client, json.loads(snapshot.read_text(encoding="utf-8")), {"shows": SOURCE})["ok"])

    def test_a_deleted_or_new_property_changes_nothing_and_exits_one(self) -> None:
        for change in ("remove", "add"):
            with self.subTest(change=change):
                fake = live_shows()
                fake.rename_property(SOURCE, "Notes", "Production notes")
                if change == "remove":
                    fake.remove_property(SOURCE, "Stage")
                else:
                    fake.add_property(SOURCE, "Rack", "number")
                status, _, snapshot = self.run_fix(fake)
                self.assertEqual(status, 1)
                self.assertEqual(json.loads(snapshot.read_text(encoding="utf-8")), SNAPSHOT)

    def test_a_type_change_is_refused(self) -> None:
        fake = live_shows()
        fake.change_property_type(SOURCE, "Notes", "number")
        with fake.client() as client:
            report = check_schema(client, SNAPSHOT, {"shows": SOURCE})
        with self.assertRaises(ValueError):
            fix_snapshot(SNAPSHOT, report)


class DriftMarkdownTests(unittest.TestCase):
    def markdown(self, fake: FakeNotion) -> str:
        with fake.client() as client:
            return drift_markdown(check_schema(client, SNAPSHOT, {"shows": SOURCE}))

    def test_no_drift_and_a_rename_alone_give_an_empty_body(self) -> None:
        self.assertEqual(self.markdown(live_shows()), "")
        fake = live_shows()
        fake.rename_property(SOURCE, "Notes", "Production notes")
        self.assertEqual(self.markdown(fake), "")

    def test_drift_lists_names_and_types_and_no_ids_or_options(self) -> None:
        fake = live_shows()
        fake.add_property(SOURCE, "Rack", "number")
        fake.remove_property(SOURCE, "Stage")
        fake.rename_property(SOURCE, "Notes", "Production notes")
        body = self.markdown(fake)
        self.assertIn("added: `Rack` (number)", body)
        self.assertIn("removed: `Stage` (select)", body)
        self.assertIn("`Notes` to `Production notes`", body)
        for private in (SOURCE, "Edit", "o1"):
            self.assertNotIn(private, body)

    def test_an_unreadable_database_is_drift(self) -> None:
        with FakeNotion().client() as client:
            body = drift_markdown(check_schema(client, SNAPSHOT, {"shows": SOURCE}))
        self.assertIn("could not be read", body)


class RefreshSnapshotTests(unittest.TestCase):
    def test_it_records_property_and_option_ids_and_never_option_names(self) -> None:
        fake = live_shows()
        fake.rename_property(SOURCE, "Notes", "Production notes")
        fake.add_property(SOURCE, "Rack", "number")
        with fake.client() as client:
            refreshed = refresh_snapshot(client, SNAPSHOT, {"shows": SOURCE})
        properties = refreshed["databases"]["shows"]["properties"]
        self.assertEqual([p["name"] for p in properties], ["Name", "Production notes", "Stage", "Rack"])
        self.assertEqual(properties[2], {"name": "Stage", "type": "select", "id": "stg", "options": ["o1"]})
        self.assertNotIn("Edit", json.dumps(refreshed))
        with fake.client() as client:
            self.assertTrue(check_schema(client, refreshed, {"shows": SOURCE})["ok"])

    def test_the_command_writes_the_refreshed_snapshot(self) -> None:
        fake = live_shows()
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "in.json").write_text(json.dumps(SNAPSHOT), encoding="utf-8")
            (base / "ds.json").write_text(json.dumps({"shows": SOURCE}), encoding="utf-8")
            with (
                mock.patch.dict(os.environ, {"T6_NOTION_TOKEN": "secret"}, clear=True),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                status = main(
                    ["schema-snapshot", "--data-sources", str(base / "ds.json"),
                     "--snapshot", str(base / "in.json"), "--out", str(base / "out.json")],
                    open_notion=lambda _token: fake.client(),
                )
            written = json.loads((base / "out.json").read_text(encoding="utf-8"))
        self.assertEqual(status, 0)
        self.assertEqual(written["databases"]["shows"]["properties"][2]["options"], ["o1"])


if __name__ == "__main__":
    unittest.main()
