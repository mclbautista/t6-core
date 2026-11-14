"""Contacts reads and the two onboarding writes against the fake Notion."""

import unittest

from t6.domain import Contact
from t6.notion.contacts import Contacts
from t6.notion.generated import ContactsRow
from t6.notion.testing import FakeNotion, title
from t6.notion.writes import Ambiguous, Rejected

SOURCE = "ds-contacts"


def fake() -> FakeNotion:
    notion = FakeNotion()
    notion.add_snapshot_data_source(SOURCE, "contacts")
    return notion


class ContactsTest(unittest.TestCase):
    def test_set_telegram_user_id_returns_the_saved_contact(self) -> None:
        notion = fake()
        page = notion.add_page(SOURCE, {ContactsRow.notion_name("name"): title("Ana Cruz")})
        with notion.client() as client:
            saved = Contacts(client, SOURCE).set_telegram_user_id(str(page["id"]), 777)
        self.assertIsInstance(saved, Contact)
        assert isinstance(saved, Contact)
        self.assertEqual((saved.name, saved.telegram_user_id), ("Ana Cruz", 777))

    def test_set_telegram_user_id_survives_a_property_rename(self) -> None:
        notion = fake()
        page = notion.add_page(SOURCE, {ContactsRow.notion_name("name"): title("Ana Cruz")})
        notion.rename_property(SOURCE, "TelegramUserID", "Telegram ID")
        with notion.client() as client:
            saved = Contacts(client, SOURCE).set_telegram_user_id(str(page["id"]), 5)
        assert isinstance(saved, Contact)
        self.assertEqual(saved.telegram_user_id, 5)

    def test_a_refused_write_is_rejected(self) -> None:
        notion = fake()
        page = notion.add_page(SOURCE, {ContactsRow.notion_name("name"): title("Ana")})
        notion.fail_next(400, code="validation_error")
        with notion.client() as client:
            result = Contacts(client, SOURCE).set_telegram_user_id(str(page["id"]), 5)
        self.assertIsInstance(result, Rejected)

    def test_a_timeout_is_ambiguous(self) -> None:
        notion = fake()
        page = notion.add_page(SOURCE, {ContactsRow.notion_name("name"): title("Ana")})
        notion.time_out_next()
        with notion.client() as client:
            result = Contacts(client, SOURCE).set_telegram_user_id(str(page["id"]), 5)
        self.assertIsInstance(result, Ambiguous)

    def test_create_saves_the_fields_and_the_note(self) -> None:
        notion = fake()
        with notion.client() as client:
            contacts = Contacts(client, SOURCE)
            saved = contacts.create("Juan", telegram_user_id=42, telegram_handle="juandc", note="Created by Sixto.")
            assert isinstance(saved, Contact)
            self.assertEqual((saved.name, saved.telegram_user_id, saved.telegram_handle), ("Juan", 42, "juandc"))
            self.assertEqual(len(contacts.list()), 1)
            blocks = client.list_block_children(saved.page_id)
        self.assertEqual(blocks[0]["paragraph"]["rich_text"][0]["text"]["content"], "Created by Sixto.")

    def test_create_without_a_handle_or_note(self) -> None:
        notion = fake()
        with notion.client() as client:
            saved = Contacts(client, SOURCE).create("Juan", telegram_user_id=42)
        assert isinstance(saved, Contact)
        self.assertIsNone(saved.telegram_handle)


if __name__ == "__main__":
    unittest.main()
