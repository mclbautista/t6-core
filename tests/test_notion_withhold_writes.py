"""Every text t6-core writes to Notion has credentials withheld; names and URLs are left alone."""

import json
from typing import Any
import unittest

from t6.notion.client import withheld
from t6.notion.testing import FakeNotion
from t6.notion.writes import create_row, update_row
from t6.privacy import WITHHELD
from test_notion_writes import SOURCE, ShowsRow, live_shows

URL = "https://example.com/folder?pw=abc&pin=1"


def paragraph(text: str) -> dict[str, Any]:
    return {"type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": text}}]}}


class WithholdOnWriteTests(unittest.TestCase):
    def test_text_properties_are_written_withheld_and_still_read_back_clean(self) -> None:
        fake = live_shows()
        text = f"Source drive\npassword: x pin=1\nLink {URL}"
        with fake.client() as client:
            row = create_row(client, ShowsRow, SOURCE, {"name": "Show A, Ate Maria", "notes": text})
        assert isinstance(row, ShowsRow)
        self.assertEqual(row.name, "Show A, Ate Maria")
        self.assertIn(WITHHELD, row.notes or "")
        self.assertNotIn("password: x", row.notes or "")
        self.assertNotIn("pin=1 ", (row.notes or "").replace(URL, ""))
        self.assertIn(URL, row.notes or "")
        stored = fake.page(row.page_id)["properties"]["Notes"]["rich_text"][0]["text"]["content"]
        self.assertEqual(stored, row.notes)

    def test_an_update_withholds_too(self) -> None:
        fake = live_shows()
        with fake.client() as client:
            created = create_row(client, ShowsRow, SOURCE, {"name": "A"})
            assert isinstance(created, ShowsRow)
            row = update_row(client, ShowsRow, created.page_id, {"notes": "pw=hunter2"})
        assert isinstance(row, ShowsRow)
        self.assertNotIn("hunter2", row.notes or "")
        self.assertIn(WITHHELD, row.notes or "")

    def test_a_raw_page_body_with_blocks_is_withheld(self) -> None:
        fake = live_shows()
        with fake.client() as client:
            page = client.create_page(
                {
                    "parent": {"data_source_id": SOURCE},
                    "properties": {"Name": {"title": [{"text": {"content": "passcode: 1234"}}]}},
                    "children": [paragraph("pin=9")],
                }
            )
        sent = json.loads(fake.requests[0].content)  # the fake does not store `children`, so check the request
        self.assertNotIn("1234", str(fake.page(page["id"])))
        self.assertNotIn("1234", str(sent))
        self.assertNotIn("pin=9", str(sent))
        self.assertEqual(str(sent).count(WITHHELD), 2)

    def test_appended_blocks_are_withheld_including_nested_ones(self) -> None:
        fake = live_shows()
        page = str(fake.add_page(SOURCE, {})["id"])
        nested = {
            "type": "toggle",
            "toggle": {
                "rich_text": [{"type": "text", "text": {"content": "Details"}}],
                "children": [paragraph("password: secret1")],
            },
        }
        with fake.client() as client:
            client.append_block_children(page, [paragraph("pin=7"), nested])
        text = str(fake.block_children(page))
        self.assertNotIn("pin=7", text)
        self.assertEqual(text.count(WITHHELD), 1)
        self.assertNotIn("secret1", str(fake._children))
        self.assertIn("Details", text)

    def test_text_without_credentials_and_non_text_values_are_unchanged(self) -> None:
        body = {
            "properties": {
                "Name": {"title": [{"text": {"content": "Kuya Jun & Ate Ana"}}]},
                "Stage": {"select": {"name": "pin=1"}},
            }
        }
        self.assertEqual(withheld(body), body)


if __name__ == "__main__":
    unittest.main()
