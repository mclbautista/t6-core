"""Custodian learning through HTTP verbs; all photos and workbook rows are synthetic."""
import base64
import os
from unittest.mock import patch

import test_drive_guesses as guess_tests
from test_drive_reload import HEADER
from test_llm_drive_photo import photo


class DriveLearningTests(guess_tests.DriveGuessTests):
    def setUp(self) -> None:
        super().setUp()
        self.enterContext(patch.dict(os.environ, {'T6_DRIVE_PHOTO_DIR': str(self.folder / 'photos')}))

    def test_approval_saves_short_name_reference_photo_and_accuracy_once(self) -> None:
        self.workbook({'SYNTHETIC SHOW': [HEADER, ['Box 14', 'SAMPLE-14']]})
        self.reload()
        drive = self.inventory()['Box 14']['id']
        image = base64.b64encode(photo(str(self.folder)).data).decode()
        result = self.capture('OUT MC7 D21\nR: Synthetic recipient', photo=image)
        values = dict(capture=result['capture'], item=0, choice='drive', typed='SAMPLE-14')
        self.http('drive/approve', **values)
        self.http('drive/approve', **values)
        detail = self.http('drive/get', drive=drive)
        self.assertEqual([(s['text'], s['confirmations'], s['project']) for s in detail['short_names']],
                         [('MC7 D21', 1, {'page_id': None, 'text': 'SYNTHETIC SHOW'})])
        self.assertTrue(detail['short_names'][0]['last_confirmed'])
        self.assertEqual(detail['reference_photo'], drive)
        self.assertEqual(self.http('drive/reference-photo', drive=drive)['photo'], image)
        summary = self.http('drive/matches')
        self.assertEqual((summary['total'], summary['counts']['typed'], summary['guess1_rate']), (1, 1, 0.0))
        self.assertEqual([(a['capture'], a['item'], a['outcome'], a['rank'], a['who']) for a in summary['approvals']],
                         [(result['capture'], 0, 'typed', None, 't6n_synthetickeeper')])
        self.assertTrue(summary['approvals'][0]['at'])

    def teach(self, drive: str, text: str = 'MC7 D21', *, times: int = 1, with_photo: bool = True) -> None:
        for _ in range(times):
            image = base64.b64encode(photo(str(self.folder)).data).decode() if with_photo else ''
            result = self.capture(f'OUT {text}\nR: Synthetic recipient', photo=image)
            self.http('drive/approve', capture=result['capture'], item=0, choice='drive', drive=drive)
            self.http('drive/capture/undo', capture=result['capture'])

    def test_third_confirmation_makes_exact_guess_but_two_stay_ranked(self) -> None:
        self.workbook({'SYNTHETIC SHOW': [HEADER, ['Box 14', 'SAMPLE-14']]})
        self.reload()
        drive = self.inventory()['Box 14']['id']
        self.teach(drive, times=2)
        ordinary = self.capture('OUT MC7 D21\nR: Synthetic recipient')
        (guess,) = ordinary['items'][0]['guesses']
        self.assertEqual(guess['drive'], drive)
        self.assertLess(guess['confidence'], 0.95)
        self.assertFalse(guess['exact'])
        self.http('drive/capture/undo', capture=ordinary['capture'])
        self.teach(drive)
        exact = self.capture('OUT MC7 D21\nR: Synthetic recipient')
        (guess,) = exact['items'][0]['guesses']
        self.assertEqual((guess['drive'], guess['confidence'], guess['exact']), (drive, 0.95, True))
        self.assertEqual(self.inbox()[0]['guesses'], exact['items'][0]['guesses'])

    def test_shared_short_name_wrong_direction_and_no_reference_prevent_exact(self) -> None:
        self.workbook({'SYNTHETIC SHOW': [HEADER, ['Box 14', 'SAMPLE-14'], ['Box 15', 'SAMPLE-15'], ['Box 16', 'SAMPLE-16']]})
        self.reload()
        ids = self.inventory()
        self.teach(ids['Box 14']['id'], times=3)
        self.teach(ids['Box 15']['id'])  # even one confirmation makes the name ambiguous
        shared = self.capture('OUT MC7 D21\nR: Synthetic recipient')
        self.assertTrue(shared['items'][0]['guesses'])
        self.assertTrue(all(g['confidence'] < 0.95 and not g['exact'] for g in shared['items'][0]['guesses']))
        self.http('drive/capture/undo', capture=shared['capture'])
        wrong = self.capture('IN MC7 D21')
        self.assertEqual(wrong['items'][0]['guesses'], [])
        self.teach(ids['Box 16']['id'], 'MC8 D22', times=3, with_photo=False)
        self.assertIsNone(self.http('drive/get', drive=ids['Box 16']['id'])['reference_photo'])
        no_reference = self.capture('OUT MC8 D22\nR: Synthetic recipient')
        self.assertTrue(no_reference['items'][0]['guesses'])
        self.assertTrue(all(g['confidence'] < 0.95 and not g['exact'] for g in no_reference['items'][0]['guesses']))

    def test_reload_preserves_learning_and_photo_only_for_preserved_drive_ids(self) -> None:
        self.workbook({'SYNTHETIC SHOW': [HEADER, ['Box 14', 'SAMPLE-14'], ['Box 15', 'SAMPLE-15']]})
        self.reload()
        ids = self.inventory()
        kept, removed = ids['Box 14']['id'], ids['Box 15']['id']
        self.teach(kept, times=3)
        self.teach(removed, 'MC8 D22', times=3)
        before = self.http('drive/get', drive=kept)
        image = self.http('drive/reference-photo', drive=kept)
        self.workbook({'SYNTHETIC SHOW': [HEADER, ['Relabelled', 'SAMPLE-14']]})
        values = dict(workbook=str(self.path), idempotency_key='http-reload-learning')
        # Reload is previewed through the same HTTP verb API.
        from urllib.request import Request, urlopen
        import json
        request = Request(f'http://127.0.0.1:{self.server.server_port}/v1/drive/reload', data=json.dumps(values).encode(),
                          headers={'Authorization': 'Bearer synthetic-keeper'})
        with urlopen(request) as response:
            preview = json.load(response)
        self.assertEqual(preview['outcome'], 'preview', preview)
        self.http('drive/reload', **values, confirm=preview['token'])
        after = self.http('drive/get', drive=kept)
        self.assertEqual(after['short_names'], before['short_names'])
        self.assertEqual(after['reference_photo'], before['reference_photo'])
        self.assertEqual(self.http('drive/reference-photo', drive=kept), image)
        self.assertEqual(self.http('drive/matches')['total'], 6)  # accuracy history spans nightly Reloads
        # A dropped serial returning later recovers its ID, but never its old learning.
        self.workbook({'SYNTHETIC SHOW': [HEADER, ['Box 14', 'SAMPLE-14'], ['Box 15', 'SAMPLE-15']]})
        self.reload('return-removed')
        returned = self.http('drive/get', drive=removed)
        self.assertEqual((returned['short_names'], returned['reference_photo']), ([], None))
        exact = self.capture('OUT MC7 D21\nR: Synthetic recipient')['items'][0]['guesses'][0]
        self.assertEqual((exact['drive'], exact['confidence'], exact['exact']), (kept, 0.95, True))

    def test_accuracy_counts_every_outcome_and_filters_by_approval_date(self) -> None:
        from datetime import datetime, timedelta
        self.workbook({'SYNTHETIC SHOW': [HEADER, ['SYN D01', 'SAMPLE-01'], ['SYN D02', 'SAMPLE-02'], ['SYN D03', 'SAMPLE-03']]})
        self.reload()
        for rank in (1, 2, 3):
            result = self.capture('OUT SYN\nR: Synthetic recipient')
            drive = result['items'][0]['guesses'][rank - 1]['drive']
            self.http('drive/approve', capture=result['capture'], item=0, choice='drive', drive=drive)
            self.http('drive/capture/undo', capture=result['capture'])
        result = self.capture('OUT SYN\nR: Synthetic recipient')
        self.http('drive/approve', capture=result['capture'], item=0, choice='none')
        self.http('drive/approve', capture=result['capture'], item=0, choice='none')
        self.http('drive/approve', capture=result['capture'], item=0, choice='not_a_drive')
        self.http('drive/approve', capture=result['capture'], item=0, choice='not_a_drive')
        new = self.capture('OUT Invented zebra\nR: Synthetic recipient')
        self.http('drive/approve', capture=new['capture'], item=0, choice='new', label='Synthetic new box')
        typed = self.capture('OUT Invented unicorn\nR: Synthetic recipient')
        self.http('drive/approve', capture=typed['capture'], item=0, choice='drive', typed='SAMPLE-03')
        summary = self.http('drive/matches')
        self.assertEqual(summary['counts'], {'guess1': 1, 'guess2': 1, 'guess3': 1, 'none': 1, 'typed': 1, 'new': 1, 'not_a_drive': 1})
        self.assertEqual(summary['total'], 7)
        self.assertAlmostEqual(summary['guess1_rate'], 1 / 7)
        self.assertEqual([a['rank'] for a in summary['approvals']], [1, 2, 3, None, None, None, None])
        approved_day = datetime.fromisoformat(summary['approvals'][0]['at']).date()
        self.assertEqual(self.http('drive/matches', start=approved_day.isoformat(), end=approved_day.isoformat()), summary)
        empty = self.http('drive/matches', end=(approved_day - timedelta(days=1)).isoformat())
        self.assertEqual((empty['total'], empty['guess1_rate'], empty['approvals']), (0, 0.0, []))
        self.assertTrue(all(count == 0 for count in empty['counts'].values()))
        self.assertEqual(self.http('drive/matches', start=(approved_day + timedelta(days=1)).isoformat())['total'], 0)

    def test_newest_approved_photo_replaces_reference_and_text_only_keeps_it(self) -> None:
        import struct
        import zlib
        self.workbook({'SYNTHETIC SHOW': [HEADER, ['Box 14', 'SAMPLE-14']]})
        self.reload()
        drive = self.inventory()['Box 14']['id']
        self.teach(drive)
        first = self.http('drive/reference-photo', drive=drive)['photo']
        data = photo(str(self.folder)).data
        text_chunk = b'tEXt' + b'Comment\x00Synthetic second photo'
        second = base64.b64encode(data[:-12] + struct.pack('>I', len(text_chunk) - 4) + text_chunk
                                  + struct.pack('>I', zlib.crc32(text_chunk)) + data[-12:]).decode()
        result = self.capture('OUT MC7 D21\nR: Synthetic recipient', photo=second)
        self.http('drive/approve', capture=result['capture'], item=0, choice='drive', drive=drive)
        self.assertNotEqual(first, second)
        self.assertEqual(self.http('drive/reference-photo', drive=drive)['photo'], second)
        self.teach(drive, with_photo=False)
        self.assertEqual(self.http('drive/reference-photo', drive=drive)['photo'], second)

    def test_learned_project_name_shortlists_its_other_open_drives(self) -> None:
        project = self.project('Synthetic Nebula', 'SNB')
        self.workbook({'NEBULA': [HEADER, ['Box 14', 'SAMPLE-14'], ['Box 15', 'SAMPLE-15']],
                       'OTHER': [HEADER, ['MC7 D21', 'SAMPLE-OTHER']]})
        self.mapped({'NEBULA': project})
        ids = self.inventory()
        self.teach(ids['Box 14']['id'])
        result = self.capture('OUT MC7 D21\nR: Synthetic recipient')
        self.assertEqual({g['drive'] for g in result['items'][0]['guesses']}, {ids['Box 14']['id'], ids['Box 15']['id']})
        detail = self.http('drive/get', drive=ids['Box 14']['id'])
        self.assertEqual(detail['short_names'][0]['project'], {'page_id': project, 'text': None})
