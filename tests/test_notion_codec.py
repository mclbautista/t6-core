"""Exercise t6.notion.codec on Notion property shapes."""

from datetime import date, datetime, timedelta, timezone
import unittest
from zoneinfo import ZoneInfo

from t6.notion import codec


class CodecTests(unittest.TestCase):
    def test_text_properties_join_their_parts(self) -> None:
        prop = {"type": "rich_text", "rich_text": [{"plain_text": "Grade "}, {"plain_text": "notes"}]}
        self.assertEqual(codec.rich_text(prop), "Grade notes")
        self.assertIsNone(codec.rich_text({"type": "rich_text", "rich_text": []}))

    def test_a_wrong_type_or_missing_property_decodes_to_none(self) -> None:
        self.assertIsNone(codec.select({"type": "multi_select", "multi_select": []}))
        self.assertIsNone(codec.title(None))
        self.assertEqual(codec.relation(None), ())

    def test_options_and_relations(self) -> None:
        self.assertEqual(codec.status({"type": "status", "status": {"name": "Done"}}), "Done")
        tags = {"type": "multi_select", "multi_select": [{"name": "ARRI"}, {"name": "RED"}]}
        self.assertEqual(codec.multi_select(tags), ("ARRI", "RED"))
        rel = {"type": "relation", "relation": [{"id": "a"}, {"id": "b"}]}
        self.assertEqual(codec.relation(rel), ("a", "b"))

    def test_integer_rejects_fractions_and_booleans(self) -> None:
        self.assertEqual(codec.integer({"type": "number", "number": 6.0}), 6)
        self.assertIsNone(codec.integer({"type": "number", "number": 6.5}))
        self.assertIsNone(codec.number({"type": "number", "number": True}))

    def test_dates_with_and_without_a_time(self) -> None:
        day = {"type": "date", "date": {"start": "2026-11-16"}}
        self.assertEqual(codec.date_start(day), date(2026, 11, 16))
        moment = {"type": "date", "date": {"start": "2026-11-16T09:30:00.000+00:00"}}
        self.assertEqual(codec.date_start(moment), datetime(2026, 11, 16, 9, 30, tzinfo=timezone.utc))
        self.assertIsNone(codec.date_start({"type": "date", "date": None}))

    def test_date_range_returns_start_and_end(self) -> None:
        span = {"type": "date", "date": {"start": "2026-11-16", "end": "2026-11-20"}}
        self.assertEqual(codec.date_range(span), (date(2026, 11, 16), date(2026, 11, 20)))
        self.assertEqual(codec.date_start(span), date(2026, 11, 16))

    def test_date_range_without_an_end_gives_none(self) -> None:
        self.assertEqual(codec.date_range({"type": "date", "date": {"start": "2026-11-16", "end": None}}), (date(2026, 11, 16), None))
        self.assertEqual(codec.date_range({"type": "date", "date": None}), (None, None))
        self.assertEqual(codec.date_range(None), (None, None))

    def test_date_range_end_keeps_its_time(self) -> None:
        span = {"type": "date", "date": {"start": "2026-11-16T09:00:00.000+08:00", "end": "2026-11-16T17:30:00.000+08:00"}}
        _, end = codec.date_range(span)
        self.assertEqual(end, datetime.fromisoformat("2026-11-16T17:30:00+08:00"))

    def test_a_time_with_no_offset_takes_the_time_zone_of_the_value(self) -> None:
        value = {"start": "2026-11-16T09:30:00.000", "end": "2026-11-16T10:30:00.000", "time_zone": "America/New_York"}
        moment = codec.date_start({"type": "date", "date": value})
        self.assertEqual(moment, datetime(2026, 11, 16, 9, 30, tzinfo=ZoneInfo("America/New_York")))
        self.assertEqual(moment.utcoffset(), timedelta(hours=-5))  # type: ignore[union-attr]
        _, end = codec.date_range({"type": "date", "date": value})
        self.assertEqual(end, datetime(2026, 11, 16, 10, 30, tzinfo=ZoneInfo("America/New_York")))

    def test_an_offset_wins_over_the_time_zone(self) -> None:
        value = {"start": "2026-11-16T09:30:00.000+08:00", "end": None, "time_zone": "America/New_York"}
        moment = codec.date_start({"type": "date", "date": value})
        self.assertEqual(moment.utcoffset(), timedelta(hours=8))  # type: ignore[union-attr]

    def test_a_time_with_no_offset_and_no_zone_stays_naive(self) -> None:
        for value in ({"start": "2026-11-16T09:30:00.000", "end": None}, {"start": "2026-11-16T09:30:00.000", "end": None, "time_zone": None}):
            self.assertEqual(codec.date_start({"type": "date", "date": value}), datetime(2026, 11, 16, 9, 30))

    def test_a_date_with_a_time_zone_stays_a_date(self) -> None:
        self.assertEqual(codec.date_start({"type": "date", "date": {"start": "2026-11-16", "end": None, "time_zone": "America/New_York"}}), date(2026, 11, 16))


if __name__ == "__main__":
    unittest.main()
