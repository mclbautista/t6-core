"""Import synthetic drive workbooks into the in-memory DriveStore.

Every label, Project, serial and name here is invented. The workbooks are built
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
from t6.drives import CustodyKind, DriveStatus, Ownership, Party, PartyKind, Ref, Role
from t6.drives.importer import (
    ReviewItem,
    import_log_sheet_workbook,
    import_per_project_workbook,
)
from t6.drives.testing import InMemoryDriveStore
from test_drives import drive_id
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


LOG_SHEETS: dict[str, list[list[Any]]] = {
    "Swing Drive": [
        ["SWING DRIVE"],
        ["HARD DRIVE LABEL", "TOTAL DISK SPACE", "FORMAT", "SERIAL #", "REMARKS"],
        ["SWING 01", "2TB, WD", None, "SW100AA1", None],
        ["SWING 02", "4TB, WD", None, "SW100AA2", "DEAD DRIVE"],
        ["SWING 03", "2TB, WD", None, None, None],
        ["SWING 04 (OLD)", "2TB, WD", None, "SW100AA4", "now an archive"],
        ["SWING 04 (NEW)", "1TB, WD", None, "SW100AA5", None],
        ["SWING 05", "2TB, WD", None, "SW100AA6", None],
        ["SWING 06", "2TB, WD", None, "zx100aa1", None],  # also in the per-project workbook
    ],
    "Released Batch": [
        ["DATE: JUNE 20, 2024"],
        ["HARD DRIVE LABEL", "SERIAL NUMBER", "QUANTITY"],
        ["PAPER MOON", None, 2],
        ["PM MASTER 02", "RB200AA1", None],
        ["PM BU 03", "RB200AA2", None],
        ["GLASS HARBOUR", None, 1],
        ["GH MASTER", "RB200AA3", None],
        [None, "TOTAL", 3],
    ],
    "Archive HD": [
        ["HARD DRIVE LABEL", "HD DESCRIPTION", "FORMAT", "SERIAL #", "TOTAL DISK SPACE", "REMARKS"],
        ["ARCHIVE 01", "PAPER MOON- 1.2TB", None, "AR300AA1", "4TB", None],
        [None, "GLASS HARBOUR- 80GB", None, None, None, None],
        ["ARCHIVE 02", None, None, "SW100AA6", None, "was SWING 05"],
    ],
    "January": [
        ["HARD DRIVE LOG SHEET"],
        ["DATE", "PROJECT NAME", "HARD DRIVE LABEL", "HD DESCRIPTION", "IN/OUT", "RECEIVED BY", "REMARKS"],
        ["January 4, 2024", "PAPER MOON", "PM DAY 1 MASTER", "6TB, WD", "IN", "KIM SAMPLE", "S/N: MV400AA1"],
        ["January 9, 2024", "PAPER MOON", "PM DAY 1 MASTER", "6TB, WD", "OUT", "LEE EXAMPLE", "S/N: MV400AA1"],
        ["January 9, 2024", "PAPER MOON", "PM DAY 1 BACK UP", "WD", "OUT", "Lee Example", "S/N:MV400AA2"],
        ["January 10, 2024", "PAPER MOON", "PM EDIT", "2TB, WD", "IN", "KIM SAMPLE", None],
        ["SEPT 1, 2024", "GLASS HARBOUR", "GH TRANSIT", "PORTABLE", "OUT", "PROD", None],
        ["January 11, 2024", "PAPER MOON", "PM DCP", "1TB", "IN/OUT", "KIM SAMPLE", "S/N: MV400AA3"],
    ],
}


def write_workbook(
    folder: str, sheets: dict[str, list[list[Any]]] = SHEETS, name: str = "per-project.xlsx"
) -> Path:
    workbook = Workbook(write_only=True)
    for title, rows in sheets.items():
        sheet = workbook.create_sheet(title)
        for row in rows:
            sheet.append(row)
    path = Path(folder) / name
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
        drive = self.store.find_by_serial("ZX100AA1")
        assert drive is not None
        self.assertEqual(
            (drive.brand, drive.capacity_tb, drive.format), ("ACME", 4.0, "EXFAT")
        )
        half = self.store.find_by_serial("ZX100AA2")
        assert half is not None
        self.assertEqual(half.capacity_tb, 0.5)

    def test_an_imported_drive_keeps_unknown_ownership(self) -> None:
        drive = self.store.find_by_serial("ZX100AA1")
        assert drive is not None
        self.assertIs(drive.ownership, Ownership.UNKNOWN)
        self.assertFalse(drive.owned_by_t6)

    def test_a_clean_row_gets_an_assignment_for_its_project(self) -> None:
        (assignment,) = self.store.assignment_history(drive_id(self.store, "ZX100AA1"))
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
        (received,) = self.store.list_events(drive_id(self.store, "ZX100AA2"))
        self.assertIs(received.kind, CustodyKind.RECEIVED)
        self.assertEqual(received.on, date(2024, 5, 2))
        self.assertIsNone(received.party)
        self.assertEqual(received.project, PAPER_MOON)
        self.assertEqual(received.logged_by, LOGGED_BY)
        self.assertIs(self.store.status(drive_id(self.store, "ZX100AA2")), DriveStatus.IN_CUSTODY)

    def test_a_release_date_and_recipient_become_a_released_event(self) -> None:
        received, released = self.store.list_events(drive_id(self.store, "ZX100AA1"))
        self.assertIs(received.kind, CustodyKind.RECEIVED)
        self.assertIs(released.kind, CustodyKind.RELEASED)
        self.assertEqual(released.on, date(2024, 6, 10))
        self.assertEqual(released.party, Party(PartyKind.CONTACT, Ref(text="PAT SAMPLE")))
        self.assertIs(self.store.status(drive_id(self.store, "ZX100AA1")), DriveStatus.RELEASED)
        # the remark was the recipient, so it is not repeated on the Assignment
        self.assertIsNone(self.store.assignment_history(drive_id(self.store, "ZX100AA1"))[0].remarks)

    def test_the_person_in_charge_is_the_recipient_when_the_tab_has_that_column(self) -> None:
        _, released = self.store.list_events(drive_id(self.store, "QQ200BB1"))
        self.assertEqual(released.party, Party(PartyKind.CONTACT, Ref(text="SAM EXAMPLE")))
        (assignment,) = self.store.assignment_history(drive_id(self.store, "QQ200BB1"))
        self.assertEqual(assignment.remarks, "red case")
        self.assertIs(assignment.role, Role.SWING)

    def test_a_release_with_no_recipient_is_logged_without_a_party(self) -> None:
        (released,) = self.store.list_events(drive_id(self.store, "ZX100AA4"))
        self.assertIs(released.kind, CustodyKind.RELEASED)
        self.assertEqual(released.on, date(2024, 6, 11))
        self.assertIsNone(released.party)

    def test_a_row_with_no_dates_has_an_assignment_and_no_events(self) -> None:
        (assignment,) = self.store.assignment_history(drive_id(self.store, "ZX100AA3"))
        self.assertIsNone(assignment.start)
        self.assertIsNone(assignment.role)
        self.assertEqual(self.store.list_events(drive_id(self.store, "ZX100AA3")), ())
        self.assertIsNone(self.store.status(drive_id(self.store, "ZX100AA3")))
        self.assertEqual(self.store.assignment_history(drive_id(self.store, "ZX100AA2"))[0].remarks, "spare cable in box")

    def test_a_project_name_is_resolved_with_lookup_or_kept_as_text(self) -> None:
        (unresolved,) = self.store.assignment_history(drive_id(self.store, "QQ200BB1"))
        self.assertEqual(unresolved.project, Ref(text="GLASS HARBOUR (WORKING TITLE)"))
        self.assertTrue(unresolved.project.unresolved)
        (untitled,) = self.store.assignment_history(drive_id(self.store, "QQ300CC1"))
        self.assertEqual(untitled.project, Ref(text="UNTITLED TAB"))
        self.assertIsNone(untitled.role)  # the label names two roles

    def test_uncertain_rows_are_preserved_and_unreadable_rows_are_reported(self) -> None:
        not_imported = [item for item in self.result.review if item.sheet == "PAPER MOON" and 8 <= item.row <= 13]
        self.assertEqual(
            [ReviewItem(i.sheet, i.row, i.reason, i.detail.split("\n", 1)[0], i.workbook)
             for i in not_imported],
            [
                ReviewItem("PAPER MOON", 8, "note in serial cell", "ZX100AA5 T6 swing drive", workbook=str(self.path.resolve())),
                ReviewItem("PAPER MOON", 9, "shared serial", "ZX100AA6 is also on PAPER MOON row 10", workbook=str(self.path.resolve())),
                ReviewItem("PAPER MOON", 10, "shared serial", "ZX100AA6 is also on PAPER MOON row 9", workbook=str(self.path.resolve())),
                ReviewItem(
                    "PAPER MOON",
                    11,
                    "unreadable",
                    "cannot read capacity 'big': expected one size such as '4TB' or '500GB'",
                 workbook=str(self.path.resolve())),
                ReviewItem("PAPER MOON", 12, "unreadable", "cannot read the date '04/05/23'", workbook=str(self.path.resolve())),
                ReviewItem("PAPER MOON", 13, "no label", workbook=str(self.path.resolve())),
            ],
        )
        for serial in ("ZX100AA5", "ZX100AA6"):
            self.assertIsNone(self.store.find_by_serial(serial), serial)
        bad_size = self.store.find_by_serial("ZX100AA7")
        assert bad_size is not None
        self.assertEqual((bad_size.capacity_tb, bad_size.needs_review), (None, True))
        bad_date = self.store.find_by_serial("ZX100AA8")
        assert bad_date is not None
        self.assertEqual(bad_date.capacity_tb, 4)
        self.assertEqual([e.on for e in self.store.list_events(bad_date.id)], [None])
        self.assertEqual(self.result.imported, 16)

    def test_a_blank_serial_becomes_a_permanent_drive_and_is_reported(self) -> None:
        drives_without_serials = [
            (drive, assignment)
            for drive, assignment in self.store.open_assignments(PAPER_MOON)
            if drive.no_serial and drive.suspected_serial is None
        ]
        # two rows with the same label and no serial are two drives
        self.assertEqual([a.label for _, a in drives_without_serials], ["PM NO SERIAL", "PM NO SERIAL"])
        self.assertNotEqual(drives_without_serials[0][0].key, drives_without_serials[1][0].key)
        self.assertEqual(drives_without_serials[0][0].capacity_tb, 4.0)
        detail = "no accepted serial recorded"
        for row in (7, 16):
            self.assertIn(ReviewItem("PAPER MOON", row, "blank serial", detail, workbook=str(self.path.resolve())), self.result.review)

    def test_an_old_and_new_label_pair_is_imported_as_written_and_reported(self) -> None:
        self.assertEqual(self.store.assignment_history(drive_id(self.store, "ZX100AB1"))[0].label, "PM SWING 07 (OLD)")
        self.assertEqual(self.store.assignment_history(drive_id(self.store, "ZX100AB2"))[0].label, "PM SWING 07 NEW")
        self.assertIn(
            ReviewItem("PAPER MOON", 14, "old and new labels", "PM SWING 07 (OLD) pairs with PM SWING 07 NEW", workbook=str(self.path.resolve())),
            self.result.review,
        )
        self.assertIn(
            ReviewItem("PAPER MOON", 15, "old and new labels", "PM SWING 07 NEW pairs with PM SWING 07 (OLD)", workbook=str(self.path.resolve())),
            self.result.review,
        )

    def test_an_unresolved_project_is_reported_once_at_its_block(self) -> None:
        unresolved = [item for item in self.result.review if item.reason == "unresolved project"]
        self.assertEqual(
            unresolved,
            [
                ReviewItem("GLASS HARBOR", 1, "unresolved project", "GLASS HARBOUR (WORKING TITLE)", workbook=str(self.path.resolve())),
                ReviewItem("UNTITLED TAB", 1, "unresolved project", "UNTITLED TAB", workbook=str(self.path.resolve())),
            ],
        )

    def test_a_second_run_changes_nothing(self) -> None:
        serials = ["ZX100AA1", "ZX100AA2", "ZX100AA3", "ZX100AA4", "ZX100AB1", "QQ200BB1", "QQ300CC1"]

        def snapshot() -> list[Any]:
            return [
                (
                    self.store.find_by_serial(serial),
                    self.store.assignment_history(drive_id(self.store, serial)),
                    self.store.list_events(drive_id(self.store, serial)),
                )
                for serial in serials
            ] + [self.store.open_assignments(PAPER_MOON)]

        before = snapshot()
        again = self.run_import()
        self.assertEqual(snapshot(), before)
        self.assertEqual(again, self.result)


class LogSheetImportTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.per_project = write_workbook(folder.name)
        self.path = write_workbook(folder.name, LOG_SHEETS, "log-sheet.xlsx")
        self.store = InMemoryDriveStore()
        import_per_project_workbook(
            self.per_project, self.store, lookup=make_lookup(), logged_by=LOGGED_BY
        )
        self.result = self.run_import()

    def run_import(self) -> Any:
        return import_log_sheet_workbook(
            self.path, self.store, lookup=make_lookup(), logged_by=LOGGED_BY
        )

    def test_an_inventory_row_becomes_a_drive_under_its_tab(self) -> None:
        drive = self.store.find_by_serial("SW100AA1")
        assert drive is not None
        self.assertEqual(drive.capacity_tb, 2.0)
        (assignment,) = self.store.assignment_history(drive_id(self.store, "SW100AA1"))
        self.assertEqual((assignment.label, assignment.project), ("SWING 01", Ref(text="Swing Drive")))
        self.assertIs(assignment.role, Role.SWING)
        self.assertEqual(self.store.list_events(drive_id(self.store, "SW100AA1")), ())
        (archived,) = self.store.assignment_history(drive_id(self.store, "AR300AA1"))
        self.assertIs(archived.role, Role.ARCHIVE)

    def test_a_batch_released_on_one_date_is_one_shipment(self) -> None:
        shipment = self.store.shipment_events("2024-06-20 Released Batch")
        self.assertEqual([drive.serial for drive, _ in shipment], ["RB200AA1", "RB200AA2", "RB200AA3"])
        for _, released in shipment:
            self.assertIs(released.kind, CustodyKind.RELEASED)
            self.assertEqual(released.on, date(2024, 6, 20))
        # the group heading above each drive is its Project
        self.assertEqual(self.store.assignment_history(drive_id(self.store, "RB200AA1"))[0].project, PAPER_MOON)
        self.assertEqual(self.store.assignment_history(drive_id(self.store, "RB200AA3"))[0].project, Ref(text="GLASS HARBOUR"))
        self.assertIs(self.store.status(drive_id(self.store, "RB200AA2")), DriveStatus.RELEASED)

    def test_movements_out_on_one_date_to_one_recipient_are_one_shipment(self) -> None:
        shipment = self.store.shipment_events("2024-01-09 to LEE EXAMPLE")
        self.assertEqual([drive.serial for drive, _ in shipment], ["MV400AA1", "MV400AA2"])
        self.assertEqual(shipment[0][1].party, Party(PartyKind.CONTACT, Ref(text="LEE EXAMPLE")))

    def test_a_movement_with_a_serial_becomes_a_custody_event(self) -> None:
        received, released = self.store.list_events(drive_id(self.store, "MV400AA1"))
        self.assertIs(received.kind, CustodyKind.RECEIVED)
        self.assertEqual(received.on, date(2024, 1, 4))
        self.assertEqual(received.reason, "received by KIM SAMPLE\nS/N: MV400AA1")
        self.assertIs(released.kind, CustodyKind.RELEASED)
        (assignment,) = self.store.assignment_history(drive_id(self.store, "MV400AA1"))
        self.assertEqual((assignment.label, assignment.project), ("PM DAY 1 MASTER", PAPER_MOON))
        drive = self.store.find_by_serial("MV400AA1")
        assert drive is not None
        self.assertEqual(drive.capacity_tb, 6.0)
        other = self.store.find_by_serial("MV400AA2")
        assert other is not None
        self.assertIsNone(other.capacity_tb)  # "WD" names no size

    def test_a_row_marked_dead_becomes_a_retired_event_with_the_remark(self) -> None:
        (retired,) = self.store.list_events(drive_id(self.store, "SW100AA2"))
        self.assertIs(retired.kind, CustodyKind.RETIRED)
        self.assertEqual(retired.reason, "DEAD DRIVE")
        self.assertIsNone(retired.on)
        self.assertIs(self.store.status(drive_id(self.store, "SW100AA2")), DriveStatus.RETIRED)

    def test_a_drive_in_both_workbooks_is_one_drive_with_the_combined_history(self) -> None:
        drive = self.store.find_by_serial("ZX100AA1")
        assert drive is not None
        self.assertEqual(drive.brand, "ACME")  # the record from the first workbook is kept
        self.assertEqual(
            {a.label for a in self.store.assignment_history(drive_id(self.store, "ZX100AA1"))}, {"PM MASTER 01", "SWING 06"}
        )
        self.assertEqual(len(self.store.list_events(drive_id(self.store, "ZX100AA1"))), 2)

    def test_doubtful_rows_go_to_the_review_report(self) -> None:
        self.assertEqual(self.result.imported, 16)
        self.assertEqual(
            [(i.sheet, i.row, i.reason) for i in self.result.review],
            [
                ("Swing Drive", 1, "unresolved project"),
                ("Swing Drive", 5, "blank serial"),
                ("Swing Drive", 6, "old and new labels"),
                ("Swing Drive", 7, "old and new labels"),
                ("Swing Drive", 8, "shared serial"),
                ("Swing Drive", 9, "additional drive facts"),
                ("Released Batch", 6, "unresolved project"),
                ("Released Batch", 8, "summary row"),
                ("Archive HD", 1, "unresolved project"),
                ("Archive HD", 3, "no label"),
                ("Archive HD", 3, "blank serial"),
                ("Archive HD", 4, "shared serial"),
                ("January", 6, "movement with no serial"),
                ("January", 7, "movement with no serial"),
                ("January", 8, "unreadable"),
            ],
        )
        self.assertTrue(all(i.workbook == str(self.path.resolve()) for i in self.result.review))
        movements = [i for i in self.result.review if i.reason == "movement with no serial"]
        self.assertIn("PM EDIT", movements[0].detail)
        self.assertIn("GH TRANSIT", movements[1].detail)
        self.assertIsNone(self.store.find_by_serial("SW100AA6"))
        self.assertIsNone(self.store.find_by_serial("MV400AA3"))

    def test_a_second_run_changes_nothing(self) -> None:
        serials = ["SW100AA1", "SW100AA2", "SW100AA4", "ZX100AA1", "RB200AA1", "AR300AA1", "MV400AA1", "MV400AA2"]

        def snapshot() -> list[Any]:
            return [
                (
                    self.store.find_by_serial(serial),
                    self.store.assignment_history(drive_id(self.store, serial)),
                    self.store.list_events(drive_id(self.store, serial)),
                )
                for serial in serials
            ] + [
                self.store.open_assignments(Ref(text="Swing Drive")),
                self.store.shipment_events("2024-06-20 Released Batch"),
            ]

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
        self.assertEqual(report["imported"], 16)
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
            {"sheet": "PAPER MOON", "row": 13, "reason": "no label", "detail": "", "workbook": str(path.resolve())}, report["review"]
        )
        self.assertNotIn("PAPER MOON", [i["detail"] for i in report["review"] if i["reason"] == "unresolved project"])

    def test_the_dry_run_can_add_the_log_sheet(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = write_workbook(folder)
            log_sheet = write_workbook(folder, LOG_SHEETS, "log-sheet.xlsx")
            printed = io.StringIO()
            with redirect_stdout(printed):
                status = main(["drives-import-dry-run", str(path), "--log-sheet", str(log_sheet)])

        self.assertEqual(status, 0)
        report = json.loads(printed.getvalue())
        self.assertEqual(report["imported"], 32)
        self.assertIn("movement with no serial", {item["reason"] for item in report["review"]})

    def test_a_missing_workbook_is_an_error(self) -> None:
        errors = io.StringIO()
        with redirect_stderr(errors):
            status = main(["drives-import-dry-run", "/nonexistent/workbook.xlsx"])
        self.assertEqual(status, 2)
        self.assertIn("error", json.loads(errors.getvalue()))


if __name__ == "__main__":
    unittest.main()
