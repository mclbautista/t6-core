"""`t6 jo get|create|format` and `t6 deadline`, against the fake Notion."""

import io
import json
import os
import tempfile
import unittest
import warnings
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from cli_api_fake import client_patch
from t6.cli import main
from t6.notion.generated import ContactsRow, JobOrdersRow
from t6.registry import Registered, SqliteProjectRegistry
from t6.notion.testing import FakeNotion, rich_text, select, title

JOB_ORDERS, CONTACTS = "ds-jo", "ds-contacts"
FIELDS: dict[str, object] = {
    "name": "ORC - TOC - JO #62",
    "number": "62",
    "department": "TO",
    "date": "2026-11-14",
    "deadline": "2026-11-14T17:00:00",
    "title": "Orchid FTR",
    "task": "Conform",
    "telegram_key": "-100123:456",
    "cc_ids": ["cc-1"],
}


class JoCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        from unittest.mock import MagicMock, patch
        people = MagicMock()
        people.authorize.return_value = None
        people.registry.bound_entity.return_value = None
        patcher = patch('t6.verbs._people', return_value=people)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.fake = FakeNotion()
        patcher = client_patch(lambda _token: self.fake.client())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.fake.add_snapshot_data_source(JOB_ORDERS, "job_orders")
        self.fake.add_snapshot_data_source(CONTACTS, "contacts")
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.registry = SqliteProjectRegistry(self.root / 'registry.sqlite')
        people.registry = self.registry
        project = self.registry.register(actor='setup', idempotency_key='setup', notion_page_id='project-page')
        assert isinstance(project, Registered)

    def write(self, name: str, text: str) -> str:
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return str(path)

    def run_command(self, argv: list[str], *, environment: dict[str, str] | None = None, sources: bool = True) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        env = {"T6_NOTION_TOKEN": "secret", "T6_PREVIEW_SIGNING_KEY": "secret", "T6_REGISTRY_DB": str(self.root / "registry.sqlite"), "T6_PERSON_ID": "test-actor", **(environment or {})}
        extra = ["--data-sources", self.write("sources.json", json.dumps({"job_orders": JOB_ORDERS}))] if sources else []
        split = argv.index("--") if "--" in argv else len(argv)
        command = [*argv[:split], *extra, *argv[split:]]
        with warnings.catch_warnings(), mock.patch.dict(os.environ, env, clear=True), redirect_stdout(out), redirect_stderr(err):
            warnings.simplefilter("ignore", ResourceWarning)
            status = main(command, open_notion=lambda _token: self.fake.client())
        return status, out.getvalue(), err.getvalue()

    def create(self, fields: dict[str, object], *extra: str, environment: dict[str, str] | None = None) -> tuple[int, str, str]:
        return self.run_command(["jo", "raise", "--file", self.write("jo.json", json.dumps({"project_id": "project-page", **fields})), *extra], environment=environment)

    def test_raise_previews_normalized_fields_without_writing(self) -> None:
        status, out, err = self.create(FIELDS, environment={"T6_STANDING_CC": "standing"})
        result = json.loads(out)
        self.assertEqual((status, err, result["outcome"]), (0, "", "preview"))
        self.assertEqual(result["plan"]["fields"]["department"], "TOC")
        self.assertEqual(result["plan"]["fields"]["cc_ids"], ["cc-1", "standing"])
        self.assertEqual(result["plan"]["fields"]["watermark"], "none")
        self.assertEqual(self.fake.page_count(), 0)

    def test_confirm_queues_a_job_order_without_writing_notion(self) -> None:
        _, out, _ = self.create(FIELDS, "--idempotency-key", "one")
        preview = json.loads(out)
        status, out, _ = self.create(FIELDS, "--idempotency-key", "one", "--confirm", preview["token"])
        result = json.loads(out)
        self.assertEqual((status, result["outcome"]), (0, "done"))
        self.assertTrue(result["result"]["jo_id"].startswith("t6j_"))
        self.assertEqual(self.fake.page_count(), 0)

    def test_preview_validates_fields_and_date_only_deadline(self) -> None:
        for fields in ({**FIELDS, "department": "NOPE"}, {**FIELDS, "bogus": 1}):
            status, out, _ = self.create(fields)
            self.assertEqual((status, json.loads(out)["outcome"]), (2, "refused"))
        _, out, _ = self.create({**FIELDS, "deadline": "2026-11-14"})
        self.assertEqual(json.loads(out)["plan"]["fields"]["deadline"], "2026-11-14T23:59:00+08:00")

    def test_get_by_page_id_and_by_telegram_key(self) -> None:
        page = self.fake.add_page(JOB_ORDERS, {
            JobOrdersRow.notion_name('name'): title('ORC - TOC - JO #62'),
            JobOrdersRow.notion_name('title'): rich_text('Orchid FTR'),
            JobOrdersRow.notion_name('telegram_key'): rich_text('-100123:456'),
        })['id']
        for argv in (["jo", "get", page], ["jo", "get", "--", "-100123:456"]):
            with self.subTest(argv=argv):
                status, out, _ = self.run_command(argv)
                self.assertEqual((status, json.loads(out)["id"], json.loads(out)["title"]), (0, page, "Orchid FTR"))

    def test_get_an_unknown_telegram_key_is_an_error(self) -> None:
        status, _, err = self.run_command(["jo", "get", "--", "-1:2"])
        self.assertEqual(status, 2)
        self.assertIn("Telegram Key", json.loads(err)["error"])

    def test_format_needs_no_notion_and_lists_findings(self) -> None:
        draft = self.write("notes.txt", "ORC - TO - JO 62\nTitle: Orchid\nTask: t\nPurpose: p\nAssigned: a\n")
        status, out, err = self.run_command(["jo", "format", "--file", draft, "--date", "2026-06-22"], environment={"T6_NOTION_TOKEN": ""}, sources=False)
        result = json.loads(out)
        self.assertEqual((status, err), (0, ""))
        self.assertTrue(result["text"].startswith("ORC - TOC - JO #62\nJune 22 / Monday\n"))
        self.assertEqual([f["code"] for f in result["findings"]], ["no_deliverable_code"])

    def test_deadline_without_assignees_needs_no_notion(self) -> None:
        argv = ["deadline", "Nov 13 5pm", "--posted", "2026-11-12T09:00:00+08:00"]
        status, out, _ = self.run_command(argv, environment={"T6_NOTION_TOKEN": ""}, sources=False)
        self.assertEqual(status, 0)
        self.assertEqual(
            json.loads(out),
            {"deadline": "2026-11-13T17:00:00+08:00", "display": "Nov 13 / Fri (17:00)", "source": "stated", "findings": []},
        )

    def test_deadline_end_of_shift_uses_the_assignees_from_notion(self) -> None:
        c = ContactsRow
        page = self.fake.add_page(CONTACTS, {c.notion_name("name"): title("Ana"), c.notion_name("end_of_shift"): select("10:00 PM")})
        argv = ["deadline", "end of shift", "--assignees", str(page["id"]), "--posted", "2026-11-12T09:00:00+08:00"]
        status, out, _ = self.run_command(argv, sources=False)
        self.assertEqual((status, json.loads(out)["deadline"], json.loads(out)["source"]), (0, "2026-11-12T22:00:00+08:00", "shift"))

    def test_an_unreadable_deadline_is_a_finding_not_a_guess(self) -> None:
        _, out, _ = self.run_command(["deadline", "tomorrow"], sources=False)
        result = json.loads(out)
        self.assertEqual((result["deadline"], [f["code"] for f in result["findings"]]), (None, ["deadline_unparsed"]))

    def test_errors_without_a_token_or_source(self) -> None:
        self.assertEqual(self.run_command(["jo", "get", "x"], environment={"T6_NOTION_TOKEN": ""})[0], 2)
        out, err = io.StringIO(), io.StringIO()
        path = self.write("sources.json", "{}")
        with mock.patch.dict(os.environ, {"T6_NOTION_TOKEN": "s"}, clear=True), redirect_stdout(out), redirect_stderr(err):
            status = main(["jo", "get", "x", "--data-sources", path], open_notion=lambda _t: self.fake.client())
        self.assertEqual((status, out.getvalue()), (2, ""))
        self.assertIn("job_orders", json.loads(err.getvalue())["error"])


if __name__ == "__main__":
    unittest.main()
