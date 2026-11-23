"""Meeting Notes write verbs and the cheap read of the latest note, through the HTTP client and fake Notion."""
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

from t6.client import Client
from t6.notion.generated import MeetingNotesRow
from t6.notion.sync import NotionClient
from t6.notion.testing import FakeNotion, date, title
from t6.person import People
from t6.preview import PreviewTokens
from t6.privacy import Person, Registry
from t6.registry import Registered
from t6.registry.sqlite import SqliteProjectRegistry
from t6.server.http import make_server
from t6.server.queue import JobQueue
from t6.server.verbs import VerbAPI
from t6.verbs import (Ambiguous, Done, MeetingNotesAppendInput, MeetingNotesCreateInput, MeetingNotesListInput,
                      MeetingNotesMarkSentInput, MeetingNotesTrashInput, NoteBlock, NoteSpan, Refused)

NOTES = "meeting_notes"
SENT = "2026-11-16T10:00:00+08:00"
MANILA = timezone(timedelta(hours=8))


def block(text: str, kind: str = "paragraph", **extra: Any) -> NoteBlock:
    return NoteBlock(kind=kind, text=text, spans=[NoteSpan(text=text)], children=[], **extra)


class MeetingNotesVerbTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.notion = FakeNotion()
        for source in ("projects", NOTES):
            self.notion.add_snapshot_data_source(source, source)
        self.project = self.notion.add_page("projects", {"Name": title("Example Show")})
        self.registry = SqliteProjectRegistry(self.tmp.name + "/registry")
        registered = self.registry.register(actor="setup", idempotency_key="show", notion_page_id=self.project["id"])
        assert isinstance(registered, Registered)
        self.t6_project = registered.project.id
        grants = {"reports": frozenset({"minutes"}), "gateway": frozenset()}
        api = VerbAPI(
            People(self.registry, PreviewTokens("key"), grants), JobQueue(self.tmp.name + "/queue"),
            {"reports-secret": "reports", "gateway-secret": "gateway"}, lambda _: self.notion.client(),
            privacy_registry=Registry(people=[Person(name="Client Corp")]),
            data_sources={"projects": "projects", NOTES: NOTES},
        )
        env = patch.dict(os.environ, {"T6_NOTION_TOKEN": "fake"})
        env.start()
        self.addCleanup(env.stop)
        self.server = make_server(("127.0.0.1", 0), api)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(self.server.shutdown)
        url = f"http://127.0.0.1:{self.server.server_port}"
        self.reports, self.gateway = Client(url, "reports-secret"), Client(url, "gateway-secret")

    def note(self, name: str, day: str | None, sent: str | None = None, edited: str | None = None) -> dict[str, Any]:
        properties: dict[str, Any] = {"Name": title(name), "Meeting Date": date(day)}
        if sent:
            properties["Sent at"] = date(sent)
        page = self.notion.add_page(NOTES, properties)
        if edited:
            self.notion._pages[page["id"]]["last_edited_time"] = edited
        return page

    def create(self, name: str = "Weekly minutes", **fields: Any) -> dict[str, Any]:
        fields = {"title": name, "meeting_date": "2026-11-16", "project_ids": [], "attendee_ids": [], "blocks": [], **fields}
        result = self.reports.meeting_notes_create(MeetingNotesCreateInput(**fields))
        self.assertIsInstance(result, Done, result)
        assert isinstance(result, Done)
        return dict(result.result["meeting_note"])

    def audit(self) -> list[tuple[str, str, str, Any]]:
        with self.registry._transaction() as db:
            return [tuple(row) for row in db.execute("SELECT action, project_id, actor, after FROM audit WHERE action LIKE 'meeting_note_%' ORDER BY id")]

    def counting(self, method: str) -> list[str]:
        calls: list[str] = []
        real = getattr(NotionClient, method)

        def counted(client: NotionClient, parent: str, *args: Any, **kwargs: Any) -> Any:
            calls.append(parent)
            return real(client, parent, *args, **kwargs)

        patcher = patch.object(NotionClient, method, counted)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def listed(self, **fields: Any) -> list[dict[str, Any]]:
        result = self.reports.meeting_notes_list(MeetingNotesListInput(**fields))
        assert isinstance(result, Done), result
        return list(result.result["meeting_notes"])

    def test_latest_keeps_the_newest_dated_note_and_fetches_one_body(self) -> None:
        older = self.note("Older", "2026-11-09")
        newest = self.note("Newest", "2026-11-16")
        self.note("Undated", None)
        with self.notion.client() as client:
            for page in (older, newest):
                client.append_block_children(page["id"], [{"type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": "Body"}}]}}])
        calls = self.counting("list_block_children")
        found = self.listed(latest=True)
        self.assertEqual([n["name"] for n in found], ["Newest"])
        self.assertEqual(found[0]["body"][0]["text"], "Body")
        self.assertEqual(calls, [newest["id"]])

    def test_latest_ties_go_to_the_later_last_edited_and_follow_the_other_filters(self) -> None:
        self.note("Early edit", "2026-11-16", edited="2026-11-16T01:00:00.000Z")
        self.note("Late edit", "2026-11-16", edited="2026-11-16T05:00:00.000Z")
        self.note("Tomorrow", "2026-11-17")
        self.assertEqual([n["name"] for n in self.listed(latest=True, before="2026-11-17")], ["Late edit"])
        self.assertEqual(self.listed(latest=True, title="Nothing"), [])

    def test_include_body_false_makes_no_block_call(self) -> None:
        self.note("Notes", "2026-11-16")
        calls = self.counting("list_block_children")
        found = self.listed(include_body=False)
        self.assertEqual((found[0]["body"], calls), ([], []))

    def test_create_writes_bound_project_pages_attendees_and_the_body(self) -> None:
        saved = self.create(project_ids=[self.t6_project], attendee_ids=["user-1"], blocks=[
            block("Agenda", "heading_2"),
            NoteBlock(kind="bulleted_list_item", text="Item", spans=[NoteSpan(text="Item", bold=True, url="https://example.com")], children=[block("Sub")]),
            block("Done", "to_do", checked=True),
        ])
        page = self.notion.page(saved["page_id"])
        related = [r["id"].replace("-", "") for r in page["properties"][MeetingNotesRow.notion_name("project")]["relation"]]
        self.assertEqual(related, [self.project["id"].replace("-", "")])
        self.assertEqual(saved["project_ids"], [self.t6_project])
        self.assertEqual(saved["attendee_ids"], ["user-1"])
        body = self.listed(title="Weekly minutes")[0]["body"]
        self.assertEqual([b["kind"] for b in body], ["heading_2", "bulleted_list_item", "to_do"])
        self.assertTrue(body[1]["spans"][0]["bold"])
        self.assertEqual(body[1]["spans"][0]["url"], "https://example.com")
        self.assertEqual(body[1]["children"][0]["text"], "Sub")
        self.assertIs(body[2]["checked"], True)

    def test_create_accepts_a_notion_page_id_and_refuses_an_unknown_t6_id_or_kind(self) -> None:
        saved = self.create(project_ids=[self.project["id"]])
        self.assertEqual(saved["project_ids"], [self.t6_project])
        unknown = self.reports.meeting_notes_create(MeetingNotesCreateInput(title="X", meeting_date="2026-11-16", project_ids=["t6p_" + "0" * 32], attendee_ids=[], blocks=[]))
        self.assertIsInstance(unknown, Refused)
        bad = self.reports.meeting_notes_create(MeetingNotesCreateInput(title="Y", meeting_date="2026-11-16", project_ids=[], attendee_ids=[], blocks=[block("x", "table")]))
        assert isinstance(bad, Refused)
        self.assertEqual(bad.rule, "input")
        self.assertEqual(self.listed(title="X") + self.listed(title="Y"), [])

    def test_create_with_a_failed_body_is_ambiguous_with_the_page_id(self) -> None:
        from t6.notion.errors import NotionError
        with patch.object(NotionClient, "append_block_children", side_effect=NotionError(400, "validation_error", "no")):
            result = self.reports.meeting_notes_create(MeetingNotesCreateInput(title="Half", meeting_date="2026-11-16", project_ids=[], attendee_ids=[], blocks=[block("x")]))
        assert isinstance(result, Ambiguous)
        self.assertEqual(result.candidates[0]["page_id"], self.listed(title="Half")[0]["page_id"])

    def test_a_body_over_100_blocks_is_appended_in_two_calls(self) -> None:
        saved = self.create()
        calls = self.counting("append_block_children")
        done = self.reports.meeting_notes_append(MeetingNotesAppendInput(note=saved["page_id"], blocks=[block(f"line {i}") for i in range(101)]))
        self.assertIsInstance(done, Done, done)
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(self.listed(title="Weekly minutes")[0]["body"]), 101)

    def test_append_and_trash_refuse_a_sent_note_and_append_refuses_a_trashed_one(self) -> None:
        sent = self.note("Sent", "2026-11-09", sent=SENT)
        for result in (self.reports.meeting_notes_append(MeetingNotesAppendInput(note=sent["id"], blocks=[block("x")])),
                       self.reports.meeting_notes_trash(MeetingNotesTrashInput(note=sent["id"]))):
            assert isinstance(result, Refused)
            self.assertEqual(result.rule, "sent")
        self.assertFalse(self.notion.page(sent["id"])["in_trash"])
        gone = self.create("Gone")
        self.assertIsInstance(self.reports.meeting_notes_trash(MeetingNotesTrashInput(note=gone["page_id"])), Done)
        result = self.reports.meeting_notes_append(MeetingNotesAppendInput(note=gone["page_id"], blocks=[block("x")]))
        assert isinstance(result, Refused)
        self.assertEqual(result.rule, "sent")

    def test_the_verbs_refuse_a_page_outside_meeting_notes(self) -> None:
        result = self.reports.meeting_notes_trash(MeetingNotesTrashInput(note=self.project["id"]))
        assert isinstance(result, Refused)
        self.assertEqual(result.rule, "input")
        self.assertFalse(self.notion.page(self.project["id"])["in_trash"])

    def test_write_replies_are_masked_like_the_list(self) -> None:
        page = self.note("Client Corp kickoff", "2026-11-16")
        [listed] = self.listed()
        for result in (self.reports.meeting_notes_append(MeetingNotesAppendInput(note=page["id"], blocks=[block("Agenda")])),
                       self.reports.meeting_notes_mark_sent(MeetingNotesMarkSentInput(note=page["id"])),
                       self.reports.meeting_notes_create(MeetingNotesCreateInput(title="Client Corp review", meeting_date="2026-11-17", project_ids=[], attendee_ids=[], blocks=[]))):
            assert isinstance(result, Done), result
            self.assertNotIn("Client Corp", json.dumps(result.result))
        self.assertNotIn("Client Corp", listed["name"])
        self.assertEqual(self.notion.page(page["id"])["properties"]["Name"]["title"][0]["plain_text"], "Client Corp kickoff")

    def test_mark_sent_twice_keeps_the_first_date(self) -> None:
        saved = self.create()
        first = self.reports.meeting_notes_mark_sent(MeetingNotesMarkSentInput(note=saved["page_id"], at=datetime(2026, 11, 16, 10, tzinfo=MANILA)))
        assert isinstance(first, Done)
        again = self.reports.meeting_notes_mark_sent(MeetingNotesMarkSentInput(note=saved["page_id"], at=datetime(2026, 11, 17, 9, tzinfo=MANILA)))
        assert isinstance(again, Done)
        self.assertTrue(first.result["meeting_note"]["sent_at"].startswith("2026-11-16T10:00:00"))
        self.assertEqual(again.result["meeting_note"]["sent_at"], first.result["meeting_note"]["sent_at"])
        self.assertEqual([row[0] for row in self.audit()].count("meeting_note_sent"), 1)

    def test_mark_sent_defaults_to_now(self) -> None:
        saved = self.create()
        done = self.reports.meeting_notes_mark_sent(MeetingNotesMarkSentInput(note=saved["page_id"]))
        assert isinstance(done, Done)
        self.assertTrue(done.result["meeting_note"]["sent_at"])

    def test_each_write_adds_an_audit_row_with_no_text(self) -> None:
        saved = self.create("Secret title", blocks=[block("Secret text")])
        page = saved["page_id"]
        self.reports.meeting_notes_append(MeetingNotesAppendInput(note=page, blocks=[block("More secret text")]))
        self.reports.meeting_notes_mark_sent(MeetingNotesMarkSentInput(note=page))
        other = self.create("Other")["page_id"]
        self.reports.meeting_notes_trash(MeetingNotesTrashInput(note=other))
        rows = self.audit()
        self.assertEqual([(a, s, who) for a, s, who, _ in rows], [
            ("meeting_note_create", page, "reports"), ("meeting_note_append", page, "reports"), ("meeting_note_sent", page, "reports"),
            ("meeting_note_create", other, "reports"), ("meeting_note_trash", other, "reports")])
        self.assertNotIn("ecret", json.dumps(rows))
        self.assertNotIn("Other", json.dumps(rows))

    def test_a_caller_without_minutes_is_refused(self) -> None:
        saved = self.create()
        for result in (
            self.gateway.meeting_notes_create(MeetingNotesCreateInput(title="X", meeting_date="2026-11-16", project_ids=[], attendee_ids=[], blocks=[])),
            self.gateway.meeting_notes_append(MeetingNotesAppendInput(note=saved["page_id"], blocks=[block("x")])),
            self.gateway.meeting_notes_trash(MeetingNotesTrashInput(note=saved["page_id"])),
            self.gateway.meeting_notes_mark_sent(MeetingNotesMarkSentInput(note=saved["page_id"])),
        ):
            assert isinstance(result, Refused)
            self.assertEqual(result.rule, "permission")
        self.assertEqual(len(self.audit()), 1)

    def test_append_idempotency_key_replays_conflicts_and_refuses_blank(self) -> None:
        saved = self.create()
        calls = self.counting("append_block_children")
        first = self.reports.meeting_notes_append(MeetingNotesAppendInput(note=saved["page_id"], blocks=[block("Once")], idempotency_key="append-1"))
        again = self.reports.meeting_notes_append(MeetingNotesAppendInput(note=saved["page_id"], blocks=[block("Once")], idempotency_key="append-1"))
        assert isinstance(first, Done) and isinstance(again, Done)
        self.assertEqual(again.result, first.result)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(self.listed(title="Weekly minutes")[0]["body"]), 1)
        self.assertEqual([row[0] for row in self.audit()].count("meeting_note_append"), 1)
        conflict = self.reports.meeting_notes_append(MeetingNotesAppendInput(note=saved["page_id"], blocks=[block("Different")], idempotency_key="append-1"))
        assert isinstance(conflict, Refused)
        self.assertEqual(conflict.rule, "idempotency_key")
        blank = self.reports.meeting_notes_append(MeetingNotesAppendInput(note=saved["page_id"], blocks=[block("Blank")], idempotency_key="   "))
        assert isinstance(blank, Refused)
        self.assertEqual(blank.rule, "idempotency_key")


if __name__ == "__main__":
    unittest.main()
