"""Contacts in studio terms: `t6.domain.Contact` in and out, no Notion property names.

`Contacts` reads Contacts and makes the two writes onboarding needs: set a
Contact's Telegram user ID, and create a Contact. Writes save through
`t6.notion.writes`: each returns the saved `Contact`, `Rejected`, or
`Ambiguous`, and never retries. The caller keeps its own idempotency key and
decides about a retry. Credentials are withheld in text (see `t6.notion.client`).
"""

from __future__ import annotations

from t6.domain import Contact
from t6.notion.errors import NotionError
from t6.notion.generated import ContactsRow
from t6.notion.reads import ContactReader, contact_from_row
from t6.notion.sync import NotionClient
from t6.notion.writes import Ambiguous, Rejected, create_row, update_row

__all__ = ["Contacts"]


class Contacts:
    """Reads Contacts from one data source, sets Telegram user IDs and creates Contacts."""

    def __init__(self, client: NotionClient, data_source_id: str) -> None:
        self._client = client
        self._source = data_source_id
        self._reader = ContactReader(client, data_source_id)

    def get(self, page_id: str) -> Contact:
        """The Contact on a page, read fresh."""
        return self._reader.get(page_id)

    def list(self) -> tuple[Contact, ...]:
        """Every Contact."""
        return self._reader.list()

    def set_telegram_user_id(self, page_id: str, user_id: int) -> Contact | Rejected | Ambiguous:
        """Set the Contact's Telegram user ID and return the Contact as saved."""
        return update_row(
            self._client, ContactsRow, page_id, {"telegram_user_id": str(user_id)}, to_domain=contact_from_row
        )

    def create(
        self, name: str, *, telegram_user_id: int, telegram_handle: str = "", note: str = ""
    ) -> Contact | Rejected | Ambiguous:
        """Create a Contact, with `note` as one line in the page body, and return it as saved.

        A note that could not be saved is `Ambiguous`: the page exists, so check before repeating the create.
        """
        values: dict[str, object] = {"name": name, "telegram_user_id": str(telegram_user_id)}
        if telegram_handle:
            values["telegram_handle"] = telegram_handle
        saved = create_row(self._client, ContactsRow, self._source, values, to_domain=contact_from_row)
        if isinstance(saved, (Rejected, Ambiguous)) or not note:
            return saved
        paragraph = {"type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": note}}]}}
        try:
            self._client.append_block_children(saved.page_id, [paragraph])
        except NotionError as error:
            reason = error.code if error.code in ("timeout", "network_error") else "readback_failed"
            return Ambiguous(reason, f"the Contact was created but its note was not saved: {error}", saved.page_id)  # type: ignore[arg-type]
        return saved
