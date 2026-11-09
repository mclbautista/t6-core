"""Exercise t6.drives through the DriveStore interface and its in-memory fake.

Serials, brands and studio IDs here are made up.
"""

from datetime import date
import unittest

from t6.drives import (
    Assignment,
    Drive,
    DriveStore,
    DuplicateSerial,
    FormFactor,
    ProjectRef,
    Role,
    UnknownDrive,
    normalize_serial,
    parse_capacity_tb,
)
from t6.drives.testing import InMemoryDriveStore


def make_store() -> DriveStore:
    return InMemoryDriveStore()


class DriveFactsTests(unittest.TestCase):
    def test_a_stored_drive_comes_back_with_all_its_facts(self) -> None:
        store = make_store()
        drive = Drive(
            serial="ZX900AA1",
            brand="Acme",
            model="Rugged Pro",
            capacity_tb=4.0,
            format="exFAT",
            form_factor=FormFactor.PORTABLE,
            owner_studio_id="studio-page-1",
            purchase_date=date(2024, 3, 1),
            warranty="3 years",
            remarks="Spare cable in box",
        )
        store.add_drive(drive)

        found = store.get_drive("ZX900AA1")

        assert found is not None
        self.assertEqual(found.serial, "ZX900AA1")
        self.assertEqual(found.brand, "Acme")
        self.assertEqual(found.model, "Rugged Pro")
        self.assertEqual(found.capacity_tb, 4.0)
        self.assertEqual(found.format, "exFAT")
        self.assertIs(found.form_factor, FormFactor.PORTABLE)
        self.assertEqual(found.owner_studio_id, "studio-page-1")
        self.assertFalse(found.owned_by_t6)
        self.assertEqual(found.purchase_date, date(2024, 3, 1))
        self.assertEqual(found.warranty, "3 years")
        self.assertEqual(found.remarks, "Spare cable in box")

    def test_a_drive_with_no_studio_owner_is_owned_by_t6(self) -> None:
        drive = Drive(serial="ZX900AA2")
        self.assertTrue(drive.owned_by_t6)
        self.assertIsNone(drive.owner_studio_id)

    def test_blank_optional_text_is_stored_as_none(self) -> None:
        drive = Drive(serial="ZX900AA3", brand="  ", remarks="", owner_studio_id=" ")
        self.assertIsNone(drive.brand)
        self.assertIsNone(drive.remarks)
        self.assertTrue(drive.owned_by_t6)

    def test_form_factor_is_portable_or_powered_desktop(self) -> None:
        self.assertEqual({factor.value for factor in FormFactor}, {"portable", "powered desktop"})

    def test_capacity_must_be_positive(self) -> None:
        for capacity in (0.0, -1.0):
            with self.subTest(capacity=capacity):
                with self.assertRaisesRegex(ValueError, "capacity"):
                    Drive(serial="ZX900AA4", capacity_tb=capacity)

    def test_a_drive_needs_a_serial(self) -> None:
        for serial in ("", "   ", "(no serial)"):
            with self.subTest(serial=serial):
                with self.assertRaisesRegex(ValueError, "serial"):
                    Drive(serial=serial)


class CapacityTests(unittest.TestCase):
    def test_capacity_text_parses_to_terabytes(self) -> None:
        cases = {
            "4TB, P": 4.0,
            "4TB": 4.0,
            "4 tb": 4.0,
            "500GB": 0.5,
            "500 gb": 0.5,
            "1.5TB": 1.5,
            " 2T ": 2.0,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_capacity_tb(text), expected)

    def test_unreadable_capacity_is_refused_with_the_text_in_the_message(self) -> None:
        for text in ("", "big", "TB", "4", "four TB", "4TB or 8TB"):
            with self.subTest(text=text):
                with self.assertRaisesRegex(ValueError, "capacity") as raised:
                    parse_capacity_tb(text)
                self.assertIn(repr(text), str(raised.exception))


class SerialTests(unittest.TestCase):
    def test_normalizing_trims_upper_cases_and_strips_notes(self) -> None:
        cases = {
            " abc123 (old) ": "ABC123",
            "ABC123": "ABC123",
            "abc123 [see remarks]": "ABC123",
            "abc  123": "ABC 123",
            "  ": "",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize_serial(raw), expected)

    def test_a_drive_stores_its_normalized_serial(self) -> None:
        self.assertEqual(Drive(serial=" abc123 (old) ").serial, "ABC123")

    def test_differently_written_serials_are_the_same_drive(self) -> None:
        self.assertEqual(Drive(serial=" abc123 (old) ", brand="A"), Drive(serial="ABC123", brand="B"))

    def test_a_drive_is_found_by_any_spelling_of_its_serial(self) -> None:
        store = make_store()
        store.add_drive(Drive(serial=" abc123 (old) "))
        for spelling in ("ABC123", "abc123", " Abc123 (new label) "):
            with self.subTest(spelling=spelling):
                found = store.get_drive(spelling)
                assert found is not None
                self.assertEqual(found.serial, "ABC123")

    def test_an_unknown_serial_is_not_found(self) -> None:
        store = make_store()
        store.add_drive(Drive(serial="ABC123"))
        self.assertIsNone(store.get_drive("XYZ999"))
        self.assertIsNone(store.get_drive("   "))


class DuplicateTests(unittest.TestCase):
    def test_a_second_drive_with_an_existing_serial_is_refused(self) -> None:
        store = make_store()
        store.add_drive(Drive(serial="ABC123", brand="First"))

        with self.assertRaisesRegex(DuplicateSerial, "ABC123") as raised:
            store.add_drive(Drive(serial=" abc123 (old) ", brand="Second"))
        self.assertIn("already recorded", str(raised.exception))

        found = store.get_drive("ABC123")
        assert found is not None
        self.assertEqual(found.brand, "First")

    def test_duplicate_serial_is_a_value_error(self) -> None:
        self.assertTrue(issubclass(DuplicateSerial, ValueError))


SHOW_A = ProjectRef.page("project-page-a")
SHOW_B = ProjectRef.page("project-page-b")


def assign(
    store: DriveStore,
    serial: str,
    label: str,
    project: ProjectRef,
    start: date,
    *,
    end: date | None = None,
    role: Role = Role.MASTER,
    remarks: str | None = None,
) -> None:
    store.add_assignment(
        serial,
        Assignment(label=label, project=project, role=role, start=start, end=end, remarks=remarks),
    )


class AssignmentFactsTests(unittest.TestCase):
    def test_an_assignment_holds_its_facts_and_is_open_without_an_end(self) -> None:
        assignment = Assignment(
            label=" M01 ",
            project=SHOW_A,
            role=Role.MASTER,
            start=date(2024, 5, 1),
            remarks="  first copy ",
        )
        self.assertEqual(assignment.label, "M01")
        self.assertEqual(assignment.project.page_id, "project-page-a")
        self.assertIs(assignment.role, Role.MASTER)
        self.assertEqual(assignment.start, date(2024, 5, 1))
        self.assertIsNone(assignment.end)
        self.assertTrue(assignment.is_open)
        self.assertEqual(assignment.remarks, "first copy")

    def test_an_assignment_with_an_end_is_closed(self) -> None:
        closed = Assignment(
            label="M01", project=SHOW_A, role=Role.BACKUP, start=date(2024, 5, 1), end=date(2024, 6, 1)
        )
        self.assertFalse(closed.is_open)

    def test_roles_are_master_backup_offline_swing_archive(self) -> None:
        self.assertEqual(
            {role.value for role in Role}, {"master", "backup", "offline", "swing", "archive"}
        )

    def test_a_project_can_be_unresolved_text(self) -> None:
        ref = ProjectRef.text(" Some Show S2 ")
        self.assertFalse(ref.resolved)
        self.assertEqual(ref.unresolved_text, "Some Show S2")
        self.assertIsNone(ref.page_id)
        self.assertTrue(SHOW_A.resolved)

    def test_a_page_id_never_equals_the_same_text(self) -> None:
        self.assertNotEqual(ProjectRef.page("x"), ProjectRef.text("x"))

    def test_a_project_ref_needs_exactly_one_of_page_or_text(self) -> None:
        for kwargs in ({}, {"page_id": "a", "unresolved_text": "b"}, {"page_id": "  "}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaisesRegex(ValueError, "ProjectRef"):
                    ProjectRef(**kwargs)

    def test_an_assignment_needs_a_label(self) -> None:
        with self.assertRaisesRegex(ValueError, "label"):
            Assignment(label="  ", project=SHOW_A, role=Role.SWING, start=date(2024, 1, 1))

    def test_an_assignment_cannot_end_before_it_starts(self) -> None:
        with self.assertRaisesRegex(ValueError, "before it starts"):
            Assignment(
                label="M01",
                project=SHOW_A,
                role=Role.MASTER,
                start=date(2024, 2, 1),
                end=date(2024, 1, 1),
            )


class OpenAssignmentTests(unittest.TestCase):
    def test_open_assignments_list_the_drives_out_for_a_project(self) -> None:
        store = make_store()
        for serial in ("D1", "D2", "D3"):
            store.add_drive(Drive(serial=serial))
        assign(store, "D1", "M01", SHOW_A, date(2024, 5, 2))
        assign(store, "D2", "B01", SHOW_A, date(2024, 5, 1), role=Role.BACKUP)
        assign(store, "D3", "M01", SHOW_B, date(2024, 5, 1))

        out = store.open_assignments(SHOW_A)

        self.assertEqual([(d.serial, a.label) for d, a in out], [("D2", "B01"), ("D1", "M01")])
        self.assertIs(out[0][1].role, Role.BACKUP)

    def test_closed_assignments_are_not_listed(self) -> None:
        store = make_store()
        store.add_drive(Drive(serial="D1"))
        assign(store, "D1", "M01", SHOW_A, date(2024, 5, 1), end=date(2024, 6, 1))
        self.assertEqual(store.open_assignments(SHOW_A), [])

    def test_a_project_with_no_assignments_has_no_open_ones(self) -> None:
        self.assertEqual(make_store().open_assignments(SHOW_A), [])

    def test_an_unresolved_text_project_is_queried_by_its_text(self) -> None:
        store = make_store()
        store.add_drive(Drive(serial="D1"))
        store.add_drive(Drive(serial="D2"))
        assign(store, "D1", "M01", ProjectRef.text("Some Show S2"), date(2024, 5, 1))
        assign(store, "D2", "M01", SHOW_A, date(2024, 5, 1))

        out = store.open_assignments(ProjectRef.text("Some Show S2"))
        self.assertEqual([d.serial for d, _ in out], ["D1"])
        self.assertEqual(store.open_assignments(ProjectRef.text("Other")), [])


class HistoryTests(unittest.TestCase):
    def test_history_lists_assignments_across_projects_in_date_order(self) -> None:
        store = make_store()
        store.add_drive(Drive(serial="D1"))
        assign(store, "D1", "SWING19", SHOW_B, date(2024, 9, 1), role=Role.SWING)
        assign(store, "D1", "M01", SHOW_A, date(2024, 1, 1), end=date(2024, 3, 1))

        history = store.assignment_history(" d1 ")

        self.assertEqual([a.label for a in history], ["M01", "SWING19"])
        self.assertEqual([a.project for a in history], [SHOW_A, SHOW_B])

    def test_relabelling_a_reused_drive_keeps_earlier_labels_and_projects(self) -> None:
        store = make_store()
        store.add_drive(Drive(serial="D1"))
        assign(store, "D1", "M01", SHOW_A, date(2024, 1, 1), end=date(2024, 3, 1))
        assign(store, "D1", "ARCH-7", SHOW_B, date(2024, 4, 1), role=Role.ARCHIVE)

        history = store.assignment_history("D1")

        self.assertEqual([(a.label, a.project) for a in history], [("M01", SHOW_A), ("ARCH-7", SHOW_B)])
        self.assertEqual(history[0].end, date(2024, 3, 1))
        self.assertEqual([d.serial for d, _ in store.open_assignments(SHOW_B)], ["D1"])
        self.assertEqual(store.open_assignments(SHOW_A), [])

    def test_a_drive_with_no_assignments_has_an_empty_history(self) -> None:
        store = make_store()
        store.add_drive(Drive(serial="D1"))
        self.assertEqual(store.assignment_history("D1"), [])

    def test_the_same_label_on_different_drives_in_different_projects_is_fine(self) -> None:
        store = make_store()
        store.add_drive(Drive(serial="D1"))
        store.add_drive(Drive(serial="D2"))
        assign(store, "D1", "SWING19", SHOW_A, date(2024, 1, 1), role=Role.SWING)
        assign(store, "D2", "SWING19", SHOW_B, date(2024, 1, 1), role=Role.SWING)

        self.assertEqual([d.serial for d, _ in store.open_assignments(SHOW_A)], ["D1"])
        self.assertEqual([d.serial for d, _ in store.open_assignments(SHOW_B)], ["D2"])
        self.assertEqual([a.label for a in store.assignment_history("D1")], ["SWING19"])
        self.assertEqual([a.label for a in store.assignment_history("D2")], ["SWING19"])

    def test_assigning_or_reading_history_of_an_unknown_drive_is_refused(self) -> None:
        store = make_store()
        with self.assertRaisesRegex(UnknownDrive, "NOPE"):
            assign(store, "NOPE", "M01", SHOW_A, date(2024, 1, 1))
        with self.assertRaises(UnknownDrive):
            store.assignment_history("NOPE")


if __name__ == "__main__":
    unittest.main()
