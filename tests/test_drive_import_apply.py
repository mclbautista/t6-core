"""`t6 admin drive import --apply`: the previewed workbook import into the t6 database. Synthetic workbooks only."""
import contextlib
import io
import json
import tempfile
import unittest
from typing import Any
from pathlib import Path

from t6.cli import main
from t6.drives.sqlite import SqliteDriveStore
from t6.registry import Registered, SqliteProjectRegistry
from test_drives_importer import LOG_SHEETS, SHEETS, write_workbook

PROJECTS = [{"page_id": "project-paper-moon", "name": "Paper Moon"}, {"page_id": "project-glass-harbour", "name": "Glass Harbour"}]
COUNTS = ("drives", "assignments", "events", "exceptions")


class DriveImportApplyTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.workbook = write_workbook(folder.name, SHEETS)
        self.log_sheet = write_workbook(folder.name, LOG_SHEETS, "log-sheet.xlsx")
        self.projects = self.folder / "projects.json"
        self.projects.write_text(json.dumps(PROJECTS))
        self.database = self.folder / "registry.sqlite"
        self.registry = SqliteProjectRegistry(self.database)

    def bind(self, page_id: str) -> None:
        outcome = self.registry.register(actor="setup", idempotency_key=page_id, notion_page_id=page_id)
        assert isinstance(outcome, Registered)

    def files(self) -> list[str]:
        return [str(self.workbook), "--log-sheet", str(self.log_sheet), "--projects", str(self.projects)]

    def run_cli(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = main(["admin", "drive", "import", *args])
        return status, out.getvalue(), err.getvalue()

    def preview(self) -> dict[str, Any]:
        status, out, _ = self.run_cli(*self.files())
        self.assertEqual(status, 0)
        report: dict[str, Any] = json.loads(out)
        (self.folder / "preview.json").write_text(out)
        return report

    def apply(self, *extra: str) -> tuple[int, str, str]:
        return self.run_cli(*self.files(), "--apply", "--preview", str(self.folder / "preview.json"),
                            "--actor", "tester", "--registry-db", str(self.database), *extra)

    def counts(self) -> tuple[int, int, int, int]:
        store = SqliteDriveStore(self.registry)
        drives = store.list_drives()
        return (len(drives), sum(len(store.list_assignments(d.id)) for d in drives),
                sum(len(store.list_events(d.id)) for d in drives), len(store.exceptions()))

    def test_the_preview_has_a_digest_of_the_files_given(self) -> None:
        first = self.preview()["digest"]
        self.assertEqual(self.preview()["digest"], first)
        self.assertEqual(len(first), 64)
        workbook_only = json.loads(self.run_cli(str(self.workbook))[1])["digest"]
        with_log_sheet = json.loads(self.run_cli(str(self.workbook), "--log-sheet", str(self.log_sheet))[1])["digest"]
        with_projects = json.loads(self.run_cli(str(self.workbook), "--projects", str(self.projects))[1])["digest"]
        self.assertEqual(len({first, workbook_only, with_log_sheet, with_projects}), 4)
        self.projects.write_text(json.dumps(PROJECTS[:1]))
        self.assertNotEqual(self.preview()["digest"], first)

    def test_apply_writes_the_t6_database_and_a_second_apply_adds_nothing(self) -> None:
        self.bind("project-paper-moon")
        self.bind("project-glass-harbour")
        preview = self.preview()
        status, out, _ = self.apply()
        self.assertEqual(status, 0)
        report = json.loads(out)
        self.assertEqual((report["digest"], report["imported"], report["review"]), (preview["digest"], preview["imported"], preview["review"]))
        written = self.counts()
        self.assertEqual(tuple(report["written"][name] for name in COUNTS), written)
        self.assertTrue(all(written))
        again = json.loads(self.apply()[1])
        self.assertEqual(self.counts(), written)
        self.assertEqual(again["written"], dict.fromkeys(COUNTS, 0))
        store = SqliteDriveStore(self.registry)
        logged_by = {event.logged_by.text for drive in store.list_drives() for event in store.list_events(drive.id)}
        self.assertEqual(logged_by, {"tester"})

    def test_the_drive_reads_show_the_result(self) -> None:
        from t6 import drive_reads
        self.bind("project-paper-moon")
        self.preview()
        self.assertEqual(self.apply()[0], 0)
        store = SqliteDriveStore(self.registry)
        self.assertEqual(len(drive_reads.list_drives(store).drives), self.counts()[0])
        self.assertEqual(len(drive_reads.exceptions(store).exceptions), self.counts()[3])

    def test_a_changed_file_is_refused_and_writes_nothing(self) -> None:
        self.bind("project-paper-moon")
        self.preview()
        for changed in (self.workbook, self.log_sheet, self.projects):
            with self.subTest(changed=changed.name):
                original = changed.read_bytes()
                changed.write_bytes(original + b" ")
                status, out, err = self.apply()
                changed.write_bytes(original)
                self.assertEqual((status, out), (2, ""))
                self.assertIn("changed since the preview", err)
                self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_a_project_not_bound_in_the_registry_is_an_exception_not_a_crash(self) -> None:
        self.preview()  # --projects names Paper Moon, but no registry page is bound to it
        status, out, err = self.apply()
        self.assertEqual((status, err), (0, ""))
        unresolved = {item.sheet for item in SqliteDriveStore(self.registry).exceptions() if item.reason == "unresolved project"}
        self.assertIn("PAPER MOON", unresolved)
        self.assertEqual(self.counts()[0], json.loads(out)["written"]["drives"])
        self.assertGreater(self.counts()[0], 0)

    def test_a_bound_project_is_not_an_exception(self) -> None:
        self.bind("project-paper-moon")
        self.preview()
        self.apply()
        unresolved = {item.sheet for item in SqliteDriveStore(self.registry).exceptions() if item.reason == "unresolved project"}
        self.assertNotIn("PAPER MOON", unresolved)

    def test_apply_needs_a_preview_and_an_actor(self) -> None:
        self.preview()
        for args, message in (
            ([*self.files(), "--apply", "--actor", "tester"], "--apply needs --preview FILE"),
            ([*self.files(), "--apply", "--preview", str(self.folder / "preview.json")], "--apply needs --actor NAME"),
        ):
            with self.subTest(message=message):
                status, out, err = self.run_cli(*args, "--registry-db", str(self.database))
                self.assertEqual((status, out), (2, ""))
                self.assertIn(message, err)
        self.assertEqual(self.counts(), (0, 0, 0, 0))


if __name__ == "__main__":
    unittest.main()
