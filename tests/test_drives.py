"""Exercise t6.drives through the DriveStore interface and its in-memory fake.

Serials, brands and studio IDs here are made up.
"""

from datetime import date
from typing import Any
import unittest

from t6.drives import (
    Assignment,
    CustodyEvent,
    CustodyKind,
    Drive,
    DriveStatus,
    DriveStore,
    DuplicateSerial,
    FormFactor,
    ManifestLine,
    Party,
    PartyKind,
    Ref,
    Role,
    UnknownDrive,
    derive_status,
    normalize_serial,
    parse_capacity_tb,
    shipment_manifest,
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


SHOW_A = Ref(page_id="project-page-a")
SHOW_B = Ref(page_id="project-page-b")


def assign(
    store: DriveStore,
    serial: str,
    label: str,
    project: Ref,
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
        assign(store, "D1", "M01", Ref(text="Some Show S2"), date(2024, 5, 1))
        assign(store, "D2", "M01", SHOW_A, date(2024, 5, 1))

        out = store.open_assignments(Ref(text="Some Show S2"))
        self.assertEqual([d.serial for d, _ in out], ["D1"])
        self.assertEqual(store.open_assignments(Ref(text="Other")), [])


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
        assert back.party is not None
        self.assertTrue(back.party.ref.unresolved)
        self.assertEqual(back.party.ref.text, "Some Studio")

    def test_a_ref_is_exactly_one_of_page_id_or_text(self) -> None:
        for kwargs in ({}, {"page_id": " "}, {"page_id": "p1", "text": "x"}):
            with self.assertRaises(ValueError):
                Ref(**kwargs)


CLIENT = Party(PartyKind.CONTACT, ref("contact-client"))
CUSTODIAN = ref("contact-custodian")


def release(store: DriveStore, *serials: str, day: int = 10) -> str:
    return store.release_shipment(
        serials, on=date(2026, 5, day), party=CLIENT, project=SHOW_A, logged_by=CUSTODIAN
    )


def store_with_drives(*serials: str) -> DriveStore:
    store = make_store()
    for serial in serials:
        store.add_drive(Drive(serial=serial))
    return store


class ShipmentTests(unittest.TestCase):
    def test_drives_released_together_share_one_shipment_id(self) -> None:
        store = store_with_drives("D1", "D2", "D3")

        shipment_id = release(store, "D1", "d2 (spare)")

        for serial in ("D1", "D2"):
            (logged,) = store.list_events(serial)
            self.assertIs(logged.kind, CustodyKind.RELEASED)
            self.assertEqual(logged.shipment_id, shipment_id)
            self.assertEqual(logged.on, date(2026, 5, 10))
            self.assertEqual(logged.party, CLIENT)
            self.assertEqual(logged.project, SHOW_A)
            self.assertEqual(logged.logged_by, CUSTODIAN)
            self.assertIs(store.status(serial), DriveStatus.RELEASED)
        self.assertEqual(store.list_events("D3"), ())

    def test_each_shipment_gets_its_own_id(self) -> None:
        store = store_with_drives("D1", "D2")
        self.assertNotEqual(release(store, "D1"), release(store, "D2"))

    def test_the_store_lists_the_events_of_a_shipment_with_their_drives(self) -> None:
        store = store_with_drives("D1", "D2", "D3")
        store.append_event("D1", event(CustodyKind.RECEIVED, 1))
        shipment_id = release(store, "D2", "D1")
        release(store, "D3")

        listed = store.shipment_events(shipment_id)

        self.assertEqual([drive.serial for drive, _ in listed], ["D2", "D1"])
        self.assertTrue(all(logged.shipment_id == shipment_id for _, logged in listed))

    def test_an_unknown_shipment_has_no_events(self) -> None:
        self.assertEqual(store_with_drives("D1").shipment_events("nope"), ())

    def test_a_shipment_with_an_unrecorded_drive_releases_nothing(self) -> None:
        store = store_with_drives("D1")
        with self.assertRaisesRegex(UnknownDrive, "NOPE"):
            release(store, "D1", "NOPE")
        self.assertEqual(store.list_events("D1"), ())

    def test_a_shipment_needs_a_drive_and_lists_a_repeated_one_once(self) -> None:
        store = store_with_drives("D1")
        with self.assertRaisesRegex(ValueError, "at least one"):
            release(store)
        shipment_id = release(store, "D1", "d1")
        self.assertEqual(len(store.shipment_events(shipment_id)), 1)

    def test_separately_appended_releases_can_share_a_shipment_id(self) -> None:
        store = store_with_drives("D1", "D2")
        for serial in ("D1", "D2"):
            store.append_event(serial, event(CustodyKind.RELEASED, 4, shipment_id=" batch-7 "))
        self.assertEqual([d.serial for d, _ in store.shipment_events("batch-7")], ["D1", "D2"])

    def test_only_a_released_event_can_carry_a_shipment_id(self) -> None:
        self.assertIsNone(event(CustodyKind.RELEASED, 1).shipment_id)
        self.assertIsNone(event(CustodyKind.RELEASED, 1, shipment_id="  ").shipment_id)
        for kind in (CustodyKind.RECEIVED, CustodyKind.RETURNED, CustodyKind.RETIRED):
            with self.assertRaisesRegex(ValueError, "shipment"):
                event(kind, 1, reason="dead", shipment_id="batch-7")


class ManifestTests(unittest.TestCase):
    def test_a_manifest_lists_each_drive_with_recipient_date_and_logger(self) -> None:
        store = make_store()
        store.add_drive(Drive(serial="D1", capacity_tb=4.0))
        store.add_drive(Drive(serial="D2", capacity_tb=0.5))
        assign(store, "D1", "M01", SHOW_A, date(2026, 5, 1))
        assign(store, "D2", "B01", SHOW_A, date(2026, 5, 1), role=Role.BACKUP)
        shipment_id = release(store, "D1", "D2")

        manifest = shipment_manifest(store, shipment_id)

        self.assertEqual(
            manifest,
            (
                ManifestLine(
                    serial="D1",
                    label="M01",
                    capacity_tb=4.0,
                    recipient=CLIENT,
                    on=date(2026, 5, 10),
                    logged_by=CUSTODIAN,
                ),
                ManifestLine(
                    serial="D2",
                    label="B01",
                    capacity_tb=0.5,
                    recipient=CLIENT,
                    on=date(2026, 5, 10),
                    logged_by=CUSTODIAN,
                ),
            ),
        )

    def test_the_label_is_the_one_the_drive_carried_on_the_release_date(self) -> None:
        store = store_with_drives("D1")
        assign(store, "D1", "OLD01", SHOW_B, date(2026, 1, 1), end=date(2026, 4, 30))
        assign(store, "D1", "M01", SHOW_A, date(2026, 5, 1))
        shipment_id = release(store, "D1")
        # relabelled for another show after the shipment went out
        assign(store, "D1", "NEW01", SHOW_B, date(2026, 6, 1))

        (line,) = shipment_manifest(store, shipment_id)

        self.assertEqual(line.label, "M01")

    def test_a_drive_with_no_assignment_on_the_date_has_no_label(self) -> None:
        store = store_with_drives("D1")
        assign(store, "D1", "OLD01", SHOW_B, date(2026, 1, 1), end=date(2026, 4, 30))
        (line,) = shipment_manifest(store, release(store, "D1"))
        self.assertIsNone(line.label)
        self.assertIsNone(line.capacity_tb)

    def test_an_unknown_shipment_has_an_empty_manifest(self) -> None:
        self.assertEqual(shipment_manifest(make_store(), "nope"), ())


class ProvisionalDriveTests(unittest.TestCase):
    def test_a_drive_added_with_no_serial_is_flagged_no_serial(self) -> None:
        store = make_store()

        recorded = store.add_drive(Drive(serial=None, brand="Acme", capacity_tb=2.0))

        self.assertTrue(recorded.no_serial)
        self.assertIsNone(recorded.serial)
        assert recorded.provisional_id is not None
        self.assertEqual(store.get_drive(recorded.provisional_id), recorded)
        self.assertEqual(recorded.brand, "Acme")
        self.assertFalse(store.add_drive(Drive(serial="D1")).no_serial)

    def test_each_provisional_drive_is_its_own_drive(self) -> None:
        store = make_store()
        first = store.add_drive(Drive(serial=None))
        second = store.add_drive(Drive(serial=None))
        self.assertNotEqual(first.key, second.key)
        self.assertNotEqual(first, second)

    def test_a_blank_serial_text_is_still_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "serial=None"):
            Drive(serial=" (sticker torn) ")

    def test_a_provisional_drive_takes_assignments_and_custody_events(self) -> None:
        store = make_store()
        key = store.add_drive(Drive(serial=None)).key
        assign(store, key, "M01", SHOW_A, date(2026, 5, 1))
        store.append_event(key, event(CustodyKind.RECEIVED, 1))

        self.assertEqual([a.label for a in store.assignment_history(key)], ["M01"])
        self.assertIs(store.status(key), DriveStatus.IN_CUSTODY)
        ((drive, _),) = store.open_assignments(SHOW_A)
        self.assertTrue(drive.no_serial)
        self.assertEqual(drive.key, key)

    def test_adding_the_serial_makes_a_normal_drive_and_keeps_its_history(self) -> None:
        store = make_store()
        key = store.add_drive(Drive(serial=None, brand="Acme")).key
        assign(store, key, "M01", SHOW_A, date(2026, 5, 1))
        received = event(CustodyKind.RECEIVED, 1)
        store.append_event(key, received)
        shipment_id = release(store, key)

        drive = store.set_serial(key, " zx900bb7 (read under the sticker) ")

        self.assertFalse(drive.no_serial)
        self.assertEqual(drive.serial, "ZX900BB7")
        self.assertIsNone(drive.provisional_id)
        self.assertEqual(drive.brand, "Acme")
        self.assertEqual(store.get_drive("ZX900BB7"), drive)
        self.assertIsNone(store.get_drive(key))
        self.assertEqual([a.label for a in store.assignment_history("ZX900BB7")], ["M01"])
        events = store.list_events("ZX900BB7")
        self.assertEqual(events[0], received)
        self.assertEqual(len(events), 2)
        self.assertIs(store.status("ZX900BB7"), DriveStatus.RELEASED)
        (line,) = shipment_manifest(store, shipment_id)
        self.assertEqual((line.serial, line.label), ("ZX900BB7", "M01"))

    def test_a_serial_another_drive_has_is_refused_with_a_clear_message(self) -> None:
        store = make_store()
        store.add_drive(Drive(serial="ABC123"))
        key = store.add_drive(Drive(serial=None)).key
        assign(store, key, "M01", SHOW_A, date(2026, 5, 1))

        with self.assertRaisesRegex(DuplicateSerial, "'ABC123' is already recorded"):
            store.set_serial(key, "abc123")

        still = store.get_drive(key)
        assert still is not None
        self.assertTrue(still.no_serial)
        self.assertEqual(len(store.assignment_history(key)), 1)

    def test_only_a_recorded_provisional_drive_can_be_given_a_serial(self) -> None:
        store = make_store()
        store.add_drive(Drive(serial="ABC123"))
        key = store.add_drive(Drive(serial=None)).key
        with self.assertRaises(UnknownDrive):
            store.set_serial("NOPE", "NEW1")
        with self.assertRaisesRegex(ValueError, "already has a serial"):
            store.set_serial("ABC123", "NEW1")
        with self.assertRaises(ValueError):
            store.set_serial(key, "  ")

    def test_a_provisional_drive_on_a_manifest_has_no_serial(self) -> None:
        store = make_store()
        key = store.add_drive(Drive(serial=None)).key
        (line,) = shipment_manifest(store, release(store, key))
        self.assertIsNone(line.serial)


class NotRecordedTests(unittest.TestCase):
    """Facts the old workbooks never recorded are None, not a guess."""

    def test_an_assignment_can_have_no_role_and_no_start(self) -> None:
        store = store_with_drives("D1")
        store.add_assignment(
            "D1", Assignment(label="EDIT", project=SHOW_A, role=None, start=None, end=date(2026, 1, 1))
        )
        assign(store, "D1", "M01", SHOW_A, date(2020, 1, 1))

        self.assertEqual([a.label for a in store.assignment_history("D1")], ["EDIT", "M01"])
        self.assertIsNone(store.assignment_history("D1")[0].role)

    def test_an_assignment_with_no_start_counts_as_started_for_a_manifest(self) -> None:
        store = store_with_drives("D1")
        store.add_assignment("D1", Assignment(label="EDIT", project=SHOW_A, role=None, start=None))
        (line,) = shipment_manifest(store, release(store, "D1"))
        self.assertEqual(line.label, "EDIT")
        self.assertEqual([a.label for _, a in store.open_assignments(SHOW_A)], ["EDIT"])

    def test_an_event_can_have_no_party(self) -> None:
        store = store_with_drives("D1")
        store.append_event("D1", event(CustodyKind.RECEIVED, 1, party=None))
        self.assertIsNone(store.list_events("D1")[0].party)
        self.assertIs(store.status("D1"), DriveStatus.IN_CUSTODY)
