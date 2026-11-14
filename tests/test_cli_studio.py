"""`t6 departments`, `contacts find`, `projects resolve` and `standing-cc`, against the fake Notion."""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from t6.cli import main
from t6.notion.generated import ContactsRow, ProjectsRow
from t6.notion.testing import FakeNotion, rich_text, select, title

JOB_ORDERS, CONTACTS, PROJECTS = "ds-jo", "ds-contacts", "ds-projects"


class StudioCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeNotion()
        for source, database in ((JOB_ORDERS, "job_orders"), (CONTACTS, "contacts"), (PROJECTS, "projects")):
            self.fake.add_snapshot_data_source(source, database)
        self.ids = {name: self.fake.add_option(JOB_ORDERS, "Department", name) for name in ("VFX", "TOC", "MISC", "AE")}
        c, p = ContactsRow, ProjectsRow
        self.maria = self.fake.add_page(
            CONTACTS,
            {
                c.notion_name("name"): title("Maria Santos"),
                c.notion_name("telegram_handle"): rich_text("@msantos"),
                c.notion_name("aliases"): rich_text("Ria"),
                c.notion_name("end_of_shift"): select("10:00 PM"),
            },
        )
        self.fake.add_page(CONTACTS, {c.notion_name("name"): title("Ben Cruz")})
        self.alpha = self.fake.add_page(
            PROJECTS, {p.notion_name("name"): title("Alpha Show"), p.notion_name("project_code"): select("ALP")}
        )
        self.fake.add_page(
            PROJECTS, {p.notion_name("name"): title("Beta Show"), p.notion_name("project_code"): select("BET")}
        )

    def run_command(
        self, argv: list[str], *, environment: dict[str, str] | None = None, sources: dict[str, str] | None = None
    ) -> tuple[int, str, str]:
        env = {"T6_NOTION_TOKEN": "secret", **(environment or {})}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data-sources.json"
            path.write_text(
                json.dumps(sources or {"job_orders": JOB_ORDERS, "contacts": CONTACTS, "projects": PROJECTS}),
                encoding="utf-8",
            )
            out, err = io.StringIO(), io.StringIO()
            command = [*argv]
            if argv[0] != "standing-cc":
                command += ["--data-sources", str(path)]
            if argv[0] == "projects":
                command += ["--registry-db", str(Path(directory) / "none.sqlite")]
            with mock.patch.dict(os.environ, env, clear=True), redirect_stdout(out), redirect_stderr(err):
                status = main(command, open_notion=lambda _token: self.fake.client())
        return status, out.getvalue(), err.getvalue()

    def test_departments_are_live_in_display_order_with_option_ids_and_no_misc(self) -> None:
        status, out, err = self.run_command(["departments"])
        self.assertEqual((status, err), (0, ""))
        departments = json.loads(out)["departments"]
        names = [d["name"] for d in departments]
        self.assertEqual(names, ["TOC", "AE", "OE", "COOR", "COLOR", "VFX", "SOUND", "ADMIN"])
        self.assertEqual({d["name"]: d["id"] for d in departments}["TOC"], self.ids["TOC"])

    def test_contacts_find_returns_the_match_with_its_notion_id(self) -> None:
        status, out, _ = self.run_command(["contacts", "find", "@msantos"])
        self.assertEqual(status, 0)
        result = json.loads(out)
        self.assertEqual(result["match"]["id"], self.maria["id"])
        self.assertEqual(
            (result["match"]["name"], result["match"]["aliases"], result["match"]["end_of_shift"], result["needs_review"]),
            ("Maria Santos", ["Ria"], "22:00", False),
        )

    def test_contacts_find_by_alias(self) -> None:
        _, out, _ = self.run_command(["contacts", "find", "Ria"])
        self.assertEqual(json.loads(out)["match"]["id"], self.maria["id"])

    def test_contacts_find_gives_candidates_when_it_cannot_decide(self) -> None:
        status, out, _ = self.run_command(["contacts", "find", "Maria Santoz Cruz"])
        result = json.loads(out)
        self.assertEqual(status, 0)
        self.assertIsNone(result["match"])
        self.assertTrue(result["needs_review"])
        self.assertIn(self.maria["id"], [c["id"] for c in result["candidates"]])
        self.assertTrue(all(0 <= c["score"] <= 1 for c in result["candidates"]))

    def test_contacts_find_with_no_match_has_no_candidates(self) -> None:
        _, out, _ = self.run_command(["contacts", "find", "Zzzzzz Qqqqq"])
        result = json.loads(out)
        self.assertEqual((result["match"], result["method"], result["candidates"]), (None, "none", []))

    def test_projects_resolve_a_code(self) -> None:
        status, out, _ = self.run_command(["projects", "resolve", "ALP"])
        result = json.loads(out)
        self.assertEqual(status, 0)
        self.assertEqual((result["match"]["id"], result["match"]["project_code"], result["method"]), (self.alpha["id"], "ALP", "exact_id"))

    def test_projects_resolve_a_name_goes_to_review_with_candidates(self) -> None:
        _, out, _ = self.run_command(["projects", "resolve", "Alpha Show"])
        result = json.loads(out)
        self.assertIsNone(result["match"])
        self.assertTrue(result["needs_review"])
        self.assertEqual(result["candidates"][0]["id"], self.alpha["id"])

    def test_standing_cc_prints_the_contact_page_id(self) -> None:
        status, out, _ = self.run_command(["standing-cc"], environment={"T6_STANDING_CC": self.maria["id"]})
        self.assertEqual((status, json.loads(out)), (0, {"standing_cc": self.maria["id"]}))

    def test_standing_cc_is_null_when_unset(self) -> None:
        self.assertEqual(json.loads(self.run_command(["standing-cc"])[1]), {"standing_cc": None})

    def test_errors_are_json_on_stderr_with_exit_2(self) -> None:
        cases = [
            (["departments"], {"environment": {"T6_NOTION_TOKEN": ""}}),
            (["departments"], {"sources": {"contacts": CONTACTS}}),
            (["contacts", "find", "x"], {"sources": {"job_orders": JOB_ORDERS}}),
        ]
        for argv, kw in cases:
            with self.subTest(argv=argv, kw=kw):
                status, out, err = self.run_command(argv, **kw)
                self.assertEqual((status, out), (2, ""))
                self.assertIn("error", json.loads(err))


if __name__ == "__main__":
    unittest.main()
