"""Reload through the verb API; all workbook evidence is synthetic and temporary."""
import os
import tempfile
import unittest
from pathlib import Path
from datetime import date
from typing import Any
from unittest.mock import patch

from openpyxl import Workbook

from t6.person import People
from t6.preview import PreviewTokens
from t6.registry import SqliteProjectRegistry
from t6.verbs import VERBS, Done, Preview, invoke


HEADER = ['HARD DRIVE LABEL', 'SERIAL NUMBER', 'BRAND NAME', 'TOTAL DISK SPACE',
          'FORMAT', 'DATE RECEIVED', 'DATE RELEASED', 'REMARKS']


class DriveReloadTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.path = self.folder / 'projects.xlsx'
        self.registry = SqliteProjectRegistry(self.folder / 't6.sqlite')
        self.people = People(self.registry, PreviewTokens('test'))
        with self.registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES ('synthetic-keeper', 'staff', '[]', '[]', NULL, 0, 1)")
            db.execute("INSERT INTO person_permissions VALUES ('synthetic-keeper', 'drives')")
        env = patch.dict(os.environ, {'T6_PREVIEW_SIGNING_KEY': 'test'})
        env.start()
        self.addCleanup(env.stop)

    def workbook(self, tabs: dict[str, list[list[Any]]]) -> None:
        book = Workbook()
        book.remove(book.active)  # type: ignore[arg-type]
        for name, rows in tabs.items():
            sheet = book.create_sheet(name)
            for row in rows:
                sheet.append(row)
        book.save(self.path)
        book.close()

    def call(self, verb: str, **values: Any) -> Any:
        entries = [v for v in VERBS if (v.noun, v.verb) == ('drive', verb)]
        self.assertEqual(len(entries), 1, f'drive {verb} must be registered')
        entry = entries[0]
        return invoke(entry, entry.input_model.model_validate(values), actor='synthetic-keeper',
                      people=self.people, open_notion=lambda _: None)

    def reload(self, key: str = 'reload-one') -> dict[str, Any]:
        values = {'workbook': str(self.path), 'idempotency_key': key}
        preview = self.call('reload', **values)
        self.assertIsInstance(preview, Preview)
        done = self.call('reload', **values, confirm=preview.token)
        self.assertIsInstance(done, Done)
        return dict(done.result)

    def drives(self) -> list[dict[str, Any]]:
        return list(self.call('list').result['drives'])

    def test_serial_on_three_tabs_loads_one_drive_with_three_text_assignments(self) -> None:
        self.workbook({name: [['PROJECT NAME: Ignored heading'], ['TOTAL', 1], ['IN-HOUSE', 1],
                              ['RELEASED', 0], HEADER, ['Synthetic MASTER', serial, 'Synthetic brand', '2TB', 'exFAT']]
                       for name, serial in [('SYNTHETIC ALPHA', ' sample-001 '),
                                            ('SYNTHETIC BETA', 'SAMPLE-001'), ('SYNTHETIC GAMMA', 'sample-001')]})
        self.reload()
        drives = self.drives()
        self.assertEqual(len(drives), 1)
        drive = drives[0]
        self.assertTrue(drive['id'].startswith('t6d_'))
        self.assertEqual((drive['serial'], drive['label'], drive['brand'], drive['capacity_tb'], drive['format']),
                         ('SAMPLE-001', 'Synthetic MASTER', 'Synthetic brand', 2.0, 'exFAT'))
        detail = self.call('get', drive=drive['id']).result
        self.assertEqual([a['project'] for a in detail['assignments']],
                         [{'page_id': None, 'text': name} for name in ('SYNTHETIC ALPHA', 'SYNTHETIC BETA', 'SYNTHETIC GAMMA')])
        self.assertEqual(self.call('exceptions').result['exceptions'], [])

    def test_release_retirement_and_undated_receipt_derive_status_in_date_order(self) -> None:
        self.workbook({
            'SYNTHETIC LATER': [HEADER, ['Synthetic travelling', 'SAMPLE-OUT', None, None, None,
                                        date(2026, 2, 1), date(2026, 3, 1), 'Synthetic recipient'],
                               ['Synthetic failed', 'SAMPLE-DEAD', None, None, None,
                                date(2026, 6, 1), date(2026, 7, 1), 'DEAD DRIVE']],
            'DEAD DRIVES': [HEADER, ['Synthetic dead tab', 'SAMPLE-TAB']],
            'SYNTHETIC EARLIER': [HEADER, ['Synthetic earlier', 'SAMPLE-OUT', None, None, None, date(2026, 1, 1)],
                                 ['Synthetic undated', 'SAMPLE-UNDATED'],
                                 ['Synthetic unknown', None]],
            'SYNTHETIC LATEST': [HEADER, ['Synthetic latest', 'SAMPLE-DEAD', None, None, None, date(2026, 8, 1)]],
        })
        self.reload()
        drives = {d['serial']: d for d in self.drives()}
        self.assertEqual({serial: d['status'] for serial, d in drives.items()},
                         {'SAMPLE-OUT': 'released', 'SAMPLE-DEAD': 'retired', 'SAMPLE-TAB': 'retired',
                          'SAMPLE-UNDATED': 'in custody', None: 'in custody'})
        released = self.call('get', drive=drives['SAMPLE-OUT']['id']).result['events']
        self.assertEqual([(e['kind'], e['on']) for e in released],
                         [('received', '2026-01-01'), ('received', '2026-02-01'), ('released', '2026-03-01')])
        self.assertEqual(released[-1]['party']['ref']['text'], 'Synthetic recipient')
        retired = self.call('get', drive=drives['SAMPLE-DEAD']['id']).result['events']
        self.assertEqual(retired[-1]['kind'], 'retired')
        self.assertEqual(self.call('get', drive=drives['SAMPLE-UNDATED']['id']).result['events'][0]['on'], None)

    def test_t6_label_is_owned_and_other_labels_stay_unknown(self) -> None:
        self.workbook({'SYNTHETIC OWNERSHIP': [HEADER, ['Synthetic t6 MASTER', 'SAMPLE-T6'],
                                              ['Synthetic external', 'SAMPLE-EXTERNAL']]})
        self.reload()
        self.assertEqual({d['serial']: d['ownership'] for d in self.drives()},
                         {'SAMPLE-T6': 't6', 'SAMPLE-EXTERNAL': 'unknown'})

    def test_reload_replaces_previous_data_and_preserves_serial_ids_even_after_absence(self) -> None:
        self.workbook({'SYNTHETIC OLD': [HEADER, ['Synthetic kept', 'SAMPLE-KEEP'],
                                        ['Synthetic removed', 'SAMPLE-REMOVE'], ['Synthetic no serial', None],
                                        [None, None, None, 'unreadable capacity']]})
        self.reload()
        original = {d['serial']: d['id'] for d in self.drives()}
        self.assertTrue(self.call('exceptions').result['exceptions'])
        project = self.registry.register(actor='setup', idempotency_key='synthetic-project').project.id  # type: ignore[union-attr]
        values = {'drives': [original['SAMPLE-KEEP']], 'project': project, 'party': 'Synthetic recipient',
                  'party_kind': 'contact', 'on': '2026-11-01', 'idempotency_key': 'practice-release'}
        preview = self.call('release', **values)
        self.assertIsInstance(preview, Preview)
        self.assertIsInstance(self.call('release', **values, confirm=preview.token), Done)
        self.assertTrue(self.call('shipments').result['shipments'])
        self.workbook({'SYNTHETIC NEW': [HEADER, ['Synthetic updated', 'SAMPLE-KEEP'], ['Synthetic no serial', None]]})
        self.reload('reload-two')
        second = {d['serial']: d for d in self.drives()}
        self.assertEqual(set(second), {'SAMPLE-KEEP', None})
        self.assertEqual(second['SAMPLE-KEEP']['id'], original['SAMPLE-KEEP'])
        self.assertNotEqual(second[None]['id'], original[None])
        self.assertEqual(second['SAMPLE-KEEP']['label'], 'Synthetic updated')
        detail = self.call('get', drive=original['SAMPLE-KEEP']).result
        self.assertEqual([a['project']['text'] for a in detail['assignments']], ['SYNTHETIC NEW'])
        self.assertEqual([e['kind'] for e in detail['events']], ['received'])
        self.assertEqual(self.call('shipments').result['shipments'], [])
        self.assertEqual(self.call('exceptions').result['exceptions'], [])
        # Reopen the database: the serial map must outlive this process and absent serials.
        self.registry = SqliteProjectRegistry(self.folder / 't6.sqlite')
        self.people = People(self.registry, PreviewTokens('test'))
        self.workbook({'SYNTHETIC RETURN': [HEADER, ['Synthetic restored', 'SAMPLE-REMOVE']]})
        self.reload('reload-three')
        self.assertEqual(self.drives()[0]['id'], original['SAMPLE-REMOVE'])

    def test_report_counts_extra_tabs_exceptions_and_lists_drives_without_serial(self) -> None:
        self.workbook({
            'SYNTHETIC REPORT': [['PROJECT NAME: Synthetic report'], ['TOTAL', 4], ['IN-HOUSE', 3],
                                 ['RELEASED', 1], HEADER, ['Synthetic shared', 'SAMPLE-SHARED'],
                                 ['Synthetic no serial', None],
                                 ['Synthetic outbound', 'SAMPLE-REPORT-OUT', None, None, None,
                                  None, date(2026, 3, 1), 'Synthetic recipient'],
                                 [None, None, None, 'unreadable capacity']],
            'ARCHIVES': [[*HEADER[:1], 'SERIAL #', *HEADER[2:]], ['Synthetic archive', 'SAMPLE-SHARED']],
            'OTHER DRIVES': [HEADER, ['Synthetic other', 'SAMPLE-OTHER']],
            '12TB DRIVES': [HEADER, ['Synthetic large', 'SAMPLE-LARGE', None, '12TB']],
            'DEAD DRIVES': [HEADER, ['Synthetic dead', 'SAMPLE-REPORT-DEAD']],
            'PROJECT TEMPLATE': [HEADER, ['Synthetic template', 'SAMPLE-SKIP']],
        })
        report = self.reload()
        self.assertEqual({k: report[k] for k in ('rows_read', 'drives', 'assignments', 'released', 'retired',
                                                'drives_without_serial', 'exceptions')},
                         {'rows_read': 8, 'drives': 6, 'assignments': 7, 'released': 1, 'retired': 1,
                          'drives_without_serial': 1, 'exceptions': 1})
        serialless = next(d for d in self.drives() if d['serial'] is None)
        self.assertEqual(report['without_serial'], [{'id': serialless['id'], 'label': 'Synthetic no serial'}])
        exceptions = self.call('exceptions').result['exceptions']
        self.assertEqual(len(exceptions), 1)
        self.assertEqual((exceptions[0]['sheet'], exceptions[0]['row']), ('SYNTHETIC REPORT', 9))
        self.assertIn('unreadable capacity', exceptions[0]['detail'])
        self.assertIn('capacity', exceptions[0]['reason'] + exceptions[0]['detail'])

    def test_every_row_remains_an_assignment_even_when_two_rows_are_identical(self) -> None:
        self.workbook({'SYNTHETIC REPEAT': [HEADER, ['Synthetic repeat', 'SAMPLE-REPEAT'],
                                          ['Synthetic repeat', 'SAMPLE-REPEAT']]})
        report = self.reload()
        (drive,) = self.drives()
        self.assertEqual(len(self.call('get', drive=drive['id']).result['assignments']), 2)
        self.assertEqual(report['assignments'], 2)

    def test_release_uses_remarks_recipient_when_person_in_charge_is_also_present(self) -> None:
        self.workbook({'SYNTHETIC RELEASE': [[*HEADER, 'PERSON IN CHARGE'],
                                           ['Synthetic released', 'SAMPLE-REMARKS', None, None, None, None,
                                            date(2026, 4, 1), 'Synthetic recipient', 'Synthetic coordinator']]})
        self.reload()
        (drive,) = self.drives()
        events = self.call('get', drive=drive['id']).result['events']
        self.assertEqual(events[-1]['party']['ref']['text'], 'Synthetic recipient')

    def test_preview_is_read_only_changed_sheet_is_refused_and_retry_is_idempotent(self) -> None:
        self.workbook({'SYNTHETIC PREVIEW': [HEADER, ['Synthetic first', 'SAMPLE-FIRST']]})
        values = {'workbook': str(self.path), 'idempotency_key': 'preview-change'}
        preview = self.call('reload', **values)
        self.assertIsInstance(preview, Preview)
        self.assertEqual(self.drives(), [])
        self.workbook({'SYNTHETIC PREVIEW': [HEADER, ['Synthetic changed', 'SAMPLE-CHANGED']]})
        refused = self.call('reload', **values, confirm=preview.token)
        self.assertEqual(refused.rule, 'sheet_changed')
        self.assertEqual(self.drives(), [])
        report = self.reload('after-change')
        ids = [d['id'] for d in self.drives()]
        self.assertEqual(self.reload('after-change'), report)
        self.assertEqual([d['id'] for d in self.drives()], ids)

    def test_a_storage_failure_rolls_back_the_entire_replacement_and_can_be_retried(self) -> None:
        import sqlite3
        self.workbook({'SYNTHETIC BEFORE': [HEADER, ['Synthetic original', 'SAMPLE-ORIGINAL']]})
        self.reload()
        before = self.drives()
        detail = self.call('get', drive=before[0]['id']).result
        self.workbook({'SYNTHETIC AFTER': [HEADER, ['Synthetic replacement', 'SAMPLE-REPLACEMENT'],
                                         ['Synthetic failure', 'SAMPLE-FAIL']]})
        values = {'workbook': str(self.path), 'idempotency_key': 'atomic-reload'}
        preview = self.call('reload', **values)
        self.assertIsInstance(preview, Preview)
        # Inject a database write failure after the first replacement row has loaded.
        with self.registry._transaction() as db:
            db.execute("CREATE TRIGGER synthetic_storage_failure BEFORE INSERT ON drives "
                       "WHEN NEW.serial='SAMPLE-FAIL' BEGIN SELECT RAISE(ABORT, 'synthetic storage failure'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'synthetic storage failure'):
            self.call('reload', **values, confirm=preview.token)
        self.assertEqual(self.drives(), before)
        self.assertEqual(self.call('get', drive=before[0]['id']).result, detail)
        with self.registry._transaction() as db:
            db.execute('DROP TRIGGER synthetic_storage_failure')
        self.assertIsInstance(self.call('reload', **values, confirm=preview.token), Done)
        self.assertEqual({d['serial'] for d in self.drives()}, {'SAMPLE-REPLACEMENT', 'SAMPLE-FAIL'})

    def test_reload_is_available_to_api_callers_with_drives_permission(self) -> None:
        from t6.verbs import catalogue
        entry = next(v for v in catalogue() if (v['noun'], v['verb']) == ('drive', 'reload'))
        self.assertFalse(entry['local_only'])
        self.assertEqual(entry['role'], 'drives')
        self.assertIn('without_serial', entry['output_schema']['properties'])
