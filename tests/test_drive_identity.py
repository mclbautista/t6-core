"""Permanent Drive identity at the public store/manifest seam. Synthetic only."""
from dataclasses import asdict, replace
from datetime import date
import unittest

from t6.drives import (
    Assignment, CustodyEvent, CustodyKind, Drive, DriveStatus, DuplicateSerial,
    Party, PartyKind, Ref, shipment_manifest,
)
from t6.drives.testing import InMemoryDriveStore

PROJECT = Ref(text="Invented Show")
LOGGER = Ref(text="Invented Custodian")
PARTY = Party(PartyKind.CONTACT, Ref(text="Invented Recipient"))


class PermanentIdentityTests(unittest.TestCase):
    def test_missing_and_disputed_serials_have_the_complete_workflow(self) -> None:
        for candidate in (None, "SYNTH-DISPUTED"):
            with self.subTest(candidate=candidate):
                store = InMemoryDriveStore()
                drive = store.add_drive(Drive(description="invented striped enclosure",
                    suspected_serial=candidate, needs_review=True, review_note="inspect sticker"))
                store.add_assignment(drive.id, Assignment(label=None, project=PROJECT, role=None, start=None))
                store.append_event(drive.id, CustodyEvent(kind=CustodyKind.RECEIVED, on=None,
                    party=None, project=PROJECT, logged_by=LOGGER))
                shipment = store.release_shipment([drive.id], on=date(2024, 1, 2),
                    party=PARTY, project=PROJECT, logged_by=LOGGER)
                line, = shipment_manifest(store, shipment)
                self.assertEqual((line.drive_id, line.serial, line.suspected_serial),
                                 (drive.id, None, candidate))
                self.assertEqual(line.review_note, "inspect sticker")
                for kind in (CustodyKind.RETURNED, CustodyKind.RETIRED):
                    store.append_event(drive.id, CustodyEvent(kind=kind, on=None, party=None,
                        project=PROJECT, logged_by=LOGGER, reason="invented retirement"))
                self.assertEqual([event.kind for event in store.list_events(drive.id)],
                    [CustodyKind.RECEIVED, CustodyKind.RELEASED, CustodyKind.RETURNED, CustodyKind.RETIRED])
                self.assertIs(store.status(drive.id), DriveStatus.RETIRED)

    def test_identity_exists_before_storage_and_blank_records_fail(self) -> None:
        drive = Drive(description="blue enclosure with a triangle")
        self.assertTrue(drive.id.startswith("t6d_"))
        self.assertEqual(drive.key, drive.id)
        self.assertNotEqual(drive, Drive(description=drive.description))
        self.assertEqual(drive, replace(drive, serial="SYNTH-1"))
        for fields in ({}, {"label": "  ", "remarks": " "}, {"needs_review": True}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                Drive(**fields)

    def test_serial_edits_preserve_workflows_and_failed_collision_is_atomic(self) -> None:
        store = InMemoryDriveStore()
        first = store.add_drive(Drive(description="triangle", suspected_serial="syntH-1", needs_review=True))
        other = store.add_drive(Drive(serial="SYNTH-1"))
        for drive in (first, other):
            store.add_assignment(drive.id, Assignment(label="M1", project=PROJECT, role=None, start=None))
            store.append_event(drive.id, CustodyEvent(kind=CustodyKind.RECEIVED, on=None, party=None, project=PROJECT, logged_by=LOGGER))
        shipment = store.release_shipment([first.id, other.id], on=date(2024, 1, 2), party=PARTY, project=PROJECT, logged_by=LOGGER)
        history = store.list_events(first.id)
        assignments = store.assignment_history(first.id)
        def snapshot() -> object:
            records = [store.get_drive(d.id) for d in (first, other)]
            return [(asdict(d), store.assignment_history(d.id),
                     store.list_events(d.id), store.status(d.id)) for d in records if d is not None], [asdict(line) for line in shipment_manifest(store, shipment)]
        before = snapshot()
        with self.assertRaises(DuplicateSerial):
            store.promote_suspected_serial(first.id)
        self.assertEqual(snapshot(), before)
        self.assertEqual(store.get_drive(first.id).suspected_serial, "SYNTH-1")  # type: ignore[union-attr]
        self.assertIsNone(store.find_by_serial("not accepted"))
        store.set_serial(other.id, "SYNTH-2")
        promoted = store.promote_suspected_serial(first.id)
        self.assertEqual(promoted.id, first.id)
        self.assertIsNone(promoted.suspected_serial)
        self.assertEqual(store.find_by_serial(" synth-1 [note] "), promoted)
        before = snapshot()
        with self.assertRaises(DuplicateSerial):
            store.set_serial(first.id, "SYNTH-2")
        self.assertEqual(snapshot(), before)
        store.set_serial(first.id, None)
        self.assertIsNone(store.find_by_serial("SYNTH-1"))
        store.set_suspected_serial(first.id, "SYNTH-2")
        store.set_review(first.id, needs_review=True, note="check scratched sticker")
        self.assertEqual(store.list_events(first.id), history)
        self.assertEqual(store.assignment_history(first.id), assignments)
        self.assertIs(store.status(first.id), DriveStatus.RELEASED)
        lines = shipment_manifest(store, shipment)
        self.assertEqual([line.drive_id for line in lines], [first.id, other.id])
        self.assertIsNone(lines[0].serial)
        self.assertEqual(lines[0].suspected_serial, "SYNTH-2")
        self.assertEqual(lines[0].description, "triangle")
        for kind in (CustodyKind.RETURNED, CustodyKind.RETIRED):
            store.append_event(first.id, CustodyEvent(kind=kind, on=None, party=None, project=PROJECT, logged_by=LOGGER, reason="synthetic retirement"))
        self.assertIs(store.status(first.id), DriveStatus.RETIRED)
        self.assertIsNone(store.get_drive("SYNTH-2"))
        self.assertIsNone(store.get_drive(first.id.upper()))

    def test_suspected_values_do_not_claim_accepted_serials(self) -> None:
        store = InMemoryDriveStore()
        a = store.add_drive(Drive(suspected_serial="shared"))
        b = store.add_drive(Drive(suspected_serial="shared"))
        self.assertIsNone(store.find_by_serial("shared"))
        store.promote_suspected_serial(a.id)
        with self.assertRaises(DuplicateSerial):
            store.promote_suspected_serial(b.id)
        self.assertEqual(store.get_drive(b.id), b)
