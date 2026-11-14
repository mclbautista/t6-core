"""Meeting Notes in studio terms: `t6.domain.MeetingNote` in and out, no Notion property names.

`MeetingNotes` reads, creates, trashes and marks sent the pages of one Meeting
Notes data source. Fields are addressed by property ID through
`t6.notion.generated.MeetingNotesRow`. A page body is a list of Notion blocks:
write it with `create` (it appends in chunks) and read it with
`NotionClient.list_block_children`.

`list` and the finders skip pages in the trash or archived. `create` saves
through `t6.notion.writes`, so it returns the saved `MeetingNote`, `Rejected`
or `Ambiguous`; a body that could not be appended is `Ambiguous` too, and the
page exists. Credentials in text are withheld (see `t6.notion.client`).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Any

from t6.domain import MeetingNote
from t6.notion.errors import NotionError
from t6.notion.generated import MeetingNotesRow
from t6.notion.reads import is_live, page_edited
from t6.notion.sync import NotionClient
from t6.notion.writes import Ambiguous, Rejected, create_row, update_row

JSON = dict[str, Any]

BLOCKS_PER_REQUEST = 100
"""Notion accepts at most this many child blocks in one request."""


def _key(attribute: str) -> str:
    return MeetingNotesRow.notion_id(attribute) or MeetingNotesRow.notion_name(attribute)


def note_from_page(page: JSON) -> MeetingNote:
    """A `MeetingNote` from a Meeting Notes page."""
    row = MeetingNotesRow.from_page(page)
    people = (MeetingNotesRow.raw_property(page, "team_attendees") or {}).get("people") or []
    names = {str(person["id"]): str(person.get("name") or "") for person in people if person.get("id")}
    return MeetingNote(
        page_id=row.page_id,
        name=row.name or "",
        meeting_date=row.meeting_date,
        project_ids=row.project,
        attendee_ids=row.team_attendees,
        attendee_names=tuple(names.get(user, "") for user in row.team_attendees),
        ready_to_send=bool(row.ready_to_send),
        sent_at=row.sent_at,
        last_edited=page_edited(page),
    )


class MeetingNotes:
    """Reads and writes Meeting Notes pages in one data source."""

    def __init__(self, client: NotionClient, data_source_id: str) -> None:
        self._client = client
        self._source = data_source_id

    def with_title(self, title: str) -> tuple[MeetingNote, ...]:
        """The notes whose title is exactly `title`."""
        found = self._query({"property": _key("name"), "title": {"equals": title}})
        return tuple(note for note in found if note.name == title)

    def with_title_prefix(self, prefix: str) -> tuple[MeetingNote, ...]:
        """The notes whose title starts with `prefix`."""
        found = self._query({"property": _key("name"), "title": {"starts_with": prefix}})
        return tuple(note for note in found if note.name.startswith(prefix))

    def ready_to_send(self, title_prefix: str = "") -> tuple[MeetingNote, ...]:
        """The notes marked Ready to send and not yet sent, with a title starting with `title_prefix`."""
        found = self._query(
            {
                "and": [
                    {"property": _key("ready_to_send"), "checkbox": {"equals": True}},
                    {"property": _key("sent_at"), "date": {"is_empty": True}},
                ]
            }
        )
        return tuple(
            note
            for note in found
            if note.ready_to_send and note.sent_at is None and note.name.startswith(title_prefix)
        )

    def create(
        self,
        *,
        title: str,
        meeting_date: dt.date,
        project_ids: Sequence[str] = (),
        attendee_ids: Sequence[str] = (),
        blocks: Sequence[JSON] = (),
    ) -> MeetingNote | Rejected | Ambiguous:
        """Create a note with a body of Notion `blocks` and return it as saved."""
        saved = create_row(
            self._client,
            MeetingNotesRow,
            self._source,
            {
                "name": title,
                "meeting_date": meeting_date,
                "project": tuple(project_ids),
                "team_attendees": tuple(attendee_ids),
            },
        )
        if isinstance(saved, (Rejected, Ambiguous)):
            return saved
        for start in range(0, len(blocks), BLOCKS_PER_REQUEST):
            try:
                self._client.append_block_children(saved.page_id, list(blocks[start : start + BLOCKS_PER_REQUEST]))
            except NotionError as error:
                reason = error.code if error.code in ("timeout", "network_error") else "readback_failed"
                return Ambiguous(reason, f"the page was created but its body was not saved: {error}", saved.page_id)  # type: ignore[arg-type]
        return note_from_page(self._client.retrieve_page(saved.page_id))

    def trash(self, page_id: str) -> None:
        """Move the note to the trash. Raises `NotionError` if Notion refuses."""
        self._client.update_page(page_id, {"in_trash": True})

    def mark_sent(self, page_id: str, when: dt.datetime) -> MeetingNote | Rejected | Ambiguous:
        """Set Sent at to `when` and return the note as saved."""
        saved = update_row(self._client, MeetingNotesRow, page_id, {"sent_at": when})
        if isinstance(saved, (Rejected, Ambiguous)):
            return saved
        return note_from_page(self._client.retrieve_page(page_id))

    def _query(self, filter: JSON) -> tuple[MeetingNote, ...]:
        pages = self._client.query_data_source(self._source, filter=filter)
        return tuple(note_from_page(page) for page in pages if is_live(page))
