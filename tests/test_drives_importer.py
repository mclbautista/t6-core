"""Import a synthetic per-project drive workbook into the in-memory DriveStore.

Every label, Project, serial and name here is invented. The workbook is built
in a temporary folder for each test; no real workbook is ever read.
"""

from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime
import io
import json
from pathlib import Path
import tempfile
from typing import Any
import unittest

from openpyxl import Workbook

from t6.cli import main
from t6.domain import Project
from t6.drives import CustodyKind, DriveStatus, Party, PartyKind, Ref, Role
from t6.drives.importer import ReviewItem, import_per_project_workbook
from t6.drives.testing import InMemoryDriveStore
from t6.lookup import Index, Lookup

HEADER = [
    "HARD DRIVE LABEL",
    "BRAND NAME",
    "SERIAL NUMBER",
    "TOTAL DISK SPACE",
    "FORMAT",
    "DATE RECEIVED",
    "DATE RELEASED",
    "REMARKS",
]
SHEETS: dict[str, list[list[Any]]] = {
    "PAPER MOON": [
        ["PROJECT NAME: PAPER MOON"],
        HEADER,
        ["PM MASTER 01", "ACME", "zx100aa1 ", "4TB, P", "EXFAT", "May 2, 2024", "June 10, 2024", "PAT SAMPLE"],
        ["PM BU 02", "ACME", "ZX100AA2", "500GB", None, datetime(2024, 5, 2), None, "spare cable in box"],
        ["PM EDIT", "ORBIT", "ZX100AA3", None, None, None, None, None],
        ["PM OFFLINE 01", "ACME", "ZX100AA4", "8TB", None, None, "Jun. 11,2024", None],
        # rows the importer must leave alone
        ["PM NO SERIAL", "ACME", None, "4TB", None, None, None, None],
        ["PM NOTE", "ACME", "ZX100AA5 T6 swing drive", "2TB", None, None, None, None],
        ["PM TWICE A", "ACME", "ZX100AA6", "4TB", None, None, None, None],
        ["PM TWICE B", "ACME", "zx100aa6", "4TB", None, None, None, None],
        ["PM BAD SIZE", "ACME", "ZX100AA7", "big", None, None, None, None],
        ["PM BAD DATE", "ACME", "ZX100AA8", "4TB", None, "04/05/23", None, None],
        [None, "ACME", "ZX100AA9", "4TB", None, None, None, None],
        ["PM SWING 07 (OLD)", "ACME", "ZX100AB1", "1TB", None, None, None, None],
        ["PM SWING 07 NEW", "ACME", "ZX100AB2", "1TB", None, None, None, None],
        ["PM NO SERIAL", "ACME", None, "4TB", None, None, None, None],
    ],
    "GLASS HARBOR": [
        ["PROJECT NAME: GLASS HARBOUR (WORKING TITLE)"],
        [],
        HEADER[:6] + ["DATE RELEASED", "PERSON IN CHARGE", "REMARKS"],
        ["GH SWING 01", "ORBIT", "QQ200BB1", "1TB", None, "July 1, 2024", "July 9, 2024", "SAM EXAMPLE", "red case"],
    ],
    "UNTITLED TAB": [
        HEADER,
        ["MASTER BACK UP", "ACME", "QQ300CC1", "4TB", None, None, None, None],
    ],
}
PAPER_MOON = Ref(page_id="project-paper-moon")
LOGGED_BY = Ref(text="workbook import")


def write_workbook(folder: str) -> Path:
    workbook = Workbook(write_only=True)
    for title, rows in SHEETS.items():
        sheet = workbook.create_sheet(title)
        for row in rows:
            sheet.append(row)
    path = Path(folder) / "per-project.xlsx"
    workbook.save(path)
    return path


def make_lookup() -> Lookup:
    return Lookup(Index(projects=(Project(page_id="project-paper-moon", name="Paper Moon"),)))


class PerProjectImportTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = write_workbook(folder.name)
        self.store = InMemoryDriveStore()
        self.result = self.run_import()

    def run_import(self) -> Any:
        return import_per_project_workbook(
            self.path, self.store, lookup=make_lookup(), logged_by=LOGGED_BY
        )

    def test_a_clean_row_becomes_a_drive_with_its_facts(self) -> None:
        drive = self.store.get_drive("ZX100AA1")
        assert drive is not None
        self.assertEqual(
            (drive.brand, drive.capacity_tb, drive.format), ("ACME", 4.0, "EXFAT")
        )
        half = self.store.get_drive("ZX100AA2")
        assert half is not None
        self.assertEqual(half.capacity_tb, 0.5)

    def test_a_clean_row_gets_an_assignment_for_its_project(self) -> None:
        (assignment,) = self.store.assignment_history("ZX100AA1")
        self.assertEqual(assignment.label, "PM MASTER 01")
        self.assertEqual(assignment.project, PAPER_MOON)
        self.assertIs(assignment.role, Role.MASTER)
        self.assertEqual(assignment.start, date(2024, 5, 2))
        self.assertTrue(assignment.is_open)
        self.assertEqual(
            [d.serial for d, _ in self.store.open_assignments(PAPER_MOON) if d.serial][-2:],
            ["ZX100AA2", "ZX100AA1"],
        )

    def test_a_received_date_becomes_a_received_event(self) -> None:
        (received,) = self.store.list_events("ZX100AA2")
        self.assertIs(received.kind, CustodyKind.RECEIVED)
        self.assertEqual(received.on, date(2024, 5, 2))
        self.assertIsNone(received.party)
        self.assertEqual(received.project, PAPER_MOON)
        self.assertEqual(received.logged_by, LOGGED_BY)
        self.assertIs(self.store.status("ZX100AA2"), DriveStatus.IN_CUSTODY)

    def test_a_release_date_and_recipient_become_a_released_event(self) -> None:
        received, released = self.store.list_events("ZX100AA1")
        self.assertIs(received.kind, CustodyKind.RECEIVED)
        self.assertIs(released.kind, CustodyKind.RELEASED)
        self.assertEqual(released.on, date(2024, 6, 10))
        self.assertEqual(released.party, Party(PartyKind.CONTACT, Ref(text="PAT SAMPLE")))
        self.assertIs(self.store.status("ZX100AA1"), DriveStatus.RELEASED)
        # the remark was the recipient, so it is not repeated on the Assignment
        self.assertIsNone(self.store.assignment_history("ZX100AA1")[0].remarks)

    def test_the_person_in_charge_is_the_recipient_when_the_tab_has_that_column(self) -> None:
        _, released = self.store.list_events("QQ200BB1")
        self.assertEqual(released.party, Party(PartyKind.CONTACT, Ref(text="SAM EXAMPLE")))
        (assignment,) = self.store.assignment_history("QQ200BB1")
        self.assertEqual(assignment.remarks, "red case")
        self.assertIs(assignment.role, Role.SWING)

    def test_a_release_with_no_recipient_is_logged_without_a_party(self) -> None:
        (released,) = self.store.list_events("ZX100AA4")
        self.assertIs(released.kind, CustodyKind.RELEASED)
        self.assertEqual(released.on, date(2024, 6, 11))
        self.assertIsNone(released.party)

    def test_a_row_with_no_dates_has_an_assignment_and_no_events(self) -> None:
        (assignment,) = self.store.assignment_history("ZX100AA3")
        self.assertIsNone(assignment.start)
        self.assertIsNone(assignment.role)
        self.assertEqual(self.store.list_events("ZX100AA3"), ())
        self.assertIsNone(self.store.status("ZX100AA3"))
        self.assertEqual(self.store.assignment_history("ZX100AA2")[0].remarks, "spare cable in box")

    def test_a_project_name_is_resolved_with_lookup_or_kept_as_text(self) -> None:
        (unresolved,) = self.store.assignment_history("QQ200BB1")
        self.assertEqual(unresolved.project, Ref(text="GLASS HARBOUR (WORKING TITLE)"))
        self.assertTrue(unresolved.project.unresolved)
        (untitled,) = self.store.assignment_history("QQ300CC1")
        self.assertEqual(untitled.project, Ref(text="UNTITLED TAB"))
        self.assertIsNone(untitled.role)  # the label names two roles

    def test_rows_that_cannot_be_read_are_not_imported_and_are_reported(self) -> None:
        not_imported = [item for item in self.result.review if item.sheet == "PAPER MOON" and 8 <= item.row <= 13]
        self.assertEqual(
            not_imported,
            [
                ReviewItem("PAPER MOON", 8, "note in serial cell", "ZX100AA5 T6 swing drive"),
                ReviewItem("PAPER MOON", 9, "shared serial", "ZX100AA6 is also on PAPER MOON row 10"),
                ReviewItem("PAPER MOON", 10, "shared serial", "ZX100AA6 is also on PAPER MOON row 9"),
                ReviewItem(
                    "PAPER MOON",
                    11,
                    "unreadable",
                    "cannot read capacity 'big': expected one size such as '4TB' or '500GB'",
                ),
                ReviewItem("PAPER MOON", 12, "unreadable", "cannot read the date '04/05/23'"),
                ReviewItem("PAPER MOON", 13, "no label"),
            ],
        )
        for serial in ("ZX100AA5", "ZX100AA6", "ZX100AA7", "ZX100AA8", "ZX100AA9"):
            self.assertIsNone(self.store.get_drive(serial), serial)
        self.assertEqual(self.result.imported, 10)

    def test_a_blank_serial_becomes_a_provisional_drive_and_is_reported(self) -> None:
        provisional = [
            (drive, assignment)
            for drive, assignment in self.store.open_assignments(PAPER_MOON)
            if drive.no_serial
        ]
        # two rows with the same label and no serial are two drives
        self.assertEqual([a.label for _, a in provisional], ["PM NO SERIAL", "PM NO SERIAL"])
        self.assertNotEqual(provisional[0][0].key, provisional[1][0].key)
        self.assertEqual(provisional[0][0].capacity_tb, 4.0)
        detail = "PM NO SERIAL is recorded as a provisional Drive"
        for row in (7, 16):
            self.assertIn(ReviewItem("PAPER MOON", row, "blank serial", detail), self.result.review)

    def test_an_old_and_new_label_pair_is_imported_as_written_and_reported(self) -> None:
        self.assertEqual(self.store.assignment_history("ZX100AB1")[0].label, "PM SWING 07 (OLD)")
        self.assertEqual(self.store.assignment_history("ZX100AB2")[0].label, "PM SWING 07 NEW")
        self.assertIn(
            ReviewItem("PAPER MOON", 14, "old and new labels", "PM SWING 07 (OLD) pairs with PM SWING 07 NEW"),
            self.result.review,
        )
        self.assertIn(
            ReviewItem("PAPER MOON", 15, "old and new labels", "PM SWING 07 NEW pairs with PM SWING 07 (OLD)"),
            self.result.review,
        )

    def test_an_unresolved_project_is_reported_once_at_its_block(self) -> None:
        unresolved = [item for item in self.result.review if item.reason == "unresolved project"]
        self.assertEqual(
            unresolved,
            [
                ReviewItem("GLASS HARBOR", 1, "unresolved project", "GLASS HARBOUR (WORKING TITLE)"),
                ReviewItem("UNTITLED TAB", 1, "unresolved project", "UNTITLED TAB"),
            ],
        )

    def test_a_second_run_changes_nothing(self) -> None:
        serials = ["ZX100AA1", "ZX100AA2", "ZX100AA3", "ZX100AA4", "ZX100AB1", "QQ200BB1", "QQ300CC1"]

        def snapshot() -> list[Any]:
            return [
                (
                    self.store.get_drive(serial),
                    self.store.assignment_history(serial),
                    self.store.list_events(serial),
                )
                for serial in serials
            ] + [self.store.open_assignments(PAPER_MOON)]

        before = snapshot()
        again = self.run_import()
        self.assertEqual(snapshot(), before)
        self.assertEqual(again, self.result)


class DryRunCommandTests(unittest.TestCase):
    def test_the_dry_run_prints_the_report_and_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = write_workbook(folder)
            projects = Path(folder) / "projects.json"
            projects.write_text(json.dumps([{"page_id": "project-paper-moon", "name": "Paper Moon"}]))
            before = {p.name: p.stat().st_mtime_ns for p in Path(folder).iterdir()}
            printed = io.StringIO()
            with redirect_stdout(printed):
                status = main(["drives-import-dry-run", str(path), "--projects", str(projects)])
            after = {p.name: p.stat().st_mtime_ns for p in Path(folder).iterdir()}

        self.assertEqual(status, 0)
        self.assertEqual(after, before)
        report = json.loads(printed.getvalue())
        self.assertEqual(report["imported"], 10)
        reasons = {item["reason"] for item in report["review"]}
        self.assertEqual(
            reasons,
            {
                "no label",
                "note in serial cell",
                "shared serial",
                "unreadable",
                "blank serial",
                "old and new labels",
                "unresolved project",
            },
        )
        self.assertIn(
            {"sheet": "PAPER MOON", "row": 13, "reason": "no label", "detail": ""}, report["review"]
        )
        self.assertNotIn("PAPER MOON", [i["detail"] for i in report["review"] if i["reason"] == "unresolved project"])

    def test_a_missing_workbook_is_an_error(self) -> None:
        errors = io.StringIO()
        with redirect_stderr(errors):
            status = main(["drives-import-dry-run", "/nonexistent/workbook.xlsx"])
        self.assertEqual(status, 2)
        self.assertIn("error", json.loads(errors.getvalue()))


if __name__ == "__main__":
    unittest.main()
