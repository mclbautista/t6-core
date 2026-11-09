"""Exercise t6.drives through the DriveStore interface and its in-memory fake.

Serials, brands and studio IDs here are made up.
"""

from datetime import date
from typing import Any
import unittest

from t6.drives import (
    CustodyEvent,
    CustodyKind,
    Drive,
    DriveStatus,
    DriveStore,
    DuplicateSerial,
    FormFactor,
    Party,
    PartyKind,
    Ref,
    UnknownDrive,
    derive_status,
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


def ref(page_id: str) -> Ref:
    return Ref(page_id=page_id)


def event(kind: CustodyKind, day: int, reason: str | None = None, **over: Any) -> CustodyEvent:
    fields: dict[str, Any] = dict(
        kind=kind,
        on=date(2026, 5, day),
        party=Party(PartyKind.STUDIO, ref("studio-1")),
        project=ref("project-1"),
        logged_by=ref("contact-1"),
        reason=reason,
    )
    fields.update(over)
    return CustodyEvent(**fields)


def store_with_drive() -> DriveStore:
    store = make_store()
    store.add_drive(Drive(serial="ZX900AA1"))
    return store


class CustodyLogTests(unittest.TestCase):
    def test_an_event_keeps_all_its_facts(self) -> None:
        store = store_with_drive()
        logged = event(CustodyKind.RELEASED, 3, reason="for grading")
        store.append_event("ZX900AA1", logged)
        (back,) = store.list_events("ZX900AA1")
        self.assertEqual(back, logged)
        self.assertEqual(back.kind, CustodyKind.RELEASED)
        self.assertEqual(back.on, date(2026, 5, 3))
        self.assertEqual(back.party, Party(PartyKind.STUDIO, ref("studio-1")))
        self.assertEqual(back.project, ref("project-1"))
        self.assertEqual(back.logged_by, ref("contact-1"))
        self.assertEqual(back.reason, "for grading")

    def test_a_contact_can_be_the_party(self) -> None:
        party = Party(PartyKind.CONTACT, ref("contact-9"))
        self.assertEqual(event(CustodyKind.RECEIVED, 1, party=party).party, party)

    def test_retired_requires_a_reason(self) -> None:
        for reason in (None, "", "   "):
            with self.assertRaises(ValueError):
                event(CustodyKind.RETIRED, 1, reason=reason)
        self.assertEqual(event(CustodyKind.RETIRED, 1, reason="dead").reason, "dead")

    def test_other_kinds_do_not_need_a_reason(self) -> None:
        for kind in (CustodyKind.RECEIVED, CustodyKind.RELEASED, CustodyKind.RETURNED):
            self.assertIsNone(event(kind, 1).reason)

    def test_events_list_in_the_order_they_were_appended(self) -> None:
        store = store_with_drive()
        first = event(CustodyKind.RECEIVED, 5)
        second = event(CustodyKind.RELEASED, 2)  # an earlier date, logged later
        store.append_event("zx900aa1", first)
        store.append_event("ZX900AA1 (old)", second)
        self.assertEqual(store.list_events("ZX900AA1"), (first, second))

    def test_the_store_offers_no_way_to_edit_or_delete_an_event(self) -> None:
        store = store_with_drive()
        for name in dir(store):
            if name.startswith("_"):
                continue
            self.assertFalse(
                any(word in name for word in ("edit", "update", "delete", "remove", "clear")),
                name,
            )
        store.append_event("ZX900AA1", event(CustodyKind.RECEIVED, 1))
        listed = store.list_events("ZX900AA1")
        self.assertIsInstance(listed, tuple)
        self.assertEqual(len(store.list_events("ZX900AA1")), 1)

    def test_a_correction_is_a_new_event_and_both_stay_visible(self) -> None:
        store = store_with_drive()
        wrong = event(CustodyKind.RELEASED, 3, project=ref("project-wrong"))
        fixed = event(CustodyKind.RELEASED, 3, project=ref("project-1"))
        store.append_event("ZX900AA1", wrong)
        store.append_event("ZX900AA1", fixed)
        self.assertEqual(store.list_events("ZX900AA1"), (wrong, fixed))

    def test_a_drive_with_no_events_has_an_empty_log(self) -> None:
        self.assertEqual(store_with_drive().list_events("ZX900AA1"), ())

    def test_events_for_an_unrecorded_drive_are_refused(self) -> None:
        store = store_with_drive()
        with self.assertRaises(UnknownDrive):
            store.append_event("NOPE", event(CustodyKind.RECEIVED, 1))
        with self.assertRaises(UnknownDrive):
            store.list_events("NOPE")
        with self.assertRaises(UnknownDrive):
            store.status("NOPE")

    def test_logs_are_kept_per_drive(self) -> None:
        store = store_with_drive()
        store.add_drive(Drive(serial="OTHER1"))
        store.append_event("ZX900AA1", event(CustodyKind.RECEIVED, 1))
        self.assertEqual(store.list_events("OTHER1"), ())


class StatusTests(unittest.TestCase):
    def test_a_drive_with_no_events_has_no_status(self) -> None:
        self.assertIsNone(store_with_drive().status("ZX900AA1"))

    def test_status_follows_the_last_event(self) -> None:
        store = store_with_drive()
        expected = [
            (CustodyKind.RECEIVED, DriveStatus.IN_CUSTODY),
            (CustodyKind.RELEASED, DriveStatus.RELEASED),
            (CustodyKind.RETURNED, DriveStatus.IN_CUSTODY),
            (CustodyKind.RETIRED, DriveStatus.RETIRED),
        ]
        for day, (kind, status) in enumerate(expected, start=1):
            reason = "lost" if kind is CustodyKind.RETIRED else None
            store.append_event("ZX900AA1", event(kind, day, reason=reason))
            self.assertEqual(store.status("ZX900AA1"), status)

    def test_a_correction_changes_the_status_whatever_its_date(self) -> None:
        store = store_with_drive()
        store.append_event("ZX900AA1", event(CustodyKind.RELEASED, 20))
        store.append_event("ZX900AA1", event(CustodyKind.RETURNED, 2))
        self.assertEqual(store.status("ZX900AA1"), DriveStatus.IN_CUSTODY)

    def test_status_is_not_a_stored_field_of_the_drive(self) -> None:
        self.assertNotIn("status", Drive.__slots__)
        self.assertIsNone(derive_status(()))


class RefTests(unittest.TestCase):
    def test_a_resolved_ref_holds_a_page_id(self) -> None:
        self.assertFalse(Ref(page_id="p1").unresolved)

    def test_an_unresolved_name_is_kept_as_text_and_flagged(self) -> None:
        unresolved = Ref(text=" Some Studio ")
        self.assertTrue(unresolved.unresolved)
        self.assertEqual(unresolved.text, "Some Studio")
        stored = event(CustodyKind.RELEASED, 1, party=Party(PartyKind.STUDIO, unresolved))
        store = store_with_drive()
        store.append_event("ZX900AA1", stored)
        (back,) = store.list_events("ZX900AA1")
        self.assertTrue(back.party.ref.unresolved)
        self.assertEqual(back.party.ref.text, "Some Studio")

    def test_a_ref_is_exactly_one_of_page_id_or_text(self) -> None:
        for kwargs in ({}, {"page_id": " "}, {"page_id": "p1", "text": "x"}):
            with self.assertRaises(ValueError):
                Ref(**kwargs)
