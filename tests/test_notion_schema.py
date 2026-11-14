"""Exercise the schema snapshot, generated models and `t6 schema-check`.

All Notion traffic goes to the in-memory fake.
"""

from contextlib import redirect_stderr, redirect_stdout
import datetime as dt
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from t6.cli import main
from t6.notion import NotionError
from t6.notion.generated import MODELS, BaysRow, JobOrdersRow, ProjectsRow
from t6.notion.model import NotionModel
from t6.notion.schema import check_schema, load_snapshot
from t6.notion.testing import FakeNotion, project_properties
from t6.registry import Rotation, SqliteProjectRegistry

ROOT = Path(__file__).resolve().parents[1]
JOB_ORDERS = "ds-job-orders"
BAYS = "ds-bays"
PROJECTS = "ds-projects"


def key(database: str, name: str) -> str:
    """The body key a model writes for a snapshot property: its ID once the snapshot has one."""
    prop = next(p for p in load_snapshot()["databases"][database]["properties"] if p["name"] == name)
    return str(prop.get("id") or name)


def fake_with(**databases: str) -> FakeNotion:
    fake = FakeNotion()
    for key, data_source_id in databases.items():
        fake.add_snapshot_data_source(data_source_id, key)
    return fake


class SnapshotTests(unittest.TestCase):
    def test_every_snapshot_database_has_a_generated_model(self) -> None:
        snapshot = load_snapshot()
        self.assertEqual(set(MODELS), set(snapshot["databases"]))
        for key, database in snapshot["databases"].items():
            with self.subTest(database=key):
                model = MODELS[key]
                self.assertTrue(issubclass(model, NotionModel))
                self.assertEqual(model.DATABASE, key)
                fields = model.notion_fields().values()
                self.assertEqual(
                    [(f.metadata["notion_name"], f.metadata["notion_type"]) for f in fields],
                    [(prop["name"], prop["type"]) for prop in database["properties"]],
                )

    def test_generated_models_match_the_snapshot(self) -> None:
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "generate_models.py"), "--check"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_snapshot_holds_no_option_names(self) -> None:
        for database in load_snapshot()["databases"].values():
            for prop in database["properties"]:
                self.assertLessEqual(set(prop), {"name", "type", "id", "options"})
                self.assertTrue(all(isinstance(option, str) for option in prop.get("options", [])))


class ModelReadWriteTests(unittest.TestCase):
    def test_a_property_with_a_trailing_space_reads_and_writes(self) -> None:
        fake = fake_with(job_orders=JOB_ORDERS)
        body = JobOrdersRow.to_properties(name="JO 1", blocker="Waiting for drive")
        self.assertIn(key("job_orders", "Blocker "), body)
        self.assertNotIn("Blocker", body)
        with fake.client() as client:
            created = client.create_page(
                {"parent": {"data_source_id": JOB_ORDERS}, "properties": body}
            )
            row = JobOrdersRow.from_page(client.retrieve_page(created["id"]))
        self.assertEqual(row.blocker, "Waiting for drive")
        self.assertEqual(row.name, "JO 1")
        self.assertEqual(row.page_id, created["id"])

    def test_a_property_with_an_emoji_reads_and_writes(self) -> None:
        fake = fake_with(job_orders=JOB_ORDERS)
        body = JobOrdersRow.to_properties(name="JO 2", coordinator=("contact-1",))
        self.assertIn(key("job_orders", "\U0001f5c2️ Coordinator"), body)
        with fake.client() as client:
            created = client.create_page(
                {"parent": {"data_source_id": JOB_ORDERS}, "properties": body}
            )
            row = JobOrdersRow.from_page(client.retrieve_page(created["id"]))
        self.assertEqual(row.coordinator, ("contact-1",))

    def test_notion_rejects_a_name_without_its_trailing_space(self) -> None:
        fake = fake_with(job_orders=JOB_ORDERS)
        with fake.client() as client, self.assertRaises(NotionError) as caught:
            client.create_page(
                {
                    "parent": {"data_source_id": JOB_ORDERS},
                    "properties": {"Blocker": {"rich_text": []}},
                }
            )
        self.assertEqual(caught.exception.status, 400)

    def test_a_live_property_that_is_not_in_the_snapshot_does_not_break_reads(self) -> None:
        fake = fake_with(bays=BAYS)
        fake.add_property(BAYS, "Rack Position", "rich_text")
        fake.add_property(BAYS, "Surprise", "checkbox")
        with fake.client() as client:
            page = client.create_page(
                {
                    "parent": {"data_source_id": BAYS},
                    "properties": {
                        **BaysRow.to_properties(name="Bay 01", hostname="bay01"),
                        "Rack Position": {"rich_text": [{"text": {"content": "A3"}}]},
                        "Surprise": {"checkbox": True},
                    },
                }
            )
            row = BaysRow.from_page(client.retrieve_page(page["id"]))
        self.assertEqual(row.hostname, "bay01")

    def test_a_missing_or_wrong_typed_property_reads_as_empty(self) -> None:
        row = ProjectsRow.from_page(
            {"id": "p1", "properties": {"Eps": {"type": "rich_text", "rich_text": []}}}
        )
        self.assertIsNone(row.eps)
        self.assertEqual(row.studio, ())
        self.assertIsNone(row.project_code)

    def test_read_only_and_unknown_attributes_cannot_be_written(self) -> None:
        with self.assertRaises(TypeError):
            ProjectsRow.to_properties(created_time=None)
        with self.assertRaises(TypeError):
            ProjectsRow.to_properties(no_such_property="x")

    def test_each_type_round_trips_through_the_fake(self) -> None:
        fake = fake_with(projects=PROJECTS)
        body = ProjectsRow.to_properties(
            name="Harbour Lights",
            eps=6,
            project_code="HBL",
            delivery_format=("ProRes", "DCP"),
            studio=("studio-1",),
            tech_notes="Rec.709",
        )
        with fake.client() as client:
            created = client.create_page({"parent": {"data_source_id": PROJECTS}, "properties": body})
            row = ProjectsRow.from_page(client.retrieve_page(created["id"]))
        self.assertEqual(
            (row.name, row.eps, row.project_code, row.delivery_format, row.studio, row.tech_notes),
            ("Harbour Lights", 6, "HBL", ("ProRes", "DCP"), ("studio-1",), "Rec.709"),
        )

    def test_the_munki_fleet_fields_round_trip_on_a_bays_page(self) -> None:
        fake = fake_with(bays=BAYS)
        checked_in = dt.datetime(2026, 11, 9, 3, 15, tzinfo=dt.timezone.utc)
        body = BaysRow.to_properties(
            name="Bay 06",
            serial_number="C02EXAMPLE01",
            munki_manifest="bay06",
            munki_client_version="6.6.3",
            ring="pilot",
            last_check_in=checked_in,
            os_version="macOS 26.5",
        )
        self.assertIn("rich_text", body[key("bays", "OS Version")])
        with fake.client() as client:
            created = client.create_page({"parent": {"data_source_id": BAYS}, "properties": body})
            row = BaysRow.from_page(client.retrieve_page(created["id"]))
        self.assertEqual(
            (row.serial_number, row.munki_manifest, row.munki_client_version, row.ring),
            ("C02EXAMPLE01", "bay06", "6.6.3", "pilot"),
        )
        self.assertEqual(row.last_check_in, checked_in)
        self.assertEqual(row.os_version, "macOS 26.5")
        self.assertEqual(fake.select_options(BAYS, "Ring"), ["pilot"])


class SelectOptionTests(unittest.TestCase):
    def test_writing_a_new_bays_ring_creates_the_select_option(self) -> None:
        fake = fake_with(bays=BAYS)
        self.assertEqual(fake.select_options(BAYS, "Ring"), [])
        with fake.client() as client:
            client.create_page(
                {
                    "parent": {"data_source_id": BAYS},
                    "properties": BaysRow.to_properties(name="Bay 02", ring="pilot"),
                }
            )
            live = client.retrieve_data_source(BAYS)
        self.assertEqual([o["name"] for o in live["properties"]["Ring"]["select"]["options"]], ["pilot"])

    def test_an_existing_option_is_not_added_twice(self) -> None:
        fake = fake_with(bays=BAYS)
        with fake.client() as client:
            for name in ("Bay 03", "Bay 04"):
                client.create_page(
                    {
                        "parent": {"data_source_id": BAYS},
                        "properties": BaysRow.to_properties(name=name, ring="production"),
                    }
                )
        self.assertEqual(fake.select_options(BAYS, "Ring"), ["production"])

    def test_a_multi_select_value_creates_its_options(self) -> None:
        fake = fake_with(bays=BAYS)
        with fake.client() as client:
            client.create_page(
                {
                    "parent": {"data_source_id": BAYS},
                    "properties": BaysRow.to_properties(name="Bay 05", type=("MacDesktop", "NAS")),
                }
            )
        self.assertEqual(fake.select_options(BAYS, "Type"), ["MacDesktop", "NAS"])


class SchemaCheckTests(unittest.TestCase):
    def check(
        self, fake: FakeNotion, data_sources: dict[str, str], registry: SqliteProjectRegistry | None = None
    ) -> dict[str, object]:
        with fake.client() as client:
            return check_schema(client, load_snapshot(), data_sources, registry)

    def test_a_matching_schema_is_ok(self) -> None:
        fake = fake_with(bays=BAYS, projects=PROJECTS)
        page = str(fake.add_page(PROJECTS, project_properties(name="Show A", project_code="AAA"))["id"])
        with tempfile.TemporaryDirectory() as directory:
            registry = SqliteProjectRegistry(Path(directory) / "registry.sqlite3")
            registry.register(actor="test", idempotency_key="k", notion_page_id=page, code="AAA", rotation=Rotation.IN)
            report = self.check(fake, {"bays": BAYS, "projects": PROJECTS}, registry)
        self.assertTrue(report["ok"])
        self.assertEqual(report["databases"]["bays"]["status"], "ok")  # type: ignore[index]
        self.assertEqual(len(report["skipped"]), 9)  # type: ignore[arg-type]

    def test_it_reports_added_removed_and_changed_properties(self) -> None:
        fake = fake_with(bays=BAYS)
        fake.add_property(BAYS, "Rack Position", "rich_text")
        fake.remove_property(BAYS, "Notes")
        fake.change_property_type(BAYS, "OS Version", "select")
        report = self.check(fake, {"bays": BAYS})
        self.assertFalse(report["ok"])
        bays = report["databases"]["bays"]  # type: ignore[index]
        self.assertEqual(bays["status"], "drift")
        self.assertEqual(bays["added"], [{"name": "Rack Position", "type": "rich_text"}])
        self.assertEqual(bays["removed"], [{"name": "Notes", "type": "rich_text"}])
        self.assertEqual(
            bays["changed"],
            [{"name": "OS Version", "snapshot_type": "rich_text", "live_type": "select"}],
        )

    def test_a_renamed_trailing_space_counts_as_drift(self) -> None:
        fake = fake_with(job_orders=JOB_ORDERS)
        fake.remove_property(JOB_ORDERS, "Blocker ")
        fake.add_property(JOB_ORDERS, "Blocker", "rich_text")
        report = self.check(fake, {"job_orders": JOB_ORDERS})
        job_orders = report["databases"]["job_orders"]  # type: ignore[index]
        self.assertEqual(job_orders["added"], [{"name": "Blocker", "type": "rich_text"}])
        self.assertEqual(job_orders["removed"], [{"name": "Blocker ", "type": "rich_text"}])

    def test_a_failed_read_is_reported_not_raised(self) -> None:
        fake = fake_with(bays=BAYS)
        report = self.check(fake, {"bays": "missing-id"})
        self.assertFalse(report["ok"])
        error = report["databases"]["bays"]["error"]  # type: ignore[index]
        self.assertEqual((error["status"], error["code"]), (404, "object_not_found"))

    def test_a_data_source_for_an_unknown_database_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.check(FakeNotion(), {"nope": "x"})


class SchemaCheckCommandTests(unittest.TestCase):
    def run_command(
        self, fake: FakeNotion, data_sources: dict[str, str], *, token: str | None = "secret"
    ) -> tuple[int, str, str]:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data-sources.json"
            path.write_text(json.dumps(data_sources), encoding="utf-8")
            environment = {"T6_NOTION_TOKEN": token} if token else {}
            out, err = io.StringIO(), io.StringIO()
            with (
                mock.patch.dict(os.environ, environment, clear=True),
                redirect_stdout(out),
                redirect_stderr(err),
            ):
                status = main(
                    ["admin", "schema-check", "--data-sources", str(path)],
                    open_notion=lambda _token: fake.client(),
                )
        return status, out.getvalue(), err.getvalue()

    def test_a_clean_check_prints_json_and_exits_zero(self) -> None:
        fake = fake_with(bays=BAYS)
        status, out, err = self.run_command(fake, {"bays": BAYS})
        self.assertEqual(status, 0)
        self.assertEqual(err, "")
        self.assertTrue(json.loads(out)["ok"])

    def test_drift_prints_json_and_exits_one(self) -> None:
        fake = fake_with(bays=BAYS)
        fake.add_property(BAYS, "Rack Position", "rich_text")
        status, out, _ = self.run_command(fake, {"bays": BAYS})
        self.assertEqual(status, 1)
        report = json.loads(out)
        self.assertEqual(report["databases"]["bays"]["added"][0]["name"], "Rack Position")

    def test_a_missing_token_exits_two_with_a_json_error(self) -> None:
        status, out, err = self.run_command(FakeNotion(), {}, token=None)
        self.assertEqual(status, 2)
        self.assertEqual(out, "")
        self.assertIn("T6_NOTION_TOKEN", json.loads(err)["error"])

    def test_the_command_runs_as_a_process(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "t6", "admin", "schema-check", "--data-sources", "missing.json"],
            capture_output=True,
            text=True,
            env={k: v for k, v in os.environ.items() if k != "T6_NOTION_TOKEN"},
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("error", json.loads(result.stderr))


if __name__ == "__main__":
    unittest.main()
