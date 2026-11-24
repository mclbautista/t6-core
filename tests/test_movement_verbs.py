"""Movement behaviour through verbs using temporary synthetic workbooks."""
from typing import Any
from datetime import date

from openpyxl import Workbook

import test_drive_reload as reload_tests

HEADER = reload_tests.HEADER
from t6.verbs import VERBS, Done, Preview, invoke


class MovementTests(reload_tests.DriveReloadFixture):
    def call_movement(self, verb: str, **values: Any) -> Any:
        entries = [v for v in VERBS if (v.noun, v.verb) == ('movement', verb)]
        self.assertEqual(len(entries), 1, f'movement {verb} must be registered')
        entry = entries[0]
        return invoke(entry, entry.input_model.model_validate(values), actor='synthetic-keeper',
                      people=self.people, open_notion=lambda _: None)

    def tracker(self, tabs: dict[str, list[list[Any]]]) -> str:
        path = self.folder / 'tracker.xlsx'
        book = Workbook()
        book.remove(book.active)  # type: ignore[arg-type]
        for name, rows in tabs.items():
            sheet = book.create_sheet(name)
            for row in rows:
                sheet.append(row)
        book.save(path)
        book.close()
        return str(path)

    def load_tracker(self, rows: list[list[Any]], key: str = 'movement-reload') -> dict[str, Any]:
        tracker = self.tracker({'SYNTHETIC MONTH': [
            ['DATE', 'PROJECT NAME', 'LABEL', 'HD DESCRIPTION', 'IN/OUT', 'RECEIVED BY', 'REMARKS'], *rows]})
        values = {'workbook': str(self.path), 'tracker': tracker, 'idempotency_key': key}
        preview = self.call('reload', **values)
        self.assertIsInstance(preview, Preview)
        done = self.call('reload', **values, confirm=preview.token)
        self.assertIsInstance(done, Done)
        return dict(done.result)

    def test_unmatched_rows_are_searchable_by_label_person_and_date(self) -> None:
        self.workbook({'SYNTHETIC PROJECT': [HEADER, ['Synthetic existing', 'SAMPLE-ONE']]})
        report = self.load_tracker([[date(2026, 3, 2), 'SYNTHETIC PROJECT', 'Synthetic missing', None,
                                     'OUT', 'Synthetic borrower', 'Synthetic note']])
        result = self.call_movement('list', q='missing', person='borrower',
                                    start='2026-03-01', end='2026-03-03').result['movements']
        self.assertEqual(len(result), 1)
        self.assertIsNone(result[0]['drive'])
        self.assertEqual(result[0]['label'], 'Synthetic missing')
        self.assertEqual(result[0]['origin']['row'], 2)
        self.assertEqual((report['movements_read'], report['movements_matched'], report['movements_unmatched']), (1, 0, 1))
        self.assertEqual(self.call_movement('list', end='2026-03-01').result['movements'], [])

    def test_certain_out_follows_undated_receipt_but_duplicate_labels_stay_unmatched(self) -> None:
        self.workbook({'SYNTHETIC PROJECT': [HEADER, ['Synthetic unique', 'SAMPLE-UNIQUE'],
                                            ['Synthetic twin', 'SAMPLE-TWIN-A'], ['Synthetic twin', 'SAMPLE-TWIN-B']]})
        report = self.load_tracker([
            [date(2026, 4, 1), 'SYNTHETIC PROJECT', '  SYNTHETIC   UNIQUE ', None, 'OUT', 'Synthetic borrower'],
            [date(2026, 4, 2), 'SYNTHETIC PROJECT', 'Synthetic twin', None, 'OUT', 'Synthetic other'],
            [date(2026, 4, 3), 'SYNTHETIC OTHER', 'Synthetic unique', None, 'OUT', 'Synthetic other'],
        ])
        drives = {d['serial']: d for d in self.drives()}
        unique = drives['SAMPLE-UNIQUE']
        self.assertEqual(unique['status'], 'released')
        events = self.call('get', drive=unique['id']).result['events']
        self.assertEqual([(e['kind'], e['on']) for e in events], [('received', None), ('released', '2026-04-01')])
        self.assertEqual(events[-1]['party']['ref']['text'], 'Synthetic borrower')
        movements = self.call_movement('list').result['movements']
        self.assertEqual([m['drive'] for m in movements], [unique['id'], None, None])
        self.assertEqual((report['movements_matched'], report['movements_unmatched']), (1, 2))
        self.assertEqual(self.call_movement('list', drive=unique['id']).result['movements'], movements[:1])

    def test_tracker_inventory_adds_only_missing_serials_and_keeps_status_hints(self) -> None:
        self.workbook({'SYNTHETIC PROJECT': [HEADER, ['Synthetic authoritative', 'SAMPLE-EXISTING', 'Synthetic brand', '2TB']]})
        tracker = self.tracker({
            'Swing Drives': [HEADER, ['Synthetic tracker label', 'SAMPLE-EXISTING', 'Other synthetic brand', '8TB',
                                     None, None, None, 'DEFECTIVE'], ['Synthetic swing', 'SAMPLE-SWING']],
            'Archive HD': [HEADER, ['Synthetic archive', 'SAMPLE-ARCHIVE', None, None, None, None, None, 'DEAD DRIVE']],
            'Released Viva HDs': [['DATE: March 2, 2026'], ['LABEL', 'SERIAL #', 'HD DESCRIPTION', 'QUANTITY'],
                                  ['SYNTHETIC GROUP', None, None, 2], ['Synthetic released', 'SAMPLE-RELEASED']],
        })
        values = {'workbook': str(self.path), 'tracker': tracker, 'idempotency_key': 'inventory'}
        preview = self.call('reload', **values)
        done = self.call('reload', **values, confirm=preview.token)
        self.assertIsInstance(done, Done)
        drives = {d['serial']: d for d in self.drives()}
        self.assertEqual(set(drives), {'SAMPLE-EXISTING', 'SAMPLE-SWING', 'SAMPLE-ARCHIVE', 'SAMPLE-RELEASED'})
        existing = drives['SAMPLE-EXISTING']
        self.assertEqual((existing['label'], existing['brand'], existing['capacity_tb']),
                         ('Synthetic authoritative', 'Synthetic brand', 2.0))
        self.assertTrue(existing['needs_review'])
        self.assertIn('DEFECTIVE', existing['review_note'])
        self.assertEqual(drives['SAMPLE-ARCHIVE']['status'], 'retired')
        released = self.call('get', drive=drives['SAMPLE-RELEASED']['id']).result
        self.assertEqual((released['drive']['status'], released['events'][-1]['on']), ('released', '2026-03-02'))
        self.assertEqual(released['assignments'][0]['project']['text'], 'SYNTHETIC GROUP')

    def test_a_title_row_above_the_header_is_skipped_but_a_stray_row_below_it_is_not(self) -> None:
        self.workbook({'SYNTHETIC PROJECT': [HEADER, ['Synthetic existing', 'SAMPLE-ONE']]})
        month_header = ['DATE', 'PROJECT NAME', 'LABEL', 'HD DESCRIPTION', 'IN/OUT', 'RECEIVED BY', 'REMARKS']
        month_row = [date(2026, 3, 2), 'SYNTHETIC PROJECT', 'Synthetic existing', None, 'OUT', 'Synthetic borrower']
        tracker = self.tracker({
            'SYNTHETIC MONTH': [['SYNTHETIC TRACKER TITLE', None, None, None, None, None, None], month_header, month_row],
            'Archive HD': [['ARCHIVE', None, None, None, None, None], HEADER, ['Synthetic archive', 'SAMPLE-ARCHIVE']],
            'Swing Drives': [['SWING DRIVE', None, None, None, None], HEADER, ['Synthetic swing', 'SAMPLE-SWING']],
        })
        values = {'workbook': str(self.path), 'tracker': tracker, 'idempotency_key': 'titles'}
        done = self.call('reload', **values, confirm=self.call('reload', **values).token)
        self.assertIsInstance(done, Done)
        self.assertEqual(done.result['exceptions'], 0)
        self.assertEqual(self.call('exceptions').result['exceptions'], [])
        self.assertEqual((done.result['movements_read'], {d['serial'] for d in self.drives()}),
                         (1, {'SAMPLE-ONE', 'SAMPLE-ARCHIVE', 'SAMPLE-SWING'}))
        stray = self.tracker({'SYNTHETIC MONTH': [month_header, month_row, ['SYNTHETIC STRAY', None, None, None, None, None, None]]})
        again = {'workbook': str(self.path), 'tracker': stray, 'idempotency_key': 'stray'}
        done = self.call('reload', **again, confirm=self.call('reload', **again).token)
        self.assertEqual(done.result['exceptions'], 1)

    def test_projects_read_returns_the_latest_movement_time_of_each_project(self) -> None:
        self.workbook({'SYNTHETIC ALPHA': [HEADER, ['Synthetic a', 'SAMPLE-A']],
                       'SYNTHETIC BETA': [HEADER, ['Synthetic b', 'SAMPLE-B']],
                       'SYNTHETIC GAMMA': [HEADER, ['Synthetic g', 'SAMPLE-G']]})
        self.load_tracker([
            [date(2026, 3, 2), 'SYNTHETIC ALPHA', 'Synthetic a', None, 'OUT', 'Synthetic borrower'],
            [date(2026, 4, 1), 'SYNTHETIC ALPHA', 'Synthetic a', None, 'IN', 'Synthetic keeper'],
            [date(2026, 3, 15), 'SYNTHETIC BETA', 'Synthetic b', None, 'OUT', 'Synthetic borrower']])
        latest = {p['project']['text']: p['latest_movement'] for p in self.call('projects').result['projects']}
        self.assertTrue(latest['SYNTHETIC ALPHA'].startswith('2026-04-01'))
        self.assertTrue(latest['SYNTHETIC BETA'].startswith('2026-03-15'))
        self.assertIsNone(latest['SYNTHETIC GAMMA'])

    # --- movement photos (t6-core #669)
    PNG = b'\x89PNG\r\n\x1a\n' + b'synthetic photo bytes'
    JPEG = b'\xff\xd8\xff' + b'synthetic other photo'

    def photo_call(self, verb: str, actor: str = 'synthetic-keeper', **values: Any) -> Any:
        entry = next(e for e in VERBS if (e.noun, e.verb) == ('movement', f'photo-{verb}'))
        return invoke(entry, entry.input_model.model_validate(values), actor=actor, people=self.people, open_notion=lambda _: None)

    def photo_setup(self) -> tuple[str, Any]:
        import base64
        from unittest.mock import patch
        import os
        folder = self.folder / 'photos'
        env = patch.dict(os.environ, {'T6_DRIVE_PHOTO_DIR': str(folder), 'T6_DRIVE_CUSTODIAN_PERSON_ID': 'synthetic-keeper'})
        env.start()
        self.addCleanup(env.stop)
        self.workbook({'SYNTHETIC PROJECT': [HEADER, ['Synthetic photo drive', 'SAMPLE-PHOTO']]})
        self.load_tracker([[date(2026, 5, 1), 'SYNTHETIC PROJECT', 'Synthetic photo drive', None, 'OUT', 'Synthetic borrower']])
        movement = self.call_movement('list').result['movements'][0]['id']
        with self.registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES ('synthetic-other', 'staff', '[]', '[]', NULL, 0, 1)")
            db.execute("INSERT INTO person_permissions VALUES ('synthetic-other', 'drives')")
            db.execute("INSERT INTO persons VALUES ('synthetic-operator', 'staff', '[]', '[]', NULL, 0, 1)")
            db.execute("INSERT INTO person_permissions VALUES ('synthetic-operator', 'drives')")
            db.execute("INSERT INTO person_permissions VALUES ('synthetic-operator', 'operator')")
        return movement, base64

    def test_a_photo_is_added_read_replaced_and_listed_on_its_movement(self) -> None:
        movement, base64 = self.photo_setup()
        encoded = base64.b64encode(self.PNG).decode()
        added = self.photo_call('add', movement=movement, photo=encoded, media_type='image/png')
        self.assertIsInstance(added, Done)
        self.assertEqual((added.result['size'], added.result['media_type']), (len(self.PNG), 'image/png'))
        files = [p for p in (self.folder / 'photos').rglob('*') if p.is_file()]
        self.assertEqual(len(files), 1)
        self.assertEqual(oct(files[0].stat().st_mode & 0o777), oct(0o600))
        self.assertEqual(base64.b64decode(self.photo_call('get', movement=movement).result['photo']), self.PNG)
        self.assertEqual(self.call_movement('list').result['movements'][0]['photo'], 'upload')
        self.photo_call('add', movement=movement, photo=base64.b64encode(self.JPEG).decode(), media_type='image/jpeg')
        again = self.photo_call('get', movement=movement).result
        self.assertEqual((again['media_type'], base64.b64decode(again['photo'])), ('image/jpeg', self.JPEG))
        self.assertEqual(len([p for p in (self.folder / 'photos').rglob('*') if p.is_file()]), 1)  # the old file is gone

    def test_a_photo_must_be_a_real_image_of_the_stated_type_within_the_size_limit_on_a_real_movement(self) -> None:
        from unittest.mock import patch
        movement, base64 = self.photo_setup()
        png = base64.b64encode(self.PNG).decode()
        def rule(**values: Any) -> str:
            result = self.photo_call('add', **values)
            self.assertEqual(result.outcome, 'refused')
            return str(result.rule)
        self.assertEqual(rule(movement=movement, photo=png, media_type='image/jpeg'), 'media_type')
        self.assertEqual(rule(movement=movement, photo=base64.b64encode(b'GIF89a....').decode(), media_type='image/png'), 'media_type')
        self.assertEqual(rule(movement=movement, photo='not base64 !!', media_type='image/png'), 'input')
        self.assertEqual(rule(movement='t6m_' + '0' * 32, photo=png, media_type='image/png'), 'movement')
        with patch('t6.drives.photos.LIMIT', 10):
            self.assertEqual(rule(movement=movement, photo=png, media_type='image/png'), 'too_large')
        self.assertEqual(list((self.folder / 'photos').rglob('*.png')), [])
        self.assertEqual(self.photo_call('get', movement=movement).rule, 'photo')

    def test_only_the_custodian_or_an_operator_may_delete_a_photo_and_nothing_goes_to_an_llm(self) -> None:
        from unittest.mock import patch
        movement, base64 = self.photo_setup()
        with patch('t6.llm.task.run', side_effect=AssertionError('a photo must never reach t6.llm')):
            self.photo_call('add', movement=movement, photo=base64.b64encode(self.PNG).decode(), media_type='image/png')
            self.photo_call('get', movement=movement)
        refused = self.photo_call('delete', actor='synthetic-other', movement=movement)
        self.assertEqual((refused.outcome, refused.rule), ('refused', 'permission'))
        self.assertEqual(self.photo_call('get', movement=movement).outcome, 'done')
        self.assertEqual(self.photo_call('delete', actor='synthetic-operator', movement=movement).outcome, 'done')
        self.assertEqual(self.photo_call('get', movement=movement).rule, 'photo')
        self.photo_call('add', movement=movement, photo=base64.b64encode(self.PNG).decode(), media_type='image/png')
        self.assertEqual(self.photo_call('delete', movement=movement).outcome, 'done')  # the custodian
        self.assertEqual([p for p in (self.folder / 'photos').rglob('*') if p.is_file()], [])
        self.assertIsNone(self.call_movement('list').result['movements'][0].get('photo'))

    def test_a_reload_removes_the_photos_of_the_movements_it_replaces_but_a_failed_reload_keeps_them(self) -> None:
        movement, base64 = self.photo_setup()
        self.photo_call('add', movement=movement, photo=base64.b64encode(self.PNG).decode(), media_type='image/png')
        self.assertEqual(len([p for p in (self.folder / 'photos').rglob('*') if p.is_file()]), 1)
        tracker = self.tracker({'SYNTHETIC MONTH': [['DATE', 'PROJECT NAME', 'LABEL', 'HD DESCRIPTION', 'IN/OUT', 'RECEIVED BY', 'REMARKS'],
                                                    [date(2026, 6, 1), 'SYNTHETIC PROJECT', 'Synthetic photo drive', None, 'IN', 'Synthetic keeper']]})
        values = {'workbook': str(self.path), 'tracker': tracker, 'idempotency_key': 'photo-reload'}
        preview = self.call('reload', **values)
        self.assertIsInstance(preview, Preview)
        self.assertEqual(len([p for p in (self.folder / 'photos').rglob('*') if p.is_file()]), 1)  # a preview changes nothing
        self.assertIsInstance(self.call('reload', **values, confirm=preview.token), Done)
        self.assertEqual([p for p in (self.folder / 'photos').rglob('*') if p.is_file()], [])
        with self.registry._transaction() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM drive_movement_photos').fetchone()[0], 0)

    def test_project_counts_distinct_drives_and_unknown_party(self) -> None:
        self.workbook({'SYNTHETIC PROJECT': [HEADER, ['Synthetic first', 'SAMPLE-FIRST'],
                                            ['Synthetic second', 'SAMPLE-SECOND'], ['Synthetic second', 'SAMPLE-SECOND'],
                                            ['Synthetic third', 'SAMPLE-THIRD']],
                       'SYNTHETIC OTHER': [HEADER, ['Synthetic reused', 'SAMPLE-FIRST']]})
        self.load_tracker([[date(2026, 4, 1), 'SYNTHETIC PROJECT', 'Synthetic first', None, 'OUT', '-'],
                           [date(2026, 4, 2), 'SYNTHETIC PROJECT', 'Synthetic third', None, 'OUT', None]])
        counts = self.call('projects').result['projects']
        self.assertEqual(counts, [
            {'project': {'page_id': None, 'text': 'SYNTHETIC PROJECT'}, 'total': 3, 'in_house': 1, 'released': 2,
             'latest_movement': '2026-04-02'},
            {'project': {'page_id': None, 'text': 'SYNTHETIC OTHER'}, 'total': 1, 'in_house': 0, 'released': 1,
             'latest_movement': None},
        ])
        for drive in self.drives():
            if drive['status'] == 'released':
                self.assertIsNone(self.call('get', drive=drive['id']).result['events'][-1]['party'])

    def correct(self, movement: str, key: str, **changes: Any) -> dict[str, Any]:
        values = {'movement': movement, 'idempotency_key': key, **changes}
        preview = self.call_movement('correct', **values)
        self.assertIsInstance(preview, Preview)
        done = self.call_movement('correct', **values, confirm=preview.token)
        self.assertIsInstance(done, Done)
        repeated = self.call_movement('correct', **values, confirm=preview.token)
        self.assertEqual(done, repeated)
        return dict(done.result['movement'])

    def test_correcting_place_updates_the_drive_fact_in_list_and_get(self) -> None:
        self.workbook({'SYNTHETIC PROJECT': [HEADER, ['Synthetic first', 'SAMPLE-FIRST']]})
        self.load_tracker([[date(2026, 4, 1), 'SYNTHETIC PROJECT', 'Synthetic first', None, 'IN', None]])
        movement = self.call_movement('list').result['movements'][0]
        for number, place in enumerate(('BOX', 'TABLE', None)):
            movement = self.correct(movement['id'], f'place-{number}', place=place)
            listed = self.drives()[0]
            detail = self.call('get', drive=movement['drive']).result['drive']
            for item in (listed, detail):
                self.assertEqual((item['place'], item['office_location']), (place, place))

    def test_correction_appends_and_changes_derived_status(self) -> None:
        self.workbook({'SYNTHETIC PROJECT': [HEADER, ['Synthetic first', 'SAMPLE-FIRST']]})
        self.load_tracker([[date(2026, 4, 1), 'SYNTHETIC PROJECT', 'Synthetic first', None, 'OUT', 'Synthetic borrower']])
        original = self.call_movement('list').result['movements'][0]
        correction = self.correct(original['id'], 'correct-direction', direction='IN', place='CABINET', person=None)
        rows = self.call_movement('list').result['movements']
        self.assertEqual(rows, [original, correction])
        self.assertEqual(correction['corrects'], original['id'])
        self.assertEqual(correction['recorder'], 'synthetic-keeper')
        self.assertEqual(correction['place'], 'CABINET')
        self.assertEqual(self.drives()[0]['status'], 'in custody')
        self.assertEqual(self.call('get', drive=original['drive']).result['events'][1]['kind'], 'released')

    def test_date_and_drive_corrections_rederive_each_affected_timeline(self) -> None:
        self.workbook({'SYNTHETIC PROJECT': [HEADER, ['Synthetic first', 'SAMPLE-FIRST'],
                                            ['Synthetic second', 'SAMPLE-SECOND']]})
        self.load_tracker([[date(2026, 4, 1), 'SYNTHETIC PROJECT', 'Synthetic first', None, 'OUT', 'Synthetic borrower'],
                           [date(2026, 4, 2), 'SYNTHETIC PROJECT', 'Synthetic first', None, 'IN', None]])
        first, second = self.call_movement('list').result['movements']
        corrected = self.correct(second['id'], 'correct-date', on='2026-03-01')
        self.assertEqual(next(d for d in self.drives() if d['serial'] == 'SAMPLE-FIRST')['status'], 'released')
        other = next(d for d in self.drives() if d['serial'] == 'SAMPLE-SECOND')['id']
        moved = self.correct(first['id'], 'correct-drive', drive=other, person='Synthetic revised')
        statuses = {d['id']: d['status'] for d in self.drives()}
        self.assertEqual(statuses, {first['drive']: 'in custody', other: 'released'})
        self.assertEqual(self.call('get', drive=other).result['events'][-1]['party']['ref']['text'], 'Synthetic revised')
        self.assertEqual(self.call_movement('list').result['movements'], [first, second, corrected, moved])

    def test_mapping_reuse_reload_wipe_and_tracker_preview_change(self) -> None:
        import json
        from t6.verbs import Refused
        project = self.registry.register(actor='setup', idempotency_key='synthetic-movement-project').project.id  # type: ignore[union-attr]
        mapping = self.folder / 'mapping.json'
        mapping.write_text(json.dumps([{'name': name, 'project': project} for name in ('SYNTHETIC PROJECT', 'SYNTHETIC ALIAS')]))
        values = {'file': str(mapping), 'idempotency_key': 'movement-mapping'}
        preview = self.call('mapping apply', **values)
        self.assertIsInstance(self.call('mapping apply', **values, confirm=preview.token), Done)
        self.workbook({'SYNTHETIC PROJECT': [HEADER, ['Synthetic first', 'SAMPLE-FIRST']]})
        row = [date(2026, 4, 1), 'SYNTHETIC ALIAS', 'Synthetic first', None, 'OUT', 'Synthetic borrower']
        self.load_tracker([row])
        original = self.call_movement('list', project=project).result['movements'][0]
        self.assertIsNotNone(original['drive'])
        self.correct(original['id'], 'practice-correction', direction='IN')
        self.load_tracker([row], key='replacement')
        movements = self.call_movement('list').result['movements']
        self.assertEqual(len(movements), 1)
        self.assertNotEqual(movements[0]['id'], original['id'])
        self.assertEqual(movements[0]['drive'], original['drive'])
        values = {'workbook': str(self.path), 'tracker': str(self.folder / 'tracker.xlsx'), 'idempotency_key': 'changed-tracker'}
        preview = self.call('reload', **values)
        self.tracker({'SYNTHETIC MONTH': [['DATE', 'PROJECT NAME', 'LABEL', 'IN/OUT', 'RECEIVED BY'],
                                        [date(2026, 5, 1), 'SYNTHETIC ALIAS', 'Synthetic first', 'IN', None]]})
        refused = self.call('reload', **values, confirm=preview.token)
        self.assertIsInstance(refused, Refused)
        self.assertEqual(refused.rule, 'sheet_changed')
        self.assertEqual(self.call_movement('list').result['movements'], movements)

    def test_sheet_dates_and_retirement_share_the_movement_timeline(self) -> None:
        self.workbook({'SYNTHETIC PROJECT': [HEADER,
            ['Synthetic later', 'SAMPLE-LATER', None, None, None, date(2026, 5, 1)],
            ['Synthetic retired', 'SAMPLE-RETIRED', None, None, None, None, None, 'DEAD DRIVE']]})
        self.load_tracker([[date(2026, 4, 1), 'SYNTHETIC PROJECT', 'Synthetic later', None, 'OUT', 'Synthetic borrower'],
                           [date(2026, 6, 1), 'SYNTHETIC PROJECT', 'Synthetic retired', None, 'OUT', 'Synthetic borrower']])
        self.assertEqual({d['serial']: d['status'] for d in self.drives()},
                         {'SAMPLE-LATER': 'in custody', 'SAMPLE-RETIRED': 'retired'})
        from t6.verbs import catalogue
        entries = {(e['noun'], e['verb']): e for e in catalogue()}
        for key in [('movement', 'list'), ('movement', 'correct'), ('drive', 'projects')]:
            self.assertFalse(entries[key]['local_only'])
        schema = entries['movement', 'list']['output_schema']
        self.assertIn('MovementItem', schema['$defs'])
