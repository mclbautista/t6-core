"""Reload through the verb API; all workbook evidence is synthetic and temporary."""
import os
import json
import tempfile
import unittest
from pathlib import Path
from datetime import date
from typing import Any
from unittest.mock import patch

from openpyxl import Workbook

from t6.person import People
from t6.notion.source import NotionSource
from t6.notion.testing import FakeNotion, project_properties
from t6.preview import PreviewTokens
from t6.registry import Rotation, SqliteProjectRegistry
from t6.verbs import VERBS, Done, Preview, Refused, invoke


HEADER = ['HARD DRIVE LABEL', 'SERIAL NUMBER', 'BRAND NAME', 'TOTAL DISK SPACE',
          'FORMAT', 'DATE RECEIVED', 'DATE RELEASED', 'REMARKS']


class DriveReloadFixture(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.path = self.folder / 'projects.xlsx'
        self.registry = SqliteProjectRegistry(self.folder / 't6.sqlite')
        self.people = People(self.registry, PreviewTokens('test'))
        self.notion = FakeNotion()
        self.notion.add_data_source('synthetic-projects')
        client = self.enterContext(self.notion.client())
        self.notion_source = NotionSource.from_client(client)
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
                      people=self.people, open_notion=lambda _: None, notion_source=self.notion_source)

    def reload(self, key: str = 'reload-one') -> dict[str, Any]:
        values = {'workbook': str(self.path), 'idempotency_key': key}
        preview = self.call('reload', **values)
        self.assertIsInstance(preview, Preview)
        done = self.call('reload', **values, confirm=preview.token)
        self.assertIsInstance(done, Done)
        return dict(done.result)

    def drives(self) -> list[dict[str, Any]]:
        return list(self.call('list').result['drives'])


class DriveReloadTests(DriveReloadFixture):
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

    def test_unmapped_project_stays_text_and_is_listed_once_in_report(self) -> None:
        self.workbook({'SYNTHETIC UNMAPPED': [HEADER, ['Synthetic first', 'SAMPLE-FIRST'],
                                             ['Synthetic second', 'SAMPLE-SECOND']]})
        report = self.reload()
        self.assertEqual(report['unmapped_projects'], ['SYNTHETIC UNMAPPED'])
        for drive in self.drives():
            detail = self.call('get', drive=drive['id']).result
            self.assertEqual(detail['assignments'][0]['project'],
                             {'page_id': None, 'text': 'SYNTHETIC UNMAPPED'})

    def test_text_only_project_text_filters_drive_list_like_a_project_id(self) -> None:
        self.workbook({'SYNTHETIC TEXT ONLY': [HEADER, ['Synthetic first', 'SAMPLE-FIRST']],
                       'SYNTHETIC OTHER': [HEADER, ['Synthetic second', 'SAMPLE-SECOND']]})
        self.reload()
        listed = self.call('list', project='SYNTHETIC TEXT ONLY').result
        self.assertEqual([d['serial'] for d in listed['drives']], ['SAMPLE-FIRST'])
        self.assertEqual(listed['total'], 1)
        self.assertEqual(self.call('list', project='SYNTHETIC MISSING').result['drives'], [])

    def test_reload_report_is_none_before_a_reload_then_the_last_report(self) -> None:
        self.assertIsNone(self.call('reload report').result['report'])
        self.workbook({'SYNTHETIC UNMAPPED': [HEADER, ['Synthetic first', 'SAMPLE-FIRST'], ['Synthetic blank', None]]})
        done = self.reload('report-one')
        self.workbook({'SYNTHETIC UNMAPPED': [HEADER, ['Synthetic first', 'SAMPLE-FIRST']], 'SYNTHETIC LATER': [HEADER]})
        last = self.reload('report-two')
        report = self.call('reload report').result['report']
        self.assertEqual(report['unmapped_projects'], last['unmapped_projects'])
        self.assertEqual(report['drives'], last['drives'])
        self.assertNotEqual(report['drives'], done['drives'])
        self.assertIsInstance(report['at'], str)
        self.assertEqual((report['exceptions'], report['exception_rows']), (0, []))
        self.workbook({'SYNTHETIC UNMAPPED': [HEADER, ['Synthetic first', 'SAMPLE-FIRST', None, 'BAD CAPACITY', None, 'not a date']]})
        self.reload('report-three')
        report = self.call('reload report').result['report']
        self.assertGreater(report['exceptions'], 0)
        self.assertEqual(report['exception_rows'], self.call('exceptions').result['exceptions'])

    def test_applied_mapping_survives_reopen_and_reloads_with_normalized_names(self) -> None:
        project = self.registry.register(actor='setup', idempotency_key='synthetic-mapping').project.id  # type: ignore[union-attr]
        mapping = self.folder / 'mapping.json'
        mapping.write_text(json.dumps([{'name': 'SYNTHETIC, MAPPED', 'project': project, 'certain': True}]))
        self.workbook({'SYNTHETIC MAPPED': [HEADER, ['Synthetic mapped', 'SAMPLE-MAPPED', None, None, None,
                                                    date(2026, 2, 1), date(2026, 3, 1), 'Synthetic recipient']],
                       'SYNTHETIC,MAPPED': [HEADER, ['Synthetic reused', 'SAMPLE-MAPPED']]})
        values = {'file': str(mapping), 'idempotency_key': 'apply-mapping'}
        preview = self.call('mapping apply', **values)
        self.assertIsInstance(preview, Preview)
        self.assertEqual(self.reload()['unmapped_projects'], ['SYNTHETIC MAPPED', 'SYNTHETIC,MAPPED'])
        self.assertIsInstance(self.call('mapping apply', **values, confirm=preview.token), Done)
        for number in range(2):
            self.registry = SqliteProjectRegistry(self.folder / 't6.sqlite')
            self.people = People(self.registry, PreviewTokens('test'))
            self.assertEqual(self.reload(f'mapped-reload-{number}')['unmapped_projects'], [])
            (drive,) = self.drives()
            detail = self.call('get', drive=drive['id']).result
            self.assertEqual([a['project'] for a in detail['assignments']],
                             [{'page_id': project, 'text': None}] * 2)
            self.assertEqual([e['project'] for e in detail['events']],
                             [{'page_id': project, 'text': None}] * 3)

    def test_propose_lists_sheet_and_tracker_names_without_storing_mapping(self) -> None:
        def register(name: str, code: str) -> str:
            page = self.notion.add_page('synthetic-projects', project_properties(name=name))
            return self.registry.register(actor='setup', idempotency_key=code, notion_page_id=page['id'],
                                          code=code, rotation=Rotation.IN).project.id  # type: ignore[union-attr]

        exact = register('Synthetic Aurora', 'SYN-A')
        coded = register('Synthetic Borealis', 'SYN-B')
        fuzzy = register('Synthetic Cobalt', 'SYN-C')
        unbound = self.registry.register(actor='setup', idempotency_key='synthetic-unbound',
                                         code='SYN-UNBOUND', rotation=Rotation.IN).project.id  # type: ignore[union-attr]
        register('Synthetic Unrelated', 'Synthetic Aurora')  # exact name must beat a code
        self.notion.add_page('synthetic-projects', project_properties(name='ZZZ UNREGISTERED ZZZ'))
        names = ['Synthetic Aurora', 'SYN-B', 'Synthetic Cobxxx', 'Synthetic Cobal',
                 'ZZZZZZZZZZ', 'ZZZ UNREGISTERED ZZZ', 'SYN-UNBOUND']
        self.workbook({name: [HEADER, [f'Synthetic label {number}', f'SAMPLE-{number}']]
                       for number, name in enumerate(names)})
        tracker = self.folder / 'tracker.xlsx'
        book = Workbook()
        sheet = book.active
        assert sheet is not None
        sheet.title = 'SYNTHETIC MONTH'
        sheet.append(['DATE', 'PROJECT NAME', 'LABEL', 'IN/OUT', 'RECEIVED BY'])
        sheet.append([date(2026, 1, 1), 'Synthetic Aurora', 'Synthetic tracker', 'OUT', 'Synthetic recipient'])
        sheet.append([date(2026, 1, 2), 'SYN-C', 'Synthetic tracker', 'IN'])
        book.save(tracker)
        book.close()
        sources = self.folder / 'sources.json'
        sources.write_text(json.dumps({'projects': 'synthetic-projects'}))
        output = self.folder / 'proposal.json'
        result = self.call('mapping propose', workbook=str(self.path), tracker=str(tracker),
                           data_sources=str(sources), file=str(output))
        self.assertIsInstance(result, Done)
        entries = json.loads(output.read_text())
        self.assertEqual(entries, result.result['mapping'])
        self.assertEqual({e['name']: (e['project'], e['certain']) for e in entries},
                         {'Synthetic Aurora': (exact, True), 'SYN-B': (coded, True),
                          'Synthetic Cobxxx': (fuzzy, False), 'Synthetic Cobal': (fuzzy, True),
                          'ZZZZZZZZZZ': (None, False), 'ZZZ UNREGISTERED ZZZ': (None, False),
                          'SYN-C': (fuzzy, True), 'SYN-UNBOUND': (unbound, True)})
        self.assertEqual(self.reload()['unmapped_projects'], sorted(names))

    def test_propose_skips_blank_names_and_writes_no_file_unless_asked(self) -> None:
        self.workbook({'SYNTHETIC SHOW': [HEADER, ['Synthetic a', 'SAMPLE-A']], ' - ': [HEADER, ['Synthetic b', 'SAMPLE-B']]})
        sources = self.folder / 'sources.json'
        sources.write_text(json.dumps({'projects': 'synthetic-projects'}))
        result = self.call('mapping propose', workbook=str(self.path), data_sources=str(sources))
        self.assertIsInstance(result, Done)
        self.assertEqual([e['name'] for e in result.result['mapping']], ['SYNTHETIC SHOW'])
        self.assertEqual(list(self.folder.glob('*mapping*')), [])

    def test_mapping_commands_are_available_as_nested_operator_commands(self) -> None:
        import argparse
        from t6.verbs import add_parsers
        parser = argparse.ArgumentParser()
        with patch('t6.verbs.VERBS', tuple(v for v in VERBS if v.noun == 'drive')):
            add_parsers(parser.add_subparsers(dest='command', required=True))
        args = parser.parse_args(['drive', 'mapping', 'propose', '--workbook', str(self.path),
                                  '--file', str(self.folder / 'mapping.json'), '--data-sources', 'sources.json'])
        self.assertEqual(args.registered_verb.verb, 'mapping propose')
        args = parser.parse_args(['drive', 'mapping', 'apply', '--file', str(self.folder / 'mapping.json'),
                                  '--idempotency-key', 'synthetic-apply'])
        self.assertEqual(args.registered_verb.verb, 'mapping apply')

    def test_report_includes_unmapped_names_even_when_their_rows_cannot_load(self) -> None:
        self.workbook({'SYNTHETIC UNREADABLE': [HEADER, [None, None, None, 'unreadable capacity']]})
        report = self.reload()
        self.assertEqual(report['drives'], 0)
        self.assertEqual(report['unmapped_projects'], ['SYNTHETIC UNREADABLE'])

    def test_apply_refuses_conflicting_or_unregistered_projects_without_partial_writes(self) -> None:
        first = self.registry.register(actor='setup', idempotency_key='synthetic-first').project.id  # type: ignore[union-attr]
        second = self.registry.register(actor='setup', idempotency_key='synthetic-second').project.id  # type: ignore[union-attr]
        self.workbook({'SYNTHETIC CHECKED': [HEADER, ['Synthetic checked', 'SAMPLE-CHECKED']]})
        mapping = self.folder / 'mapping.json'
        for number, (name, project, rule) in enumerate([
                ('SYNTHETIC, CHECKED', second, 'mapping'),
                ('SYNTHETIC OTHER', 't6p_' + '0' * 24, 'project')]):
            with self.subTest(rule=rule):
                mapping.write_text(json.dumps([{'name': 'SYNTHETIC CHECKED', 'project': first},
                                                {'name': name, 'project': project}]))
                refused = self.call('mapping apply', file=str(mapping), idempotency_key=f'invalid-{number}')
                self.assertIsInstance(refused, Refused)
                self.assertEqual(refused.rule, rule)
                self.assertEqual(self.reload(f'after-invalid-{number}')['unmapped_projects'], ['SYNTHETIC CHECKED'])

    def test_apply_refuses_a_mapping_changed_after_preview(self) -> None:
        project = self.registry.register(actor='setup', idempotency_key='synthetic-checked').project.id  # type: ignore[union-attr]
        self.workbook({'SYNTHETIC CHECKED': [HEADER, ['Synthetic checked', 'SAMPLE-CHECKED']]})
        mapping = self.folder / 'mapping.json'
        mapping.write_text(json.dumps([{'name': 'SYNTHETIC CHECKED', 'project': project}]))
        values = {'file': str(mapping), 'idempotency_key': 'changed-mapping'}
        preview = self.call('mapping apply', **values)
        self.assertIsInstance(preview, Preview)
        mapping.write_text(json.dumps([{'name': 'SYNTHETIC DIFFERENT', 'project': project}]))
        self.assertIsInstance(self.call('mapping apply', **values, confirm=preview.token), Refused)
        self.assertEqual(self.reload()['unmapped_projects'], ['SYNTHETIC CHECKED'])

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
