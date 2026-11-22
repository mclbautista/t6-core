"""Drive catalog snapshots and the facts that find a Drive on a shelf: office location and QR code.

These tests are written against the DriveStore interface. `test_drive_database` runs them again on the SQLite store,
so the in-memory fake and the durable store give the same answers.
"""
import unittest
from datetime import date

from t6.drives import (
    CatalogDiffEntry,
    CatalogEntry,
    CatalogOutOfOrder,
    CatalogSnapshot,
    Drive,
    DriveStore,
    DuplicateQrCode,
    UnknownDrive,
    catalog_entries,
)
from t6.drives.testing import InMemoryDriveStore


def make_store() -> DriveStore:
    return InMemoryDriveStore()


def file(path: str, size: int = 10, mtime: str = '2026-01-01T00:00:00', checksum: str | None = None) -> CatalogEntry:
    return CatalogEntry(path=path, kind='file', size_bytes=size, mtime=mtime, checksum=checksum)


def folder(path: str) -> CatalogEntry:
    return CatalogEntry(path=path, kind='dir')


DAY1, DAY2, DAY3, DAY4 = date(2026, 3, 1), date(2026, 3, 10), date(2026, 3, 20), date(2026, 4, 1)


class ShelfFactsTests(unittest.TestCase):
    def test_a_drive_keeps_its_office_location_and_qr_code(self) -> None:
        drive = Drive(label='Synthetic 1', office_location='  Office A / Shelf 1 ', qr_code=' ab-12 ')
        self.assertEqual((drive.office_location, drive.qr_code), ('Office A / Shelf 1', 'AB-12'))
        blank = Drive(label='Synthetic 2', office_location='  ', qr_code=None)
        self.assertEqual((blank.office_location, blank.qr_code), (None, None))

    def test_a_qr_code_is_a_short_url_safe_token(self) -> None:
        for bad in ('', '  ', 'a', 'has space', 'a/b', 'a?b', 'x' * 65, 'ünï'):
            with self.subTest(bad=bad):
                if bad.strip():
                    with self.assertRaises(ValueError):
                        Drive(label='Synthetic', qr_code=bad)
                else:
                    self.assertIsNone(Drive(label='Synthetic', qr_code=bad).qr_code)

    def test_both_facts_change_the_drive_version(self) -> None:
        base = Drive(label='Synthetic')
        from dataclasses import replace
        self.assertNotEqual(base.version, replace(base, office_location='Office A / Shelf 1').version)
        self.assertNotEqual(base.version, replace(base, qr_code='QR-1').version)

    def test_the_store_finds_a_drive_by_qr_code_in_any_case_and_refuses_a_second_holder(self) -> None:
        store = make_store()
        first = store.add_drive(Drive(label='Synthetic 1', qr_code='QR-1'))
        self.assertEqual(store.find_by_qr_code('qr-1'), first)
        self.assertIsNone(store.find_by_qr_code('QR-2'))
        with self.assertRaises(DuplicateQrCode):
            store.add_drive(Drive(label='Synthetic 2', qr_code='qr-1'))
        second = store.add_drive(Drive(label='Synthetic 2'))
        with self.assertRaises(DuplicateQrCode):
            store.set_facts(second.id, {'qr_code': 'QR-1'})
        self.assertEqual(store.set_facts(second.id, {'qr_code': 'QR-2', 'office_location': 'Office A / Shelf 2'}).qr_code, 'QR-2')
        self.assertEqual(store.set_facts(first.id, {'qr_code': None}).qr_code, None)
        self.assertEqual(store.set_facts(second.id, {'qr_code': 'QR-1'}).qr_code, 'QR-1')  # released by the first, free again


class CatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = make_store()
        self.drive = self.store.add_drive(Drive(label='Synthetic 1')).id

    def record(self, on: date, entries: list[CatalogEntry], root: str = 'Volume') -> tuple[CatalogSnapshot, tuple[CatalogDiffEntry, ...]]:
        return self.store.record_catalog(self.drive, on, root, entries, 1000.0)

    def paths(self, on: date) -> list[str] | None:
        found = self.store.contents_at(self.drive, on)
        return None if found is None else [entry.path for entry in found.entries]

    def test_the_first_snapshot_stores_every_entry_as_added(self) -> None:
        snapshot, diff = self.record(DAY1, [file('Show A/clip1.mov', 100), folder('Show A'), file('notes.txt', 5)])
        self.assertEqual((snapshot.drive_id, snapshot.captured_on, snapshot.parent_snapshot_id, snapshot.root_path), (self.drive, DAY1, None, 'Volume'))
        self.assertEqual((snapshot.entry_count, snapshot.total_bytes), (3, 105))
        self.assertEqual([(d.path, d.change) for d in diff], [('Show A', 'added'), ('Show A/clip1.mov', 'added'), ('notes.txt', 'added')])

    def test_a_later_snapshot_stores_only_what_changed(self) -> None:
        first, _ = self.record(DAY1, [folder('Show A'), file('Show A/clip1.mov', 100), file('Show A/clip2.mov', 200), file('notes.txt', 5)])
        second, diff = self.record(DAY2, [folder('Show A'), file('Show A/clip1.mov', 100), file('Show A/clip2.mov', 250, mtime='2026-03-05T00:00:00'),
                                          file('Show A/clip3.mov', 300)])
        self.assertEqual(second.parent_snapshot_id, first.id)
        self.assertEqual([(d.path, d.change) for d in diff], [('Show A/clip2.mov', 'modified'), ('Show A/clip3.mov', 'added'), ('notes.txt', 'removed')])
        self.assertEqual(self.store.catalog_diff(self.drive, second.id), diff)
        self.assertEqual((second.entry_count, second.total_bytes), (4, 650))
        stored = sum(len(self.store.catalog_diff(self.drive, s.id) or ()) for s in self.store.catalog_snapshots(self.drive))
        self.assertEqual(stored, 4 + 3)  # not 4 + 4: the unchanged entries are not copied

    def test_an_unchanged_scan_is_recorded_with_no_diff(self) -> None:
        self.record(DAY1, [file('a.txt')])
        snapshot, diff = self.record(DAY2, [file('a.txt')])
        self.assertEqual(diff, ())
        self.assertEqual(snapshot.entry_count, 1)

    def test_a_changed_checksum_or_kind_is_a_modification(self) -> None:
        self.record(DAY1, [file('a', checksum='x'), file('b')])
        _, diff = self.record(DAY2, [file('a', checksum='y'), folder('b')])
        self.assertEqual([(d.path, d.change, d.kind) for d in diff], [('a', 'modified', 'file'), ('b', 'modified', 'dir')])

    def test_contents_at_folds_the_diffs_up_to_the_date(self) -> None:
        self.record(DAY1, [file('a.txt', 1), file('b.txt', 2)])
        self.record(DAY2, [file('a.txt', 1), file('b.txt', 22), file('c.txt', 3)])
        self.record(DAY3, [file('b.txt', 22), file('c.txt', 3)])
        self.assertIsNone(self.paths(date(2026, 2, 28)))
        self.assertEqual(self.paths(DAY1), ['a.txt', 'b.txt'])
        self.assertEqual(self.paths(date(2026, 3, 9)), ['a.txt', 'b.txt'])  # between scans: the last one before the date
        self.assertEqual(self.paths(DAY2), ['a.txt', 'b.txt', 'c.txt'])
        self.assertEqual(self.paths(DAY3), ['b.txt', 'c.txt'])
        self.assertEqual(self.paths(DAY4), ['b.txt', 'c.txt'])
        at_day2 = self.store.contents_at(self.drive, DAY2)
        assert at_day2 is not None
        self.assertEqual({e.path: e.size_bytes for e in at_day2.entries}, {'a.txt': 1, 'b.txt': 22, 'c.txt': 3})
        self.assertEqual(at_day2.snapshot.captured_on, DAY2)
        before_third = self.store.contents_at(self.drive, date(2026, 3, 19))
        assert before_third is not None
        self.assertEqual(before_third.snapshot.captured_on, DAY2)

    def test_a_removed_file_that_comes_back_is_added_again(self) -> None:
        self.record(DAY1, [file('a.txt')])
        self.record(DAY2, [])
        _, diff = self.record(DAY3, [file('a.txt')])
        self.assertEqual([(d.path, d.change) for d in diff], [('a.txt', 'added')])
        self.assertEqual(self.paths(DAY2), [])
        self.assertEqual(self.paths(DAY3), ['a.txt'])

    def test_snapshots_list_in_date_order_with_what_each_changed(self) -> None:
        self.record(DAY1, [file('a')])
        self.record(DAY2, [file('a'), file('b')])
        listed = self.store.catalog_snapshots(self.drive)
        self.assertEqual([s.captured_on for s in listed], [DAY1, DAY2])
        self.assertEqual(listed[1].parent_snapshot_id, listed[0].id)
        self.assertIsNone(self.store.catalog_diff(self.drive, 'snap_nothere'))

    def test_snapshots_are_kept_per_drive(self) -> None:
        other = self.store.add_drive(Drive(label='Synthetic 2')).id
        self.record(DAY1, [file('a')])
        self.store.record_catalog(other, DAY1, 'Volume', [file('z')], 1000.0)
        self.assertEqual(self.paths(DAY1), ['a'])
        found = self.store.contents_at(other, DAY1)
        assert found is not None
        self.assertEqual([e.path for e in found.entries], ['z'])
        self.assertEqual(self.store.catalog_diff(other, self.store.catalog_snapshots(self.drive)[0].id), None)

    def test_a_scan_dated_on_or_before_the_latest_is_refused(self) -> None:
        self.record(DAY2, [file('a')])
        for on in (DAY1, DAY2):
            with self.assertRaises(CatalogOutOfOrder):
                self.record(on, [file('a')])
        self.assertEqual(len(self.store.catalog_snapshots(self.drive)), 1)

    def test_an_unknown_drive_is_refused(self) -> None:
        with self.assertRaises(UnknownDrive):
            self.store.record_catalog('t6d_' + '0' * 32, DAY1, 'Volume', [], 1000.0)
        with self.assertRaises(UnknownDrive):
            self.store.contents_at('t6d_' + '0' * 32, DAY1)
        with self.assertRaises(UnknownDrive):
            self.store.catalog_snapshots('t6d_' + '0' * 32)


class CatalogEntryTests(unittest.TestCase):
    def test_a_path_is_relative_with_forward_slashes(self) -> None:
        self.assertEqual(CatalogEntry(path='Show A\\clip.mov', kind='file', size_bytes=1).path, 'Show A/clip.mov')
        self.assertEqual(CatalogEntry(path=' Show A/ ', kind='dir').path, 'Show A')
        for bad in ('', ' ', '/abs', '../up', 'a/../b', 'a//b', 'a\x00b', '.'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                CatalogEntry(path=bad, kind='dir')

    def test_a_file_has_a_size_and_a_folder_does_not(self) -> None:
        with self.assertRaises(ValueError):
            CatalogEntry(path='a', kind='file')
        with self.assertRaises(ValueError):
            CatalogEntry(path='a', kind='file', size_bytes=-1)
        with self.assertRaises(ValueError):
            CatalogEntry(path='a', kind='dir', size_bytes=1)
        with self.assertRaises(ValueError):
            CatalogEntry(path='a', kind='link', size_bytes=1)  # type: ignore[arg-type]

    def test_a_manifest_refuses_a_path_listed_twice(self) -> None:
        with self.assertRaises(ValueError):
            catalog_entries([{'path': 'a', 'kind': 'dir'}, {'path': 'a', 'kind': 'dir'}])
        entries = catalog_entries([{'path': 'b', 'kind': 'file', 'size_bytes': 3}, {'path': 'a', 'kind': 'dir'}])
        self.assertEqual([e.path for e in entries], ['a', 'b'])  # in path order


if __name__ == '__main__':
    unittest.main()
