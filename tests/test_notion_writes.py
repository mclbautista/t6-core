"""Typed writes: the saved row, or `Rejected` or `Ambiguous`. Notion is the in-memory fake."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any
import unittest

from t6.notion.model import NotionModel, OptionId, notion
from t6.notion.sync import NotionClient
from t6.notion.testing import FakeNotion
from t6.notion.writes import Ambiguous, Rejected, create_row, update_row

SOURCE = "ds-shows"
MANILA = timezone(timedelta(hours=8))


@dataclass(frozen=True, slots=True, kw_only=True)
class ShowsRow(NotionModel):
    name: str | None = notion("Name", "title", id="title")
    notes: str | None = notion("Notes", "rich_text", id="nt")
    stage: str | None = notion("Stage", "select", id="stg")
    tags: tuple[str, ...] = notion("Tags", "multi_select", id="tg", default=())
    due: date | datetime | None = notion("Due", "date", id="du")
    cast: tuple[str, ...] = notion("Cast", "relation", id="cs", default=())
    live: bool | None = notion("Live", "checkbox", id="lv")
    eps: float | None = notion("Eps", "number", id="ep")


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
                "select": {"options": [{"id": "o1", "name": "Edit"}, {"id": "o2", "name": "Color"}]},
            },
            "Tags": {"id": "tg", "name": "Tags", "type": "multi_select", "multi_select": {"options": []}},
            "Due": {"id": "du", "name": "Due", "type": "date", "date": {}},
            "Cast": {"id": "cs", "name": "Cast", "type": "relation", "relation": {}},
            "Live": {"id": "lv", "name": "Live", "type": "checkbox", "checkbox": {}},
            "Eps": {"id": "ep", "name": "Eps", "type": "number", "number": {}},
        },
    )
    return fake


class Altered(NotionClient):
    """A client whose readback shows a different Notes than was written."""

    def retrieve_page(self, page_id: str) -> dict[str, Any]:
        page = super().retrieve_page(page_id)
        page["properties"]["Notes"]["rich_text"] = [{"plain_text": "changed elsewhere"}]
        return page


class DropsSeconds(NotionClient):
    """A client whose readback drops seconds from dates, as Notion does."""

    def retrieve_page(self, page_id: str) -> dict[str, Any]:
        page = super().retrieve_page(page_id)
        date_prop = page["properties"]["Due"]["date"]
        date_prop["start"] = date_prop["start"][:16] + "+08:00"
        return page


class SuccessTests(unittest.TestCase):
    def test_create_returns_the_saved_row(self) -> None:
        fake = live_shows()
        due = datetime(2026, 11, 13, 23, 59, 30, tzinfo=MANILA)
        values: dict[str, Any] = {
            "name": "Show A",
            "notes": "hello",
            "stage": OptionId("o2"),
            "tags": ("x", "y"),
            "due": due,
            "cast": ("11111111-1111-1111-1111-111111111111",),
            "live": True,
            "eps": 8,
        }
        with fake.client() as client:
            row = create_row(client, ShowsRow, SOURCE, values)
        assert isinstance(row, ShowsRow)
        self.assertEqual((row.name, row.notes, row.stage), ("Show A", "hello", "Color"))
        self.assertEqual(row.tags, ("x", "y"))
        self.assertEqual(row.eps, 8)
        self.assertEqual(fake.page_count(), 1)

    def test_update_by_name_or_clear_returns_the_saved_row(self) -> None:
        fake = live_shows()
        with fake.client() as client:
            created = create_row(client, ShowsRow, SOURCE, {"name": "A", "stage": "Edit", "notes": "n"})
            assert isinstance(created, ShowsRow)
            row = update_row(client, ShowsRow, created.page_id, {"stage": None, "notes": None, "live": False})
        assert isinstance(row, ShowsRow)
        self.assertEqual((row.stage, row.notes, row.live), (None, None, False))

    def test_to_domain_converts_the_saved_row(self) -> None:
        with live_shows().client() as client:
            result = create_row(client, ShowsRow, SOURCE, {"name": "A"}, to_domain=lambda row: row.name)
        self.assertEqual(result, "A")

    def test_a_date_with_seconds_matches_the_minute_notion_keeps(self) -> None:
        fake = live_shows()
        with fake.client() as client:
            row = create_row(client, ShowsRow, SOURCE, {"due": datetime(2026, 11, 13, 23, 59, 30, tzinfo=MANILA)})
            self.assertIsInstance(row, ShowsRow)
            row = create_row(client, ShowsRow, SOURCE, {"due": date(2026, 11, 13)})
            self.assertIsInstance(row, ShowsRow)

    def test_a_readback_without_seconds_still_matches(self) -> None:
        fake = live_shows()
        with DropsSeconds("fake-token", transport=fake.transport) as client:
            row = create_row(client, ShowsRow, SOURCE, {"due": datetime(2026, 11, 13, 23, 59, 30, tzinfo=MANILA)})
        self.assertIsInstance(row, ShowsRow)


class FailureTests(unittest.TestCase):
    def test_a_4xx_is_rejected(self) -> None:
        for status in (400, 404, 429):
            with self.subTest(status=status):
                fake = live_shows()
                fake.fail_next(status, code="validation_error")
                with fake.client() as client:
                    result = create_row(client, ShowsRow, SOURCE, {"name": "A"})
                self.assertEqual(result, Rejected(status, "validation_error", "injected failure"))
                self.assertEqual(fake.page_count(), 0)

    def test_a_timeout_after_sending_is_ambiguous(self) -> None:
        fake = live_shows()
        fake.time_out_next()
        with fake.client() as client:
            result = create_row(client, ShowsRow, SOURCE, {"name": "A"})
        assert isinstance(result, Ambiguous)
        self.assertEqual((result.reason, result.page_id), ("timeout", None))

    def test_a_5xx_on_update_is_ambiguous_with_the_page_id(self) -> None:
        fake = live_shows()
        page = str(fake.add_page(SOURCE, {})["id"])
        fake.fail_next(503)
        with fake.client() as client:
            result = update_row(client, ShowsRow, page, {"name": "A"})
        assert isinstance(result, Ambiguous)
        self.assertEqual((result.reason, result.page_id), ("server_error", page))

    def test_a_failed_readback_is_ambiguous(self) -> None:
        fake = live_shows()
        fake.fail_next(500, after=1)
        with fake.client() as client:
            result = create_row(client, ShowsRow, SOURCE, {"name": "A"})
        assert isinstance(result, Ambiguous)
        self.assertEqual(result.reason, "readback_failed")
        self.assertIsNotNone(result.page_id)
        self.assertEqual(fake.page_count(), 1)

    def test_a_readback_mismatch_is_ambiguous_and_names_the_field(self) -> None:
        fake = live_shows()
        client = Altered("fake-token", transport=fake.transport)
        with client:
            result = create_row(client, ShowsRow, SOURCE, {"name": "A", "notes": "mine"})
        assert isinstance(result, Ambiguous)
        self.assertEqual((result.reason, result.mismatched), ("mismatch", ("notes",)))
        self.assertIn("notes", result.detail)

    def test_a_write_is_never_retried(self) -> None:
        fake = live_shows()
        fake.time_out_next(times=2)
        with fake.client() as client:
            create_row(client, ShowsRow, SOURCE, {"name": "A"})
        self.assertEqual(len(fake.requests), 1)


if __name__ == "__main__":
    unittest.main()
