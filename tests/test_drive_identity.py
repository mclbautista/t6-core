"""Permanent Drive identity at the public store/manifest seam. Synthetic only."""
from collections.abc import Iterator
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


class InterruptedEvents(list[CustodyEvent]):
    def __iter__(self) -> Iterator[CustodyEvent]:
        yield self[0]
        raise OSError("synthetic event read failure")


class PermanentIdentityTests(unittest.TestCase):
    def test_failed_import_leaves_no_changes_and_can_be_retried(self) -> None:
        for existing in (False, True):
            with self.subTest(existing=existing):
                store = InMemoryDriveStore()
                prior = Drive(serial="SYNTH-PRIOR", description="invented enclosure",
                              needs_review=False, review_note="prior note")
                old_assignment = Assignment(label="OLD", project=PROJECT, role=None, start=None)
                old_event = CustodyEvent(kind=CustodyKind.RELEASED, on=None, party=PARTY,
                    project=PROJECT, logged_by=LOGGER, shipment_id="synthetic-prior-shipment")
                store.import_row("prior-row", prior, old_assignment, [old_event])
                incoming = Drive(serial=prior.serial if existing else "SYNTH-NEW",
                    description="incoming enclosure", needs_review=True, review_note="incoming note")
                assignment = replace(old_assignment, label="NEW")
                events = [replace(old_event, shipment_id="synthetic-new-shipment"),
                          replace(old_event, shipment_id="synthetic-new-shipment", reason="second release")]

                def snapshot() -> object:
                    records = []
                    for drive in (prior, incoming):
                        held = store.get_drive(drive.id)
                        records.append(None if held is None else (
                            asdict(held), store.assignment_history(held.id),
                            store.list_events(held.id), store.status(held.id)))
                    shipments = [[(asdict(drive), asdict(event))
                        for drive, event in store.shipment_events(shipment)]
                        for shipment in ("synthetic-prior-shipment", "synthetic-new-shipment")]
                    receipts = [store.imported_drive(key) for key in ("prior-row", "new-row")]
                    lookups = [store.find_by_serial(serial) for serial in ("SYNTH-PRIOR", "SYNTH-NEW")]
                    return records, shipments, receipts, [asdict(d) if d else None for d in lookups]

                before = snapshot()
                with self.assertRaisesRegex(OSError, "synthetic event read failure"):
                    store.import_row("new-row", incoming, assignment, InterruptedEvents(events))
                self.assertEqual(snapshot(), before)

                store.import_row("new-row", incoming, assignment, events)
                target_id = prior.id if existing else incoming.id
                self.assertEqual(store.imported_drive("new-row"), target_id)
                self.assertEqual(store.assignment_history(target_id),
                                 [old_assignment, assignment] if existing else [assignment])
                self.assertEqual(store.list_events(target_id),
                                 (old_event, *events) if existing else tuple(events))
                self.assertEqual([(drive.id, event) for drive, event in
                    store.shipment_events("synthetic-new-shipment")],
                    [(target_id, event) for event in events])
                self.assertEqual(store.imported_drive("prior-row"), prior.id)
                self.assertEqual(store.list_events(prior.id),
                                 (old_event, *events) if existing else (old_event,))
                after = snapshot()
                store.import_row("new-row", incoming, assignment, InterruptedEvents(events))
                self.assertEqual(snapshot(), after)

    def test_matching_import_keeps_unflagged_notes_and_prior_review_state_on_rerun(self) -> None:
        for needs_review in (False, True):
            with self.subTest(needs_review=needs_review):
                store = InMemoryDriveStore()
                prior = store.add_drive(Drive(serial="SYNTH-NOTES", description="original enclosure",
                    needs_review=needs_review, review_note="prior note"))
                incoming = Drive(serial=" synth-notes [import] ", description="incoming enclosure",
                    needs_review=False, review_note="incoming note")
                assignment = Assignment(label="NEW", project=PROJECT, role=None, start=None)
                store.import_row("notes-row", incoming, assignment, [])
                expected = replace(prior, review_note="prior note; incoming note")
                held = store.get_drive(prior.id)
                assert held is not None
                self.assertEqual(asdict(held), asdict(expected))
                self.assertIsNone(store.get_drive(incoming.id))
                self.assertEqual(store.imported_drive("notes-row"), prior.id)
                store.import_row("notes-row", incoming, assignment, [])
                held = store.get_drive(prior.id)
                assert held is not None
                self.assertEqual(asdict(held), asdict(expected))
                self.assertEqual(store.assignment_history(prior.id), [assignment])
                self.assertEqual(store.list_events(prior.id), ())
                self.assertEqual(store.imported_drive("notes-row"), prior.id)

    def test_manifest_uses_physical_label_when_assignment_label_is_missing(self) -> None:
        store = InMemoryDriveStore()
        drive = store.add_drive(Drive(label="invented enclosure label"))
        store.add_assignment(drive.id, Assignment(label=None, project=PROJECT, role=None, start=None))
        shipment = store.release_shipment([drive.id], on=date(2024, 1, 2), party=PARTY, project=PROJECT, logged_by=LOGGER)
        line, = shipment_manifest(store, shipment)
        self.assertEqual(line.label, "invented enclosure label")

    def test_removing_the_sole_identifying_fact_fails_atomically_until_another_is_recorded(self) -> None:
        store = InMemoryDriveStore()
        drive = store.add_drive(Drive(serial="SYNTH-SOLE"))
        store.add_assignment(drive.id, Assignment(label="assignment is not a physical fact", project=PROJECT, role=None, start=None))
        shipment = store.release_shipment([drive.id], on=date(2024, 1, 2), party=PARTY, project=PROJECT, logged_by=LOGGER)
        def snapshot() -> object:
            held = store.get_drive(drive.id)
            assert held is not None
            return (asdict(held), store.assignment_history(drive.id), store.list_events(drive.id),
                    store.status(drive.id), [asdict(line) for line in shipment_manifest(store, shipment)])
        before = snapshot()
        with self.assertRaises(ValueError):
            store.set_serial(drive.id, None)
        self.assertEqual(snapshot(), before)
        self.assertEqual(store.find_by_serial("SYNTH-SOLE"), drive)
        self.assertIsNone(store.get_drive(drive.id).suspected_serial)  # type: ignore[union-attr]
        # A person explicitly records a different uncertain candidate; removal
        # must not fabricate a label or automatically retain the old serial.
        store.set_suspected_serial(drive.id, "SYNTH-EXPLICIT-CANDIDATE")
        history, assignments, status = store.list_events(drive.id), store.assignment_history(drive.id), store.status(drive.id)
        updated = store.set_serial(drive.id, None)
        self.assertEqual((updated.id, updated.serial, updated.suspected_serial),
                         (drive.id, None, "SYNTH-EXPLICIT-CANDIDATE"))
        self.assertIsNone(store.find_by_serial("SYNTH-SOLE"))
        self.assertEqual((store.list_events(drive.id), store.assignment_history(drive.id), store.status(drive.id)),
                         (history, assignments, status))
        self.assertEqual([line.drive_id for line in shipment_manifest(store, shipment)], [drive.id])

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
