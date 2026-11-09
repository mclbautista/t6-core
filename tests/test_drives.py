"""Exercise t6.drives through the DriveStore interface and its in-memory fake.

Serials, brands and studio IDs here are made up.
"""

from datetime import date
import unittest

from t6.drives import (
    Drive,
    DriveStore,
    DuplicateSerial,
    FormFactor,
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


if __name__ == "__main__":
    unittest.main()
