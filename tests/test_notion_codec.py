"""Exercise t6.notion.codec on Notion property shapes."""

from datetime import date, datetime, timezone
import unittest

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


if __name__ == "__main__":
    unittest.main()
