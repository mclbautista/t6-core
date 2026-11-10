"""Synthetic-only workbook regression tests at both public import boundaries."""
from dataclasses import asdict
from pathlib import Path
import tempfile
import unittest
from typing import Any

from t6.drives import Drive, CustodyKind, Ref, shipment_manifest
from t6.drives.importer import import_log_sheet_workbook, import_per_project_workbook
from t6.drives.testing import InMemoryDriveStore
from test_drives_importer import write_workbook, make_lookup, LOGGED_BY, PAPER_MOON


class ImportIdentityTests(unittest.TestCase):
    def test_receipts_survive_serial_correction_and_capacity_alias_conflicts_are_reported(self) -> None:
        for importer in (import_per_project_workbook, import_log_sheet_workbook):
            with self.subTest(importer=importer.__name__), tempfile.TemporaryDirectory() as folder:
                path = write_workbook(folder, {"PAPER MOON": [
                    ["HARD DRIVE LABEL", "SERIAL NUMBER", "SPACE", "TOTAL DISK SPACE"],
                    ["invented accepted", "SYNTH-BEFORE", "500GB", "0.5TB"],
                    ["invented conflict", None, "2TB", "4TB"],
                ]})
                store = InMemoryDriveStore()
                result = importer(path, store, lookup=make_lookup(), logged_by=LOGGED_BY)
                self.assertTrue(any(i.row == 3 and "conflicting" in i.detail for i in result.review))
                drive = store.find_by_serial("SYNTH-BEFORE")
                assert drive is not None
                store.set_serial(drive.id, "SYNTH-AFTER")
                for serial in ("SYNTH-AFTER", None):
                    store.set_serial(drive.id, serial)
                    before = [(asdict(d), store.assignment_history(d.id), store.list_events(d.id))
                              for d, _ in store.open_assignments(PAPER_MOON)]
                    importer(path, store, lookup=make_lookup(), logged_by=LOGGED_BY)
                    self.assertEqual([(asdict(d), store.assignment_history(d.id), store.list_events(d.id))
                                      for d, _ in store.open_assignments(PAPER_MOON)], before)
                    self.assertIsNone(store.find_by_serial("SYNTH-BEFORE"))

    def test_cross_workbook_matching_preserves_additional_facts_in_review(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            header = ["HARD DRIVE LABEL", "SERIAL NUMBER", "HD DESCRIPTION", "REMARKS"]
            first = write_workbook(folder, {"PAPER MOON": [header,
                ["original", "SYNTH-CROSS", "blue case", "first observation"]]}, "invented-first.xlsx")
            second = write_workbook(folder, {"PAPER MOON": [header,
                ["changed", "SYNTH-CROSS", "red case", "second observation"],
                ["original", None, "blue case", "not identity evidence"]]}, "invented-second.xlsx")
            store = InMemoryDriveStore()
            import_per_project_workbook(first, store, lookup=make_lookup(), logged_by=LOGGED_BY)
            held = store.find_by_serial("SYNTH-CROSS")
            assert held is not None
            result = import_log_sheet_workbook(second, store, lookup=make_lookup(), logged_by=LOGGED_BY)
            matched = [item for item in result.review if item.reason == "additional drive facts"]
            self.assertEqual(len(matched), 1)
            self.assertEqual((matched[0].workbook, matched[0].sheet, matched[0].row),
                             (str(second.resolve()), "PAPER MOON", 2))
            self.assertIn("red case", matched[0].detail)
            self.assertIn("second observation", matched[0].detail)
            self.assertEqual(store.find_by_serial("SYNTH-CROSS").id, held.id)  # type: ignore[union-attr]
            self.assertEqual(len(store.open_assignments(PAPER_MOON)), 3)
            self.assertEqual(import_log_sheet_workbook(second, store, lookup=make_lookup(), logged_by=LOGGED_BY), result)

    def test_uncertain_inventory_is_preserved_and_reruns_keep_identity(self) -> None:
        for importer in (import_per_project_workbook, import_log_sheet_workbook):
            with self.subTest(importer=importer.__name__), tempfile.TemporaryDirectory() as folder:
                path = write_workbook(folder, {"PAPER MOON": [
                    ["HARD DRIVE LABEL", "SERIAL NUMBER", "SPACE", "REMARKS", "REMARKS", "T6 IN", "HD DESCRIPTION"],
                    ["same label", "SYNTH-1", "500GB", "first note", "second note", "May 2, 2024"],
                    ["same label", "synth-1", "2TB", "different physical drive", None, "YES"],
                    [None, None, "4TB", "red case", None, "T6 IN"],
                    ["candidate", "SYNTH-2 (check sticker)", "1TB", "keep this", None, None],
                    ["bad size", None, "4TB or 8TB", "do not invent capacity"],
                    [None, None, None, None, None, None, "triangular enclosure"],
                ]})
                store = InMemoryDriveStore()
                def run() -> Any:
                    return importer(path, store, lookup=make_lookup(), logged_by=LOGGED_BY)
                result = run()
                drives = [d for d, _ in store.open_assignments(PAPER_MOON)]
                self.assertEqual(len(drives), 5)
                self.assertIsNone(store.find_by_serial("SYNTH-1"))
                candidates = [d for d in drives if d.suspected_serial == "SYNTH-1"]
                self.assertEqual(len(candidates), 2)
                self.assertTrue(all(d.needs_review for d in candidates))
                self.assertNotEqual(candidates[0].id, candidates[1].id)
                first = next(d for d in drives if d.remarks and "first note" in d.remarks)
                self.assertIn("second note", first.remarks or "")
                received, = store.list_events(first.id)
                self.assertIs(received.kind, CustodyKind.RECEIVED)
                self.assertEqual(received.on.isoformat() if received.on else None, "2024-05-02")
                second = next(d for d in drives if d.remarks and "different physical drive" in d.remarks)
                undated, = store.list_events(second.id)
                self.assertIsNone(undated.on)
                self.assertIs(undated.kind, CustodyKind.RECEIVED)
                self.assertEqual(first.capacity_tb, 0.5)
                noted = next(d for d in drives if d.label == "candidate")
                self.assertEqual(noted.suspected_serial, "SYNTH-2")
                self.assertIn("check sticker", noted.review_note or "")
                self.assertTrue(all(item.workbook == str(path.resolve()) for item in result.review))
                self.assertTrue(any(item.row == 6 and item.reason == "unreadable" for item in result.review))
                before = [(asdict(d), store.assignment_history(d.id), store.list_events(d.id)) for d in drives]
                again = run()
                after = [(asdict(d), store.assignment_history(d.id), store.list_events(d.id)) for d, _ in store.open_assignments(PAPER_MOON)]
                self.assertEqual(before, after)
                self.assertEqual(again, result)
                self.assertEqual(len(result.review), len(set(result.review)))

    def test_movements_are_row_identified_not_deduplicated_by_visible_facts(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            header = ["DATE", "PROJECT NAME", "HARD DRIVE LABEL", "HD DESCRIPTION", "IN/OUT", "RECEIVED BY", "REMARKS", "REMARKS"]
            movement: list[Any] = ["May 2, 2024", "PAPER MOON", "same", "2TB", "OUT", "Invented Person", "keep note", "S/N: SYNTH-7"]
            path = write_workbook(folder, {"Movements": [header, movement, movement,
                [None, "PAPER MOON", "same", "red case", "T6 IN", None, "no serial one"],
                [None, "PAPER MOON", "same", "red case", "IN", None, "no serial two"],
                [None, "PAPER MOON", None, "2TB", "IN", None, "S/N: SYNTH-8"],
                [None, "PAPER MOON", "same", None, "OUT", None, "S/N: SYNTH-9 S/N: SYNTH-7"],
            ]}, "invented-log.xlsx")
            store = InMemoryDriveStore()
            store.add_drive(Drive(label="same", suspected_serial="SYNTH-9"))
            def run() -> Any:
                return import_log_sheet_workbook(path, store, lookup=make_lookup(), logged_by=LOGGED_BY)
            result = run()
            drive = store.find_by_serial("SYNTH-7")
            assert drive is not None
            events = store.list_events(drive.id)
            self.assertEqual(len(events), 2)
            self.assertEqual(events[0], events[1])
            self.assertIn("keep note", events[0].reason or "")
            shipment = events[0].shipment_id
            assert shipment is not None
            self.assertEqual(len(shipment_manifest(store, shipment)), 2)
            self.assertEqual({i.row for i in result.review if i.reason == "movement with no serial"}, {4, 5})
            self.assertTrue(any(i.row == 7 and i.reason == "ambiguous serial" for i in result.review))
            self.assertIsNone(store.find_by_serial("SYNTH-9"))
            self.assertIsNotNone(store.find_by_serial("SYNTH-8"))
            self.assertEqual(run(), result)
            self.assertEqual(store.list_events(drive.id), events)
            self.assertEqual(len(store.shipment_events(shipment)), 2)
