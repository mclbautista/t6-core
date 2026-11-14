"""The full Job Order domain model: read and write in studio terms against the fake Notion."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any
import unittest

from t6.domain import Department, JobOrder, JobOrderStatus
from t6.notion.generated import JobOrdersRow
from t6.notion.model import encode
from t6.notion.job_orders import FIELDS, RELATIONS, UNMAPPED, JobOrders
from t6.notion.schema import check_schema, load_snapshot, refresh_snapshot
from t6.notion.testing import FakeNotion
from t6.notion.writes import Ambiguous, Rejected
from t6.privacy import WITHHELD

JO = "ds-job-orders"
MANILA = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parents[1]


def live_job_orders() -> FakeNotion:
    fake = FakeNotion()
    fake.add_snapshot_data_source(JO, "job_orders")
    for status in JobOrderStatus:
        fake.add_option(JO, "Status", status.value)
    for department in Department:
        fake.add_option(JO, "Department", department.value)
    return fake


FULL: dict[str, Any] = {
    "name": "Conform ep 101",
    "number": "12",
    "department": Department.OE,
    "date": datetime(2026, 11, 13, 9, 0, tzinfo=MANILA),
    "deadline": datetime(2026, 11, 14, 23, 59, tzinfo=MANILA),
    "title": "EP101",
    "subtitle": "Locked cut",
    "task": "Conform",
    "purpose": "Review",
    "source": "Drive A",
    "platform": "Frame.io",
    "link_settings": "view only",
    "watermark": "T6",
    "mode_of_transfer": "link",
    "blocker": "waiting on audio",
    "pending": "audio",
    "notes": "rush",
    "watchdog_notes": "flagged",
    "telegram_key": "-100:55",
    "status": JobOrderStatus.NEEDS_INTAKE_REVIEW,
    "project_id": "11111111111111111111111111111111",
    "assigned_ids": ("22222222222222222222222222222222",),
    "cc_ids": ("33333333333333333333333333333333",),
    "coordinator_ids": ("44444444444444444444444444444444",),
    "deliverable_ids": ("55555555555555555555555555555555",),
    "source_text": "Line one\n\nLine three",
}


class ModelTests(unittest.TestCase):
    def test_every_job_orders_property_maps_to_a_field_or_is_unmapped(self) -> None:
        mapped = {*FIELDS.values(), *RELATIONS.values(), "projects", "health"}
        attributes = set(JobOrdersRow.notion_fields())
        self.assertEqual(attributes - mapped, set(UNMAPPED))
        self.assertEqual(mapped - attributes, set())

    def test_no_notion_property_name_appears_outside_the_generated_mapping(self) -> None:
        names = [str(p["name"]) for p in load_snapshot()["databases"]["job_orders"]["properties"]]
        for path in ("src/t6/domain/__init__.py", "src/t6/notion/job_orders.py"):
            source = (ROOT / path).read_text(encoding="utf-8")
            for name in (n for n in names if not n.isalnum()):  # plain words such as Task are also domain words
                self.assertNotIn(f'"{name}"', source, f"{name!r} in {path}")


class CreateAndReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = live_job_orders()
        self.client = self.fake.client()
        self.addCleanup(self.client.close)
        self.orders = JobOrders(self.client, JO)

    def test_create_returns_every_field_as_saved_including_the_body(self) -> None:
        order = self.orders.create(FULL)
        self.assertIsInstance(order, JobOrder, order)
        assert isinstance(order, JobOrder)
        expected = {**FULL, "date": FULL["date"], "status": "Needs Intake Review", "department": "OE"}
        for name, value in expected.items():
            self.assertEqual(getattr(order, name), value, name)
        self.assertIsInstance(order.status, JobOrderStatus)
        self.assertIsInstance(order.department, Department)
        self.assertIsNone(order.health)

    def test_get_find_by_telegram_key_and_list(self) -> None:
        first = self.orders.create({"name": "A", "telegram_key": "k1", "source_text": "hello"})
        second = self.orders.create({"name": "B", "telegram_key": "k2"})
        assert isinstance(first, JobOrder) and isinstance(second, JobOrder)
        self.assertEqual(self.orders.get(first.page_id).source_text, "hello")
        found = self.orders.find_by_telegram_key("k2")
        assert found is not None
        self.assertEqual((found.page_id, found.source_text), (second.page_id, None))
        self.assertIsNone(self.orders.find_by_telegram_key("nope"))
        self.assertEqual([o.name for o in self.orders.list()], ["A", "B"])

    def test_find_all_by_telegram_key_returns_every_match_without_bodies(self) -> None:
        first = self.orders.create({"name": "A", "telegram_key": "k", "source_text": "hello"})
        self.orders.create({"name": "B", "telegram_key": "other"})
        second = self.orders.create({"name": "C", "telegram_key": "k"})
        assert isinstance(first, JobOrder) and isinstance(second, JobOrder)
        found = self.orders.find_all_by_telegram_key("k")
        self.assertEqual([o.page_id for o in found], [first.page_id, second.page_id])
        self.assertEqual([o.source_text for o in found], [None, None])
        self.assertEqual(self.orders.find_all_by_telegram_key("nope"), ())

    def test_a_long_text_field_is_sent_in_parts_of_2000_characters(self) -> None:
        parts = encode("rich_text", "x" * 4500)["rich_text"]
        self.assertEqual([len(p["text"]["content"]) for p in parts], [2000, 2000, 500])
        order = self.orders.create({"name": "A", "notes": "y" * 4500})
        assert isinstance(order, JobOrder)
        self.assertEqual(order.notes, "y" * 4500)

    def test_a_credential_in_the_body_or_a_field_is_withheld(self) -> None:
        order = self.orders.create({"name": "A", "notes": "pin=1", "source_text": "password: x"})
        assert isinstance(order, JobOrder)
        self.assertIn(WITHHELD, order.notes or "")
        self.assertIn(WITHHELD, order.source_text or "")
        self.assertNotIn("password: x", order.source_text or "")

    def test_a_long_body_line_is_split_and_reads_back_whole(self) -> None:
        text = "x" * 4500
        order = self.orders.create({"name": "A", "source_text": text})
        assert isinstance(order, JobOrder)
        self.assertEqual(order.source_text, text)

    def test_unwritable_and_unknown_fields_raise(self) -> None:
        for field in ("health", "page_id", "colour"):
            with self.assertRaises(TypeError):
                self.orders.create({"name": "A", field: "x"})

    def test_a_refused_write_is_rejected_and_a_timeout_is_ambiguous(self) -> None:
        self.fake.fail_next(400, code="validation_error")
        self.assertIsInstance(self.orders.create({"name": "A"}), Rejected)
        self.fake.time_out_next()
        self.assertIsInstance(self.orders.create({"name": "B"}), Ambiguous)

    def test_a_body_that_fails_to_save_is_ambiguous_with_the_page(self) -> None:
        self.fake.fail_next(500, after=3)  # create, readback, then the append fails
        result = self.orders.create({"name": "A", "source_text": "hi"})
        assert isinstance(result, Ambiguous)
        self.assertIsNotNone(result.page_id)
        self.assertEqual(self.fake.page_count(), 1)


class NamedValueTests(unittest.TestCase):
    def snapshot(self, fake: FakeNotion) -> dict[str, Any]:
        with fake.client() as client:
            return refresh_snapshot(client, load_snapshot(), {"job_orders": JO})

    def test_refresh_records_option_ids_and_never_option_names(self) -> None:
        snapshot = self.snapshot(live_job_orders())
        status = snapshot["databases"]["job_orders"]["named_options"]["status"]
        self.assertEqual(set(status["values"]), {m.name for m in JobOrderStatus})
        self.assertNotIn("Needs Intake Review", json.dumps(snapshot))

    def test_reads_and_writes_survive_a_status_rename(self) -> None:
        fake = live_job_orders()
        snapshot = self.snapshot(fake)
        fake.rename_option(JO, "Status", "Needs Intake Review", "Intake check")
        with fake.client() as client:
            orders = JobOrders(client, JO, snapshot)
            order = orders.create({"name": "A", "status": JobOrderStatus.NEEDS_INTAKE_REVIEW})
            assert isinstance(order, JobOrder)
            self.assertIs(order.status, JobOrderStatus.NEEDS_INTAKE_REVIEW)
            self.assertEqual(
                fake.page(order.page_id)["properties"]["Status"]["status"]["name"], "Intake check"
            )

    def test_an_unknown_status_reads_as_its_plain_name(self) -> None:
        fake = live_job_orders()
        fake.add_option(JO, "Status", "On hold")
        with fake.client() as client:
            order = JobOrders(client, JO).create({"name": "A", "status": "On hold"})
        assert isinstance(order, JobOrder)
        self.assertEqual(order.status, "On hold")
        self.assertNotIsInstance(order.status, JobOrderStatus)

    def test_schema_check_flags_a_named_value_that_is_no_longer_an_option(self) -> None:
        fake = live_job_orders()
        snapshot = self.snapshot(fake)
        with fake.client() as client:
            self.assertEqual(check_schema(client, snapshot, {"job_orders": JO})["databases"]["job_orders"]["named_missing"], [])
        fake.rename_option(JO, "Status", "Done", "Finished")
        with fake.client() as client:
            report = check_schema(client, snapshot, {"job_orders": JO})
        self.assertEqual(report["databases"]["job_orders"]["named_missing"], [])  # renamed: found by ID
        no_ids = load_snapshot()
        for block in no_ids["databases"]["job_orders"]["named_options"].values():
            block["values"] = {}
        with fake.client() as client:
            report = check_schema(client, no_ids, {"job_orders": JO})  # no IDs recorded: by label
        self.assertEqual(
            report["databases"]["job_orders"]["named_missing"], [{"attribute": "status", "value": "DONE"}]
        )
        self.assertFalse(report["ok"])


if __name__ == "__main__":
    unittest.main()
