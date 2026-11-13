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
from t6.notion.schema import check_schema, refresh_snapshot
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

    def test_an_unknown_select_name_creates_an_option_but_an_unknown_status_name_is_refused(self) -> None:
        fake = FakeNotion()
        fake.add_data_source(
            SOURCE,
            {
                "Stage": {"id": "stg", "name": "Stage", "type": "select", "select": {"options": []}},
                "State": {"id": "st", "name": "State", "type": "status", "status": {"options": [{"id": "s1", "name": "Todo"}]}},
            },
        )
        page = fake.add_page(SOURCE, {})
        with fake.client() as client:
            client.update_page(page["id"], {"properties": {"Stage": {"select": {"name": "Grade"}}}})
            self.assertEqual(fake.select_options(SOURCE, "Stage"), ["Grade"])
            client.update_page(page["id"], {"properties": {"State": {"status": {"name": "Todo"}}}})
            with self.assertRaises(Exception) as caught:
                client.update_page(page["id"], {"properties": {"State": {"status": {"name": "Todp"}}}})
        self.assertIn("400", str(caught.exception) + str(getattr(caught.exception, "status", "")))
        self.assertEqual(fake.select_options(SOURCE, "State"), ["Todo"])

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
