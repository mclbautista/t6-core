"""Meeting Notes in domain terms, and the last-edited time and trash rules of the readers."""

import datetime as dt
import unittest

from t6.domain import MeetingNote
from t6.notion.meeting_notes import MeetingNotes, note_from_page
from t6.notion.reads import ContactReader, ProjectReader, page_edited
from t6.notion.testing import FakeNotion, checkbox, title

NOTES = "ds-meeting-notes"
PROJECTS = "ds-projects"
CONTACTS = "ds-contacts"
EDITED = "2026-11-10T04:30:00.000Z"


def _date(start: str) -> dict[str, object]:
    return {"type": "date", "date": {"start": start}}


class MeetingNotesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeNotion()
        self.fake.add_snapshot_data_source(NOTES, "meeting_notes")
        self.client = self.fake.client()
        self.addCleanup(self.client.close)
        self.notes = MeetingNotes(self.client, NOTES)

    def make(self, title: str, day: dt.date) -> MeetingNote:
        saved = self.notes.create(title=title, meeting_date=day)
        assert isinstance(saved, MeetingNote)
        return saved

    def test_create_saves_the_properties_and_a_body_in_chunks(self) -> None:
        blocks = [
            {"type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": f"line {i}"}}]}}
            for i in range(250)
        ]
        saved = self.notes.create(
            title="PSR MOM 2026-11-16",
            meeting_date=dt.date(2026, 11, 16),
            project_ids=["11111111-1111-1111-1111-111111111111"],
            blocks=blocks,
        )
        assert isinstance(saved, MeetingNote)
        self.assertEqual(saved.name, "PSR MOM 2026-11-16")
        self.assertEqual(saved.meeting_date, dt.date(2026, 11, 16))
        self.assertEqual(saved.project_ids, ("11111111-1111-1111-1111-111111111111",))
        self.assertEqual(len(self.client.list_block_children(saved.page_id)), 250)

    def test_with_title_finds_only_an_exact_title(self) -> None:
        self.make("PSR MOM 2026-11-16", dt.date(2026, 11, 16))
        self.make("PSR MOM 2026-11-09", dt.date(2026, 11, 9))
        self.assertEqual([n.name for n in self.notes.with_title("PSR MOM 2026-11-16")], ["PSR MOM 2026-11-16"])
        self.assertEqual(len(self.notes.with_title_prefix("PSR MOM")), 2)

    def test_trash_hides_a_note_from_the_finders(self) -> None:
        saved = self.make("PSR MOM 2026-11-16", dt.date(2026, 11, 16))
        self.notes.trash(saved.page_id)
        self.assertEqual(self.notes.with_title_prefix("PSR MOM"), ())

    def test_ready_to_send_skips_sent_and_unready_notes(self) -> None:
        self.make("PSR MOM 2026-11-16", dt.date(2026, 11, 16))
        self.make("PSR MOM 2026-11-09", dt.date(2026, 11, 9))
        self.fake.add_page(
            NOTES,
            {
                "Name": title("Other 2026-11-16"),
                "Ready to send": checkbox(True),
                "Sent at": _date("2026-11-16T10:00:00+08:00"),
            },
        )
        self.fake.add_page(NOTES, {"Name": title("PSR MOM 2026-11-02"), "Ready to send": checkbox(True)})
        self.assertEqual([n.name for n in self.notes.ready_to_send("PSR MOM")], ["PSR MOM 2026-11-02"])
        self.assertEqual(len(self.notes.ready_to_send()), 1)

    def test_mark_sent_sets_sent_at(self) -> None:
        saved = self.make("PSR MOM 2026-11-16", dt.date(2026, 11, 16))
        when = dt.datetime(2026, 11, 16, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
        sent = self.notes.mark_sent(saved.page_id, when)
        assert isinstance(sent, MeetingNote)
        self.assertEqual(sent.sent_at, when)

    def test_a_note_carries_attendee_names_and_last_edited(self) -> None:
        page = self.fake.add_page(
            NOTES,
            {
                "Name": title("PSR MOM 2026-11-16"),
                "Team Attendees": {
                    "type": "people",
                    "people": [{"object": "user", "id": "u1", "name": "A Person"}, {"object": "user", "id": "u2"}],
                },
            },
        )
        page["last_edited_time"] = EDITED
        note = note_from_page(page)
        self.assertEqual(note.attendee_ids, ("u1", "u2"))
        self.assertEqual(note.attendee_names, ("A Person", ""))
        self.assertEqual(note.last_edited, dt.datetime(2026, 11, 10, 4, 30, tzinfo=dt.timezone.utc))


class ReaderTests(unittest.TestCase):
    def test_page_edited_reads_the_page_timestamp(self) -> None:
        self.assertEqual(page_edited({"last_edited_time": EDITED}), dt.datetime(2026, 11, 10, 4, 30, tzinfo=dt.timezone.utc))
        self.assertIsNone(page_edited({}))

    def test_lists_skip_trashed_and_archived_pages_and_carry_last_edited(self) -> None:
        fake = FakeNotion()
        fake.add_snapshot_data_source(PROJECTS, "projects")
        fake.add_snapshot_data_source(CONTACTS, "contacts")
        for source in (PROJECTS, CONTACTS):
            fake.add_page(source, {"Name": title("Kept")}, page_id=f"{source}-kept")
            fake.add_page(source, {"Name": title("Trashed")}, page_id=f"{source}-trashed")
            fake.add_page(source, {"Name": title("Archived")}, page_id=f"{source}-archived")
        for source in (PROJECTS, CONTACTS):
            fake._pages[f"{source}-trashed"]["in_trash"] = True
            fake._pages[f"{source}-archived"]["archived"] = True
            fake._pages[f"{source}-kept"]["last_edited_time"] = EDITED
        with fake.client() as client:
            projects = ProjectReader(client, PROJECTS).list()
            contacts = ContactReader(client, CONTACTS).list()
        self.assertEqual([p.name for p in projects], ["Kept"])
        self.assertEqual([c.name for c in contacts], ["Kept"])
        self.assertEqual(projects[0].last_edited, dt.datetime(2026, 11, 10, 4, 30, tzinfo=dt.timezone.utc))
        self.assertEqual(contacts[0].last_edited, projects[0].last_edited)


if __name__ == "__main__":
    unittest.main()
