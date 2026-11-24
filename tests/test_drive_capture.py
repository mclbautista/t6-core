"""Recording-channel captures through the HTTP verb API and fake photo provider."""
import base64
import json
import os
import threading
from typing import Any
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from test_drive_reload import DriveReloadFixture, HEADER
from test_llm_drive_photo import photo
from t6.llm.testing import FakeProvider
from t6.server.http import make_server
from t6.server.verbs import VerbAPI
from t6.worker.internal_queue import InternalJobQueue


class CaptureTests(DriveReloadFixture):
    def setUp(self) -> None:
        super().setUp()
        self.enterContext(patch.dict(os.environ, {'T6_DRIVE_RECORDING_CHAT_ID': '-900001',
                                                'T6_DRIVE_RECORDING_TOPIC_ID': ''}))
        self.workbook({'SYNTHETIC PROJECT': [HEADER, ['Synthetic first', 'SAMPLE-FIRST'],
                                            ['Synthetic second', 'SAMPLE-SECOND']]})
        self.reload()
        with self.registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES ('t6n_synthetickeeper', 'staff', '[]', '[]', NULL, 0, 1)")
            db.execute("INSERT INTO person_permissions VALUES ('t6n_synthetickeeper', 'drives')")
        self.queue = InternalJobQueue(self.registry)
        self.fake = FakeProvider()
        api = VerbAPI(self.people, self.queue, {'synthetic-gateway': 'gateway', 'synthetic-other': 'worker', 'synthetic-keeper-secret': 't6n_synthetickeeper'}, lambda _: None)
        self.server = make_server(('127.0.0.1', 0), api)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(self.server.shutdown)
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.gateway_down = False
        self.post: dict[str, Any] = dict(chat_id=-900001, message_id=1, sender_telegram_id=900010,
                         posted_at='2026-11-24T10:30:00+08:00', text='OUT Synthetic borrower',
                         photo=base64.b64encode(photo(str(self.folder)).data).decode())

    def api(self, verb: str, secret: str = 'synthetic-gateway', **values: Any) -> dict[str, Any]:
        request = Request(f'http://127.0.0.1:{self.server.server_port}/v1/drive/{verb.replace(" ", "/")}',
                          data=json.dumps(values).encode(), headers={'Authorization': 'Bearer ' + secret})
        try:
            response = urlopen(request)
        except HTTPError as error:
            response = error
        with response:
            result = json.load(response)
        self.assertIn('outcome', result, result)
        return dict(result)

    def test_in_with_place_sets_the_drive_place_and_out_clears_it(self) -> None:
        drive = self.drives()[0]['id']
        for number, command, expected, holder in (
                (1, 'IN SHELF', 'SHELF', None),
                (2, 'OUT Synthetic borrower', None, 'Synthetic borrower'),
                (3, 'IN CABINET', 'CABINET', None),
                (4, 'IN', 'CABINET', None)):
            self.api('capture', **{**self.post, 'message_id': number, 'text': command,
                                   'posted_at': f'2026-11-24T{10 + number}:30:00+08:00'})
            self.resolve([{'label': 'Synthetic first', 'serial': 'SAMPLE-FIRST', 'confidence': 0.9}])
            listed = next(d for d in self.api('list')['result']['drives'] if d['id'] == drive)
            detail = self.api('get', drive=drive)['result']['drive']
            for item in (listed, detail):
                self.assertEqual((item['place'], item['office_location'], item['holder']), (expected, expected, holder))

    def test_capture_stores_first_and_repeated_delivery_queues_nothing_new(self) -> None:
        first = self.api('capture', **self.post)
        self.assertEqual(first['outcome'], 'done', first)
        capture = first['result']['capture']
        self.assertEqual(self.api('capture', **{**self.post, 'text': 'IN'})['result']['capture'], capture)
        result = self.api('capture result', capture=capture)['result']
        self.assertEqual(result['state'], 'pending')
        self.assertTrue(result['practice'])
        self.assertEqual(result['lines'], [])
        self.assertEqual(len(self.queue.find('drive.capture.resolve')), 1)
        self.assertEqual(self.fake.calls, [])
        self.assertEqual([d['status'] for d in self.drives()], ['in custody', 'in custody'])

    def resolve(self, readings: list[dict[str, Any]], choice: str = "match") -> None:
        from t6.drives.capture import CaptureResolver
        from t6.worker import Worker
        from t6.worker.internal_queue import InternalQueueClient
        with self.registry._transaction() as db:  # Jev runs only on a first resolution, never on a follow-up answer or a caption edit
            first = any(not json.loads(row[0]).get('answered') and not json.loads(row[0]).get('edited') for row in db.execute("SELECT data FROM drive_captures") if json.loads(row[0])['state'] == 'pending')
        if first:
            self.fake.decisions({'model': 'synthetic-jev', 'answers': {'c0': {
                'type': 'choice', 'choice': choice,
                'probabilities': {'match': 1 if choice == 'match' else 0, 'maybe': 0, 'no': 1 if choice == 'no' else 0}}}})
        if choice != 'no':
            self.fake.reply(json.dumps({'drives': readings}))
        resolver = CaptureResolver(self.registry, self.fake, decisions=self.fake)
        worker = Worker(InternalQueueClient(self.queue), 'main', {'drive.capture.resolve': resolver.handle})
        self.assertTrue(worker.run_once())

    def test_not_transmittal_is_ignored_and_silent_without_retained_content(self) -> None:
        from t6.verbs import VERBS, invoke
        capture = self.api('capture', **{**self.post, 'text': 'Synthetic lunch photo'})['result']['capture']
        self.resolve([{'label': 'Synthetic first', 'serial': 'SAMPLE-FIRST', 'confidence': 0.9}], 'no')
        result = self.api('capture result', capture=capture)['result']
        self.assertEqual(result['state'], 'ignored')
        self.assertEqual(result['lines'], [])
        self.assertEqual(result['differences'], [])
        self.assertIsNone(result['message'])
        self.assertIsNone(result['photo_reference'])
        self.assertEqual(self.call('exceptions').result['captures'], [])
        entry = next(v for v in VERBS if (v.noun, v.verb) == ('movement', 'list'))
        self.assertEqual(invoke(entry, entry.input_model(), actor='synthetic-keeper', people=self.people,
                                open_notion=lambda _: None).result['movements'], [])  # type: ignore[union-attr]
        self.assertEqual([d['status'] for d in self.drives()], ['in custody', 'in custody'])
        # Privacy contract: inspect durable evidence because the result never exposes post content.
        with self.registry._transaction() as db:
            row = db.execute('SELECT data, photo FROM drive_captures WHERE id=?', (capture,)).fetchone()
        self.assertNotIn('Synthetic lunch photo', row[0])
        self.assertNotIn('text', json.loads(row[0]))
        self.assertEqual(row[1], b'')
        self.assertEqual(self.api('capture', **self.post)['result'], result)
        self.assertEqual(self.api('capture', capture=capture, text='Synthetic first')['result'], result)
        self.assertEqual(self.api('capture undo', capture=capture)['result'], result)
        self.assertEqual(len(self.queue.find('drive.capture.resolve')), 1)

    def test_jev_errors_keep_posts_for_resolution(self) -> None:
        from t6.drives.capture import CaptureResolver
        from t6.llm.transport import DecisionsRequest
        from t6.worker import Worker
        from t6.worker.internal_queue import InternalQueueClient
        for number, failure in enumerate(('timeout', 'api_error', 'malformed', 'policy'), 10):
            for matched in (True, False):
                with self.subTest(failure=failure, matched=matched):
                    capture = self.api('capture', **{**self.post, 'message_id': number * 2 + matched})['result']['capture']
                    readings = [{'label': 'Synthetic first', 'serial': 'SAMPLE-FIRST', 'confidence': 0.9}] if matched else []
                    self.fake.reply(json.dumps({'drives': readings}))
                    decisions = FakeProvider(retention='unknown' if failure == 'policy' else 'zdr_enforced')
                    if failure == 'timeout':
                        decisions.timeout()
                    elif failure == 'api_error':
                        decisions.api_error()
                    else:
                        decisions.decisions({})
                    resolver = CaptureResolver(self.registry, self.fake, decisions=decisions)
                    worker = Worker(InternalQueueClient(self.queue), 'main', {'drive.capture.resolve': resolver.handle})
                    self.assertTrue(worker.run_once())
                    result = self.api('capture result', capture=capture)['result']
                    self.assertEqual(result['state'], 'resolved' if matched else 'exception')
                    self.assertEqual(result['photo_reference'], capture)
                    if failure != 'policy':
                        request = decisions.calls[0].request
                        self.assertIsInstance(request, DecisionsRequest)
                        assert isinstance(request, DecisionsRequest)
                        self.assertEqual(request.state, {
                            'text': 'OUT Synthetic borrower', 'has_photo': 'true',
                        })

    def test_two_photo_drives_make_two_movements_and_one_shipment(self) -> None:
        from t6.verbs import VERBS, invoke
        capture = self.api('capture', **self.post)['result']['capture']
        self.resolve([{'label': label, 'serial': None, 'confidence': 0.99}
                      for label in ('Synthetic first', 'Synthetic second')])
        result = self.api('capture result', capture=capture)['result']
        self.assertEqual(result['state'], 'resolved')
        self.assertEqual(result['lines'], [dict(label=label, direction='OUT', person='Synthetic borrower')
                                          for label in ('Synthetic first', 'Synthetic second')])
        entry = next(v for v in VERBS if (v.noun, v.verb) == ('movement', 'list'))
        rows = invoke(entry, entry.input_model(), actor='synthetic-keeper', people=self.people,
                      open_notion=lambda _: None).result['movements']  # type: ignore[union-attr]
        self.assertEqual(len(rows), 2)
        self.assertEqual({m['capture'] for m in rows}, {capture})
        self.assertEqual({m['on'] for m in rows}, {'2026-11-24'})
        self.assertEqual({m['recorder_telegram_id'] for m in rows}, {900010})
        self.assertEqual({m['recorder'] for m in rows}, {None})
        self.assertEqual({m['project']['text'] for m in rows}, {'SYNTHETIC PROJECT'})
        shipments = self.call('shipments').result['shipments']
        self.assertEqual(len(shipments), 1)
        self.assertEqual(shipments[0]['drive_count'], 2)
        self.assertEqual({m['shipment_id'] for m in rows}, {shipments[0]['shipment_id']})
        self.assertEqual({m['posted_at'] for m in rows}, {self.post['posted_at']})
        self.assertEqual([d['status'] for d in self.drives()], ['released', 'released'])
        self.assertEqual(len(self.fake.calls), 2)

    def test_photo_label_wins_over_typed_label_and_notes_difference(self) -> None:
        capture = self.api('capture', **{**self.post, 'text': 'OUT Synthetic borrower\nSynthetic second'})['result']['capture']
        self.resolve([{'label': 'Synthetic first', 'serial': None, 'confidence': 0.9}])
        result = self.api('capture result', capture=capture)['result']
        self.assertEqual(result['lines'], [dict(label='Synthetic first', direction='OUT', person='Synthetic borrower')])
        self.assertEqual(len(result['differences']), 1)
        self.assertIn('Synthetic second', result['differences'][0])
        self.assertIn('Synthetic first', result['differences'][0])
        self.assertEqual({d['label']: d['status'] for d in self.drives()},
                         {'Synthetic first': 'released', 'Synthetic second': 'in custody'})

    def test_tolerant_captions_resolve_direction_place_and_person(self) -> None:
        drive = self.drives()[0]['id']
        reading = [{'label': 'Synthetic first', 'serial': 'SAMPLE-FIRST', 'confidence': 0.9}]
        for number, (text, direction, person, place) in enumerate((
                ('IN: SHELF', 'IN', None, 'SHELF'), ('in - table', 'IN', None, 'TABLE'),
                ('OUT: Marco', 'OUT', 'Marco', None), ('Out-Marco', 'OUT', 'Marco', None),
                ('in', 'IN', None, None), ('IN\nSAMPLE-FIRST', 'IN', None, None)), 1):
            with self.subTest(text=text):
                capture = self.api('capture', **{**self.post, 'message_id': number, 'text': text,
                                                 'posted_at': f'2026-11-24T{10 + number}:30:00+08:00'})['result']['capture']
                self.resolve(reading)
                result = self.api('capture result', capture=capture)['result']
                self.assertEqual(result['state'], 'resolved')
                self.assertEqual(result['lines'][0]['direction'], direction)
                if direction == 'OUT':
                    self.assertEqual(result['lines'][0]['person'], person)
                self.assertEqual(self.api('get', drive=drive)['result']['drive']['place'], place if direction == 'IN' else None)

    def read_caption(self, text: str, message_id: int = 1) -> dict[str, Any]:
        return dict(self.api('capture', **{**self.post, 'message_id': message_id, 'text': text})['result'])

    def test_caption_habit_reads_items_counts_recipient_and_tags(self) -> None:
        for number, (text, direction, items, counts, recipient, tags) in enumerate((
                ('OUT: SYN7 D21\nR: Marco', 'OUT', ['SYN7 D21'], [], 'Marco', []),
                ('OUT SYN7 D21\nSYN7 D22\nR: Marco @synthetic_pm', 'OUT', ['SYN7 D21', 'SYN7 D22'], [], 'Marco', ['synthetic_pm']),
                ('in - RMX D07 (Master 02 and 03)', 'IN', ['RMX D07 (Master 02 and 03)'], [], None, []),
                ('IN:\nLSB\n48 FD @synthetic_pm @synthetic_two', 'IN', ['LSB'], [{'count': 48, 'kind': 'FD'}], None, ['synthetic_pm', 'synthetic_two']),
                ('OUT\n50 Portable drives\nR: Synthetic Studio', 'OUT', [], [{'count': 50, 'kind': 'Portable drives'}], 'Synthetic Studio', []),
                ('Out - SYN HD #97\n14 SYN D03\nr: Marco', 'OUT', ['SYN HD #97', '14 SYN D03'], [], 'Marco', []),
                ('IN SYN9 D01\n\n  SYN9 D02  \n12 FD\n20 Flash drives', 'IN', ['SYN9 D01', 'SYN9 D02'],
                 [{'count': 12, 'kind': 'FD'}, {'count': 20, 'kind': 'Flash drives'}], None, []))):
            with self.subTest(text=text):
                result = self.read_caption(text, number)
                self.assertEqual(result['items'], [dict(text=t, state='open') for t in items])
                self.assertEqual((result['counts'], result['recipient'], result['tags']), (counts, recipient, tags))
                self.assertEqual((result['grade'], result['problem']), ('complete', None))

    def test_caption_without_direction_or_recipient_is_a_problem(self) -> None:
        for number, (text, problem) in enumerate((
                ('SYN7 D21\nR: Marco', 'missing_direction'), ('SYN7 D21', 'missing_direction'),
                ('OUT SYN7 D21', 'missing_recipient'), ('OUT\n48 FD', 'missing_recipient'),
                ('OUT', 'missing_recipient')), 1):
            with self.subTest(text=text):
                result = self.read_caption(text, number)
                self.assertEqual((result['grade'], result['problem']), ('problem', problem))

    def test_caption_without_an_item_or_count_line_is_thin_and_in_needs_no_recipient(self) -> None:
        for number, text in enumerate(('IN', 'in:', 'OUT:\nR: Marco @synthetic_pm'), 1):
            with self.subTest(text=text):
                result = self.read_caption(text, number)
                self.assertEqual((result['items'], result['counts'], result['grade'], result['problem']), ([], [], 'thin', None))
        self.assertEqual(self.read_caption('IN SYN7 D21', 10)['grade'], 'complete')

    def test_editing_a_caption_reads_it_again_and_can_change_the_grade(self) -> None:
        capture = self.read_caption('SYN7 D21\nR: Marco')['capture']
        self.assertEqual(self.api('capture result', capture=capture)['result']['problem'], 'missing_direction')
        for text, grade, problem in (('OUT SYN7 D21', 'problem', 'missing_recipient'), ('OUT SYN7 D21\nR: Marco', 'complete', None)):
            edited = self.api('capture', capture=capture, text=text, edit=True)['result']
            self.assertEqual((edited['grade'], edited['problem']), (grade, problem))
            self.assertEqual(self.api('capture result', capture=capture)['result']['grade'], grade)

    def test_the_r_line_names_the_person_an_out_is_released_to(self) -> None:
        capture = self.read_caption('OUT SYN7 D21\nR: Marco')['capture']
        self.resolve([{'label': 'Synthetic first', 'serial': None, 'confidence': 0.9}])
        result = self.api('capture result', capture=capture)['result']
        self.assertEqual((result['state'], result['lines'][0]['person']), ('resolved', 'Marco'))

    def test_an_ignored_capture_keeps_no_caption_reading(self) -> None:
        capture = self.read_caption('OUT SYN7 D21\nR: Marco')['capture']
        self.resolve([], 'no')
        result = self.api('capture result', capture=capture)['result']
        self.assertEqual((result['state'], result['items'], result['grade'], result['problem']), ('ignored', [], None, None))

    def test_incident_caption_with_readable_label_resolves_as_in(self) -> None:
        capture = self.api('capture', **{**self.post, 'text': 'IN: Remote Master 3 Day 8'})['result']['capture']
        self.resolve([{'label': 'Synthetic first', 'serial': None, 'confidence': 0.9}])
        result = self.api('capture result', capture=capture)['result']
        self.assertEqual(result['state'], 'resolved')
        self.assertEqual([line['direction'] for line in result['lines']], ['IN'])
        self.assertEqual([d['status'] for d in self.drives()], ['in custody', 'in custody'])

    def test_missing_direction_asks_once_and_a_direction_answer_resolves(self) -> None:
        reading = [{'label': 'Synthetic first', 'serial': None, 'confidence': 0.9}]
        for number, (answer, direction) in enumerate((('OUT Angel', 'OUT'), ('in: shelf', 'IN')), 1):
            with self.subTest(answer=answer):
                capture = self.api('capture', **{**self.post, 'message_id': number, 'text': 'Remote Master 3 Day 8'})['result']['capture']
                self.resolve(reading)
                result = self.api('capture result', capture=capture)['result']
                self.assertEqual((result['state'], result['reason'], result['may_ask']), ('exception', 'missing_direction', True))
                self.assertEqual(self.api('capture', capture=capture, text=answer)['result']['state'], 'pending')
                self.resolve(reading)
                result = self.api('capture result', capture=capture)['result']
                self.assertEqual((result['state'], result['reason'], result['may_ask']), ('resolved', None, False))
                self.assertEqual(result['lines'][0]['direction'], direction)

    def test_reasons_and_no_second_question_after_a_failed_answer(self) -> None:
        capture = self.api('capture', **self.post)['result']['capture']
        self.resolve([])
        result = self.api('capture result', capture=capture)['result']
        self.assertEqual((result['reason'], result['may_ask']), ('unreadable_label', True))
        self.api('capture', capture=capture, text='NO-SUCH-DRIVE')
        self.resolve([])
        result = self.api('capture result', capture=capture)['result']
        self.assertEqual((result['state'], result['reason'], result['may_ask']), ('exception', 'no_drive_match', False))  # a label was typed but matches no Drive
        other = self.api('capture', **{**self.post, 'message_id': 2})['result']['capture']
        self.resolve([{'label': 'Unknown label', 'serial': None, 'confidence': 0.9}])
        result = self.api('capture result', capture=other)['result']
        self.assertEqual((result['reason'], result['may_ask']), ('no_drive_match', True))
        self.api('capture', capture=other, text='still wrong')
        self.resolve([{'label': 'Unknown label', 'serial': None, 'confidence': 0.9}])
        self.assertFalse(self.api('capture result', capture=other)['result']['may_ask'])

    def test_out_without_person_asks_for_direction_and_a_typed_unmatched_label_is_no_drive_match(self) -> None:
        capture = self.api('capture', **{**self.post, 'text': 'OUT:'})['result']['capture']
        self.resolve([])
        result = self.api('capture result', capture=capture)['result']
        self.assertEqual((result['reason'], result['may_ask']), ('missing_direction', True))
        other = self.api('capture', **{**self.post, 'message_id': 2, 'text': 'IN: SHELF Unknown typed'})['result']['capture']
        self.resolve([])
        self.assertEqual(self.api('capture result', capture=other)['result']['reason'], 'no_drive_match')

    def test_direction_answer_on_a_text_only_capture_is_not_no_photo_again(self) -> None:
        text_only = {k: v for k, v in self.post.items() if k != 'photo'}
        capture = self.api('capture', **text_only)['result']['capture']
        self.resolve([], 'match')
        self.assertEqual(self.api('capture result', capture=capture)['result']['reason'], 'no_photo')
        self.api('capture', capture=capture, text='IN')
        self.resolve([], 'match')
        self.assertEqual(self.api('capture result', capture=capture)['result']['reason'], 'unreadable_label')

    def test_edit_of_an_exception_caption_resolves_it_without_using_the_question(self) -> None:
        reading = [{'label': 'Synthetic first', 'serial': None, 'confidence': 0.9}]
        capture = self.api('capture', **{**self.post, 'text': 'Remote Master 3 Day 8'})['result']['capture']
        self.resolve(reading)
        self.assertEqual(self.api('capture result', capture=capture)['result']['reason'], 'missing_direction')
        edited = self.api('capture', capture=capture, text='OUT Angel', edit=True)['result']
        self.assertEqual(edited['state'], 'pending')
        self.assertEqual(self.api('capture', capture=capture, text='OUT Angel', edit=True)['result'], edited)
        self.assertEqual(len(self.queue.find('drive.capture.resolve')), 2)
        self.resolve(reading)
        result = self.api('capture result', capture=capture)['result']
        self.assertEqual((result['state'], result['lines'][0]['person']), ('resolved', 'Angel'))
        other = self.api('capture', **{**self.post, 'message_id': 2, 'text': 'Remote Master 3 Day 8'})['result']['capture']
        self.resolve([])
        self.api('capture', capture=other, text='Remote Master 3 Day 8 again', edit=True)
        self.resolve([])
        self.assertTrue(self.api('capture result', capture=other)['result']['may_ask'])

    def test_edit_of_a_resolved_undone_or_ignored_capture_changes_nothing_and_needs_the_channel(self) -> None:
        capture = self.api('capture', **self.post)['result']['capture']
        self.resolve([{'label': 'Synthetic first', 'serial': None, 'confidence': 0.9}])
        resolved = self.api('capture result', capture=capture)['result']
        self.assertEqual(self.api('capture', capture=capture, text='IN SHELF', edit=True)['result'], resolved)
        self.api('capture undo', capture=capture)
        undone = self.api('capture result', capture=capture)['result']
        self.assertEqual(self.api('capture', capture=capture, text='IN SHELF', edit=True)['result'], undone)
        self.assertEqual(len(self.queue.find('drive.capture.resolve')), 1)
        with patch.dict(os.environ, {'T6_DRIVE_RECORDING_CHAT_ID': '-900002'}):
            refused = self.api('capture', capture=capture, text='IN SHELF', edit=True)
        self.assertEqual(refused['rule'], 'recording_channel')

    def test_undo_is_refused_an_hour_after_resolution_and_the_result_says_when(self) -> None:
        now = [1_800_000_000.0]
        self.registry._clock = lambda: now[0]
        capture = self.api('capture', **self.post)['result']['capture']
        self.assertIsNone(self.api('capture result', capture=capture)['result']['undo_until'])
        self.resolve([{'label': 'Synthetic first', 'serial': None, 'confidence': 0.9}])
        result = self.api('capture result', capture=capture)['result']
        self.assertEqual(result['undo_until'], '2027-01-15T09:00:00+00:00')
        now[0] += 3601
        refused = self.api('capture undo', capture=capture)
        self.assertEqual((refused['outcome'], refused['rule']), ('refused', 'undo_window'), refused)
        self.assertEqual(self.api('capture result', capture=capture)['result']['state'], 'resolved')
        now[0] = 1_800_003_600.0
        undone = self.api('capture undo', capture=capture)['result']
        self.assertEqual((undone['state'], undone['undo_until']), ('undone', None))

    def test_undo_of_a_capture_resolved_before_the_window_existed_is_allowed(self) -> None:
        capture = self.api('capture', **self.post)['result']['capture']
        self.resolve([{'label': 'Synthetic first', 'serial': None, 'confidence': 0.9}])
        with self.registry._transaction() as db:
            data = json.loads(db.execute('SELECT data FROM drive_captures WHERE id=?', (capture,)).fetchone()[0])
            data.pop('resolved_at')
            db.execute('UPDATE drive_captures SET data=? WHERE id=?', (json.dumps(data), capture))
        self.assertIsNone(self.api('capture result', capture=capture)['result']['undo_until'])
        self.assertEqual(self.api('capture undo', capture=capture)['result']['state'], 'undone')

    def test_unreadable_photo_is_exception_and_follow_up_resolves_same_capture(self) -> None:
        capture = self.api('capture', **self.post)['result']['capture']
        self.resolve([])
        result = self.api('capture result', capture=capture)['result']
        self.assertEqual(result['state'], 'exception')
        self.assertEqual(result['message'], 'ask for the label')
        self.assertEqual(result['photo_reference'], capture)
        follow = self.api('capture', capture=capture, text='SAMPLE-FIRST')
        self.assertEqual(follow['outcome'], 'done', follow)
        self.assertEqual(follow['result']['capture'], capture)
        self.assertEqual(follow['result']['state'], 'pending')
        self.resolve([])
        resolved = self.api('capture result', capture=capture)['result']
        self.assertEqual(resolved['state'], 'resolved')
        self.assertEqual(resolved['lines'], [dict(label='Synthetic first', direction='OUT', person='Synthetic borrower')])
        self.assertEqual(self.api('capture', capture=capture, text='SAMPLE-FIRST')['result'], resolved)

    def test_jev_runs_before_the_photo_read_and_a_no_never_reaches_the_image_model(self) -> None:
        from t6.drives.capture import CaptureResolver
        from t6.llm.transport import DecisionsRequest
        from t6.worker import Worker
        from t6.worker.internal_queue import InternalQueueClient
        capture = self.api('capture', **{**self.post, 'text': 'Synthetic lunch photo'})['result']['capture']
        self.fake.decisions({'model': 'synthetic-jev', 'answers': {'c0': {
            'type': 'choice', 'choice': 'no', 'probabilities': {'match': 0, 'maybe': 0, 'no': 1}}}})
        resolver = CaptureResolver(self.registry, self.fake, decisions=self.fake)
        self.assertTrue(Worker(InternalQueueClient(self.queue), 'main', {'drive.capture.resolve': resolver.handle}).run_once())
        (call,) = self.fake.calls  # only Jev was called; the image model never was
        assert isinstance(call.request, DecisionsRequest)
        self.assertEqual(call.request.state, {'text': 'Synthetic lunch photo', 'has_photo': 'true'})
        self.assertEqual(self.api('capture result', capture=capture)['result']['state'], 'ignored')
        with self.registry._transaction() as db:
            row = db.execute('SELECT data, photo FROM drive_captures WHERE id=?', (capture,)).fetchone()
        self.assertEqual((row[1], 'text' in json.loads(row[0])), (b'', False))

    def test_text_only_post_is_accepted_and_becomes_no_photo_exception_without_an_image_read(self) -> None:
        text_only = {k: v for k, v in self.post.items() if k != 'photo'}
        self.assertEqual(self.api('capture', **{**text_only, 'chat_id': -900002})['rule'], 'recording_channel')
        capture = self.api('capture', **text_only)['result']['capture']
        self.resolve([], 'match')
        self.assertEqual(len(self.fake.calls), 1)  # Jev only
        self.assertEqual(self.fake.calls[0].request.state['has_photo'], 'false')  # type: ignore[union-attr]
        result = self.api('capture result', capture=capture)['result']
        self.assertEqual((result['state'], result['reason'], result['may_ask']), ('exception', 'no_photo', False))
        (row,) = self.api('capture list', 'synthetic-keeper-secret')['result']['captures']
        self.assertEqual((row['capture'], row['text'], row['photo']), (capture, 'OUT Synthetic borrower', None))

    def test_text_only_post_ignored_when_jev_says_no_and_empty_post_refused(self) -> None:
        text_only = {k: v for k, v in self.post.items() if k != 'photo'}
        capture = self.api('capture', **{**text_only, 'text': 'Synthetic chatter'})['result']['capture']
        self.resolve([], 'no')
        self.assertEqual(self.api('capture result', capture=capture)['result']['state'], 'ignored')
        self.assertEqual(self.api('capture', **{**text_only, 'message_id': 9, 'text': ''})['rule'], 'input')

    def test_follow_up_answers_and_custodian_picks_skip_jev(self) -> None:
        from t6.drives.capture import CaptureResolver
        from t6.worker import Worker
        from t6.worker.internal_queue import InternalQueueClient
        capture = self.api('capture', **self.post)['result']['capture']
        self.resolve([])
        before = len(self.fake.calls)
        self.api('capture', capture=capture, text='Synthetic first')
        self.fake.reply(json.dumps({'drives': []}))
        decisions = FakeProvider()  # nothing queued: a Jev call would fail the run
        resolver = CaptureResolver(self.registry, self.fake, decisions=decisions)
        self.assertTrue(Worker(InternalQueueClient(self.queue), 'main', {'drive.capture.resolve': resolver.handle}).run_once())
        self.assertEqual(decisions.calls, [])
        self.assertEqual(len(self.fake.calls), before + 1)  # the photo is read again for a label answer
        self.assertEqual(self.api('capture result', capture=capture)['result']['state'], 'resolved')

    def test_caption_edit_never_calls_jev_and_keeps_may_ask(self) -> None:
        from t6.drives.capture import CaptureResolver
        from t6.worker import Worker
        from t6.worker.internal_queue import InternalQueueClient
        capture = self.api('capture', **{**self.post, 'text': 'Remote Master 3 Day 8'})['result']['capture']
        self.resolve([])
        self.assertTrue(self.api('capture result', capture=capture)['result']['may_ask'])
        self.api('capture', capture=capture, text='Remote Master 3 Day 8 again', edit=True)
        self.fake.reply(json.dumps({'drives': []}))
        decisions = FakeProvider()  # nothing queued: a Jev call would fail the run
        resolver = CaptureResolver(self.registry, self.fake, decisions=decisions)
        self.assertTrue(Worker(InternalQueueClient(self.queue), 'main', {'drive.capture.resolve': resolver.handle}).run_once())
        self.assertEqual(decisions.calls, [])
        result = self.api('capture result', capture=capture)['result']
        self.assertEqual((result['state'], result['may_ask']), ('exception', True))

    def test_edit_of_a_text_only_no_photo_capture_stays_no_photo_without_any_read(self) -> None:
        from t6.drives.capture import CaptureResolver
        from t6.worker import Worker
        from t6.worker.internal_queue import InternalQueueClient
        text_only = {k: v for k, v in self.post.items() if k != 'photo'}
        capture = self.api('capture', **text_only)['result']['capture']
        self.resolve([], 'match')
        self.assertEqual(self.api('capture result', capture=capture)['result']['reason'], 'no_photo')
        before = len(self.fake.calls)
        edited = self.api('capture', capture=capture, text='OUT Someone else', edit=True)['result']
        self.assertEqual(edited['state'], 'pending')
        decisions = FakeProvider()
        resolver = CaptureResolver(self.registry, self.fake, decisions=decisions)
        self.assertTrue(Worker(InternalQueueClient(self.queue), 'main', {'drive.capture.resolve': resolver.handle}).run_once())
        self.assertEqual((decisions.calls, len(self.fake.calls)), ([], before))
        result = self.api('capture result', capture=capture)['result']
        self.assertEqual((result['state'], result['reason'], result['may_ask']), ('exception', 'no_photo', False))
        (row,) = self.api('capture list', 'synthetic-keeper-secret')['result']['captures']
        self.assertEqual((row['text'], row['photo']), ('OUT Someone else', None))

    def test_no_jev_provider_keeps_the_post(self) -> None:
        from t6.drives.capture import CaptureResolver
        from t6.worker import Worker
        from t6.worker.internal_queue import InternalQueueClient
        text_only = {k: v for k, v in self.post.items() if k != 'photo'}
        capture = self.api('capture', **text_only)['result']['capture']
        resolver = CaptureResolver(self.registry, self.fake)
        self.assertTrue(Worker(InternalQueueClient(self.queue), 'main', {'drive.capture.resolve': resolver.handle}).run_once())
        self.assertEqual(self.api('capture result', capture=capture)['result']['reason'], 'no_photo')
        self.assertEqual(self.fake.calls, [])

    def test_capture_list_shows_unresolved_captures_with_text_and_photo_access(self) -> None:
        keeper = 'synthetic-keeper-secret'
        photo_bytes = base64.b64decode(self.post['photo'])
        pending = self.api('capture', **self.post)['result']['capture']
        self.assertEqual(self.api('capture list', keeper)['result']['captures'], [])
        self.resolve([])
        second = self.api('capture', **{**self.post, 'message_id': 2, 'posted_at': '2026-11-24T11:30:00+08:00'})['result']['capture']
        self.assertEqual([c['capture'] for c in self.api('capture list', keeper, state='pending')['result']['captures']], [second])
        self.resolve([])
        listed = self.api('capture list', keeper)['result']['captures']
        self.assertEqual([c['capture'] for c in listed], [second, pending])  # newest first
        self.assertEqual({k: listed[1][k] for k in ('state', 'text', 'posted_at', 'sender_telegram_id', 'photo')},
                         {'state': 'exception', 'text': 'OUT Synthetic borrower', 'posted_at': '2026-11-24T10:30:00+08:00',
                          'sender_telegram_id': 900010, 'photo': {'size': len(photo_bytes), 'media_type': 'image/png'}})
        photo = self.api('capture photo', keeper, capture=pending)
        self.assertEqual(photo['outcome'], 'done', photo)
        self.assertEqual((base64.b64decode(photo['result']['photo']), photo['result']['media_type']), (photo_bytes, 'image/png'))
        self.assertEqual(self.api('capture list', keeper, state='resolved')['result']['captures'], [])
        self.assertEqual(self.api('capture photo', keeper, capture='capture_missing')['rule'], 'capture')

    def test_only_a_drive_caller_reads_the_capture_list_or_photo(self) -> None:
        capture = self.api('capture', **self.post)['result']['capture']
        self.resolve([])
        for secret in ('synthetic-gateway', 'synthetic-other'):
            for result in (self.api('capture list', secret), self.api('capture photo', secret, capture=capture)):
                self.assertEqual((result['outcome'], result['rule']), ('refused', 'permission'), (secret, result))
        self.assertEqual(self.api('capture list', 'synthetic-keeper-secret')['outcome'], 'done')

    def test_an_ignored_capture_lists_with_no_text_and_no_photo(self) -> None:
        capture = self.api('capture', **{**self.post, 'text': 'Synthetic lunch photo'})['result']['capture']
        self.resolve([{'label': 'Synthetic first', 'serial': 'SAMPLE-FIRST', 'confidence': 0.9}], 'no')
        (row,) = self.api('capture list', 'synthetic-keeper-secret', state='ignored')['result']['captures']
        self.assertEqual((row['capture'], row['state'], row['text'], row['photo']), (capture, 'ignored', None, None))
        self.assertEqual(self.api('capture list', 'synthetic-keeper-secret')['result']['captures'], [])
        photo = self.api('capture photo', 'synthetic-keeper-secret', capture=capture)
        self.assertEqual((photo['outcome'], photo['rule']), ('refused', 'capture'))

    def test_giving_an_unresolved_capture_a_drive_resolves_it_without_reading_the_photo(self) -> None:
        keeper = 'synthetic-keeper-secret'
        capture = self.api('capture', **self.post)['result']['capture']
        self.resolve([])
        drive = self.drives()[1]['id']
        self.assertEqual([c['capture'] for c in self.call('exceptions').result['captures']], [capture])
        refused = self.api('capture', 'synthetic-gateway', capture=capture, drive=drive)
        self.assertEqual((refused['outcome'], refused['rule']), ('refused', 'permission'))
        self.assertEqual(self.api('capture', keeper, capture=capture, drive='t6d_missing')['rule'], 'drive')
        calls = len(self.fake.calls)
        follow = self.api('capture', keeper, capture=capture, drive=drive)
        self.assertEqual((follow['outcome'], follow['result']['state']), ('done', 'pending'), follow)
        self.assertEqual(self.api('capture', keeper, capture=capture, drive=drive)['result'], follow['result'])
        self.resolve([])
        self.assertEqual(len(self.fake.calls), calls)
        resolved = self.api('capture result', capture=capture)['result']
        self.assertEqual((resolved['state'], resolved['lines']),
                         ('resolved', [dict(label='Synthetic second', direction='OUT', person='Synthetic borrower')]))
        self.assertEqual({d['id']: d['status'] for d in self.drives()}[drive], 'released')
        self.assertEqual(self.call('exceptions').result['captures'], [])
        self.assertEqual(self.api('capture list', keeper)['result']['captures'], [])
        (row,) = self.api('capture list', keeper, state='resolved')['result']['captures']
        self.assertEqual(row['capture'], capture)
        self.assertEqual(self.api('capture', keeper, capture=capture, drive=drive)['result']['state'], 'resolved')

    def test_undo_appends_reversals_and_restores_previous_status_once(self) -> None:
        capture = self.api('capture', **self.post)['result']['capture']
        self.resolve([{'label': 'Synthetic first', 'serial': None, 'confidence': 0.9}])
        drive = self.drives()[0]['id']
        self.assertEqual(self.call('get', drive=drive).result['drive']['status'], 'released')
        undone = self.api('capture undo', capture=capture)
        self.assertEqual(undone['outcome'], 'done', undone)
        self.assertEqual(undone['result']['state'], 'undone')
        events = self.call('get', drive=drive).result['events']
        self.assertEqual([e['kind'] for e in events], ['received', 'released', 'received'])
        self.assertEqual(self.call('get', drive=drive).result['drive']['status'], 'in custody')
        self.assertEqual(self.api('capture undo', capture=capture), undone)
        self.assertEqual(self.call('get', drive=drive).result['events'], events)

    def test_other_chats_and_services_are_refused_before_storage_or_photo_read(self) -> None:
        for chat in (-900002, -900003):
            result = self.api('capture', **{**self.post, 'chat_id': chat})
            self.assertEqual((result['outcome'], result['rule']), ('refused', 'recording_channel'))
        with patch.dict(os.environ, {'T6_DRIVE_RECORDING_CHAT_ID': ''}):
            self.assertEqual(self.api('capture', **self.post)['rule'], 'recording_channel')
        self.assertEqual(self.api('capture', secret='synthetic-other', **self.post)['rule'], 'service')
        self.assertEqual(self.queue.find('drive.capture.resolve'), [])
        self.assertEqual(self.fake.calls, [])

    def test_other_or_missing_topic_is_refused_before_storage_or_photo_read(self) -> None:
        with patch.dict(os.environ, {'T6_DRIVE_RECORDING_TOPIC_ID': '4801'}):
            for values in (self.post, {**self.post, 'topic_id': 4802},
                           {**self.post, 'chat_id': -900002, 'topic_id': 4801}):
                with self.subTest(chat=values['chat_id'], topic=values.get('topic_id')):
                    result = self.api('capture', **values)
                    self.assertEqual((result['outcome'], result.get('rule')), ('refused', 'recording_channel'))
        self.assertEqual(self.queue.find('drive.capture.resolve'), [])
        self.assertEqual(self.call('exceptions').result['captures'], [])
        self.assertEqual(self.fake.calls, [])
        # A refused delivery must leave its Telegram key available for a valid post.
        self.assertEqual(self.api('capture', **self.post)['outcome'], 'done')

    def test_no_open_assignment_lands_on_needs_project_list(self) -> None:
        from t6.verbs import VERBS, invoke
        drive = self.drives()[0]['id']
        values = dict(drive=drive, assignment=1, end='2026-11-23', idempotency_key='synthetic-unassign')
        preview = self.call('unassign', **values)
        self.call('unassign', **values, confirm=preview.token)
        capture = self.api('capture', **self.post)['result']['capture']
        self.resolve([{'label': 'Synthetic first', 'serial': None, 'confidence': 0.9}])
        self.assertEqual(self.api('capture result', capture=capture)['result']['state'], 'resolved')
        entry = next(v for v in VERBS if (v.noun, v.verb) == ('movement', 'list'))
        rows = invoke(entry, entry.input_model.model_validate({'needs_project': True}), actor='synthetic-keeper',
                      people=self.people, open_notion=lambda _: None).result['movements']  # type: ignore[union-attr]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['drive'], drive)
        self.assertIsNone(rows[0]['project'])

    def movements(self, verb: str, **values: Any) -> Any:
        from t6.verbs import VERBS, invoke
        entry = next(v for v in VERBS if (v.noun, v.verb) == ('movement', verb))
        return invoke(entry, entry.input_model.model_validate(values), actor='synthetic-keeper', people=self.people,
                      open_notion=lambda _: None)

    def correct_project(self, movement: str, project: str, key: str) -> dict[str, Any]:
        from t6.verbs import Done
        values = dict(movement=movement, project=project, idempotency_key=key)
        done = self.movements('correct', **values, confirm=self.movements('correct', **values).token)
        self.assertIsInstance(done, Done)
        return dict(done.result['movement'])

    def needs_project_movement(self) -> dict[str, Any]:
        drive = self.drives()[0]['id']
        values = dict(drive=drive, assignment=1, end='2026-11-23', idempotency_key='synthetic-unassign')
        self.call('unassign', **values, confirm=self.call('unassign', **values).token)
        self.api('capture', **self.post)
        self.resolve([{'label': 'Synthetic first', 'serial': None, 'confidence': 0.9}])
        (original,) = self.movements('list', needs_project=True).result['movements']
        return dict(original)

    @staticmethod
    def bare(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """A movement row without its `photo` mark (a corrected row comes from the correct verb, which has none)."""
        return [{k: v for k, v in row.items() if k != 'photo'} for row in rows]

    def test_correcting_the_project_takes_a_movement_off_the_needs_project_list(self) -> None:
        original = self.needs_project_movement()
        registered = self.registry.register(actor='setup', idempotency_key='synthetic-project').project.id  # type: ignore[union-attr]
        corrected = self.correct_project(original['id'], registered, 'synthetic-registered')
        self.assertEqual((corrected['project'], corrected['corrects']), ({'page_id': registered, 'text': None}, original['id']))
        self.assertEqual(self.movements('list', needs_project=True).result['movements'], [])
        self.assertEqual(self.bare(self.movements('list', project=registered).result['movements']), [corrected])
        self.assertEqual(self.bare(self.movements('list', drive=original['drive']).result['movements']), self.bare([original, corrected]))

    def test_correcting_to_a_text_project_keeps_the_text_and_the_original_entry(self) -> None:
        original = self.needs_project_movement()
        corrected = self.correct_project(original['id'], 'SYNTHETIC TEXT PROJECT', 'synthetic-text')
        self.assertEqual(corrected['project'], {'page_id': None, 'text': 'SYNTHETIC TEXT PROJECT'})
        self.assertEqual(self.movements('list', needs_project=True).result['movements'], [])
        self.assertEqual(self.bare(self.movements('list', drive=original['drive']).result['movements']), self.bare([original, corrected]))
        self.assertIsNone(self.movements('list', drive=original['drive']).result['movements'][0]['project'])

    def test_in_uses_sender_as_receiver_and_recorder_and_keeps_place(self) -> None:
        with self.registry._transaction() as db:
            db.execute("UPDATE persons SET telegram_id=900010 WHERE id='synthetic-keeper'")
        capture = self.api('capture', **{**self.post, 'text': 'IN SHELF\nSAMPLE-FIRST'})['result']['capture']
        self.resolve([])
        result = self.api('capture result', capture=capture)['result']
        self.assertEqual(result['lines'], [dict(label='Synthetic first', direction='IN', person='synthetic-keeper')])
        from t6.verbs import VERBS, invoke
        entry = next(v for v in VERBS if (v.noun, v.verb) == ('movement', 'list'))
        row = invoke(entry, entry.input_model(), actor='synthetic-keeper', people=self.people,
                     open_notion=lambda _: None).result['movements'][0]  # type: ignore[union-attr]
        self.assertEqual((row['direction'], row['person'], row['recorder'], row['place']),
                         ('IN', 'synthetic-keeper', 'synthetic-keeper', 'SHELF'))
        self.assertEqual(self.call('get', drive=row['drive']).result['events'][-1]['logged_by']['page_id'], 'synthetic-keeper')

    def test_exceptions_list_and_reload_include_captures(self) -> None:
        capture = self.api('capture', **self.post)['result']['capture']
        self.resolve([])
        unresolved = self.call('exceptions').result['captures']
        self.assertEqual([row['capture'] for row in unresolved], [capture])
        self.assertEqual(unresolved[0]['photo_reference'], capture)
        self.reload('synthetic-wipe-captures')
        self.assertEqual(self.api('capture result', capture=capture)['rule'], 'capture')
        self.assertEqual(self.call('exceptions').result['captures'], [])

    def test_production_worker_configuration_reaches_photo_resolution(self) -> None:
        from t6.worker.__main__ import WorkerSettings, configured_handlers
        from t6.worker.client import HttpTransport
        from t6.worker import Worker
        from t6.worker.internal_queue import InternalQueueClient
        config = self.folder / 'llm.json'
        config.write_text(json.dumps({'llm': {'default': 'synthetic', 'providers': {
            'synthetic': {'base_url': 'https://synthetic.invalid', 'model': 'synthetic',
                          'retention': 'zdr_enforced', 'api_key_env': 'SYNTHETIC_KEY', 'supports_images': True}}}}))
        env = dict(T6_SERVER_URL='https://synthetic.invalid', T6_WORKER_SECRET='synthetic', T6_WORKER_QUEUE='main',
                   T6_API_URL='https://synthetic.invalid', T6_DATABASE=str(self.folder / 't6.sqlite'), T6_LLM_CONFIG=str(config), T6_DRIVE_RECORDING_CHAT_ID='-900001')
        capture = self.api('capture', **self.post)['result']['capture']
        self.fake.reply(json.dumps({'drives': [{'label': 'Synthetic first', 'serial': None, 'confidence': 0.9}]}))
        with patch('t6.llm.config.LLMConfig.provider_for', return_value=self.fake):
            handlers = configured_handlers(env, WorkerSettings.from_env(env), HttpTransport('https://synthetic.invalid'))
            self.assertIn('drive.capture.resolve', handlers.get('main', {}))
            worker = Worker(InternalQueueClient(self.queue), 'main', handlers['main'])
            self.assertTrue(worker.run_once())
        self.assertEqual(self.api('capture result', capture=capture)['result']['state'], 'resolved')

    def test_worker_uses_configured_decisions_transport_to_ignore_post(self) -> None:
        from io import BytesIO
        from t6.worker.__main__ import WorkerSettings, configured_handlers
        from t6.worker.client import HttpTransport
        from t6.worker import Worker
        from t6.worker.internal_queue import InternalQueueClient
        from t6.llm.decisions import DECISIONS_URL, JEV_MODEL
        config = self.folder / 'llm.json'
        config.write_text(json.dumps({'llm': {'default': 'synthetic', 'providers': {
            'synthetic': {'base_url': 'https://synthetic.invalid', 'model': 'synthetic',
                          'retention': 'zdr_enforced', 'api_key_env': 'SYNTHETIC_KEY', 'supports_images': True}}},
            'decisions': {'api_key_env': 'SYNTHETIC_JEV_KEY', 'retention': 'zdr_enforced'}}))
        env = dict(T6_SERVER_URL='https://synthetic.invalid', T6_WORKER_SECRET='synthetic', T6_WORKER_QUEUE='main',
                   T6_API_URL='https://synthetic.invalid', T6_DATABASE=str(self.folder / 't6.sqlite'),
                   T6_LLM_CONFIG=str(config), T6_DRIVE_RECORDING_CHAT_ID='-900001', SYNTHETIC_JEV_KEY='synthetic')
        capture = self.api('capture', **self.post)['result']['capture']
        self.fake.reply('{"drives": []}')
        reply = {'model': JEV_MODEL, 'answers': {'c0': {
            'type': 'choice', 'choice': 'no', 'probabilities': {'match': 0, 'maybe': 0, 'no': 1}}}}
        with patch('t6.llm.config.LLMConfig.provider_for', return_value=self.fake):
            handlers = configured_handlers(env, WorkerSettings.from_env(env), HttpTransport('https://synthetic.invalid'))
        with patch('urllib.request.urlopen', return_value=BytesIO(json.dumps(reply).encode())) as send:
            worker = Worker(InternalQueueClient(self.queue), 'main', handlers['main'])
            self.assertTrue(worker.run_once())
        self.assertEqual(self.api('capture result', capture=capture)['result']['state'], 'ignored')
        request = send.call_args.args[0]
        self.assertEqual(request.full_url, DECISIONS_URL)
        self.assertEqual(json.loads(request.data)['model'], JEV_MODEL)

    def test_multiple_open_assignments_need_project_and_pending_undo_stops_read(self) -> None:
        self.workbook({'SYNTHETIC ALPHA': [HEADER, ['Synthetic first', 'SAMPLE-FIRST']],
                       'SYNTHETIC BETA': [HEADER, ['Synthetic first', 'SAMPLE-FIRST']]})
        self.reload('synthetic-multiple')
        capture = self.api('capture', **self.post)['result']['capture']
        self.resolve([{'label': 'Synthetic first', 'serial': None, 'confidence': 0.9}])
        from t6.verbs import VERBS, invoke
        entry = next(v for v in VERBS if (v.noun, v.verb) == ('movement', 'list'))
        rows = invoke(entry, entry.input_model(needs_project=True), actor='synthetic-keeper',
                      people=self.people, open_notion=lambda _: None).result['movements']  # type: ignore[union-attr]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['capture'], capture)
        pending = self.api('capture', **{**self.post, 'message_id': 2})['result']['capture']
        self.api('capture undo', capture=pending)
        before = len(self.fake.calls)
        self.resolve([])
        self.assertEqual(len(self.fake.calls), before)
        self.assertEqual(self.api('capture result', capture=pending)['result']['state'], 'undone')

    def test_matching_is_exact_and_ignores_suspected_serials(self) -> None:
        values = dict(label='Synthetic uncertain', suspected_serial='SAMPLE-SUSPECTED', idempotency_key='synthetic-uncertain')
        preview = self.call('add', **values)
        self.call('add', **values, confirm=preview.token)
        for message, label, serial in ((2, 'Synthetic fir', None), (3, None, 'SAMPLE-SUSPECTED')):
            with self.subTest(label=label, serial=serial):
                capture = self.api('capture', **{**self.post, 'message_id': message})['result']['capture']
                self.resolve([{'label': label, 'serial': serial, 'confidence': 0.9}])
                self.assertEqual(self.api('capture result', capture=capture)['result']['state'], 'exception')
                follow = self.api('capture', capture=capture, text='Synthetic first')
                self.assertEqual(follow['outcome'], 'done')
                self.resolve([{'label': label, 'serial': serial, 'confidence': 0.9}])
                self.assertEqual(self.api('capture result', capture=capture)['result']['state'], 'resolved')

    def test_resolution_rechecks_channel_before_sending_photo(self) -> None:
        capture = self.api('capture', **self.post)['result']['capture']
        with patch.dict(os.environ, {'T6_DRIVE_RECORDING_CHAT_ID': '-900002'}):
            self.resolve([])
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.api('capture result', capture=capture)['result']['state'], 'pending')

    def test_resolution_rechecks_stored_topic_before_sending_photo(self) -> None:
        with patch.dict(os.environ, {'T6_DRIVE_RECORDING_TOPIC_ID': '4801'}):
            capture = self.api('capture', **{**self.post, 'topic_id': 4801})['result']['capture']
        with patch.dict(os.environ, {'T6_DRIVE_RECORDING_TOPIC_ID': '4802'}):
            self.resolve([])
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.api('capture result', capture=capture)['result']['state'], 'pending')

    def test_follow_up_rechecks_stored_topic(self) -> None:
        with patch.dict(os.environ, {'T6_DRIVE_RECORDING_TOPIC_ID': '4801'}):
            capture = self.api('capture', **{**self.post, 'topic_id': 4801})['result']['capture']
        with patch.dict(os.environ, {'T6_DRIVE_RECORDING_TOPIC_ID': '4802'}):
            result = self.api('capture', capture=capture, text='Synthetic first', topic_id=4802)
        self.assertEqual((result['outcome'], result.get('rule')), ('refused', 'recording_channel'))
        self.assertEqual(len(self.queue.find('drive.capture.resolve')), 1)

    def test_matching_topic_is_kept_for_resolution_through_client_and_cli(self) -> None:
        from contextlib import redirect_stdout
        from io import StringIO
        from t6.cli import main
        from t6.client import Client
        from t6.verbs import Done, DriveCaptureInput
        env = {'T6_API_URL': f'http://127.0.0.1:{self.server.server_port}',
               'T6_API_SECRET': 'synthetic-gateway', 'T6_DRIVE_RECORDING_TOPIC_ID': '4801'}
        with patch.dict(os.environ, env):
            first = Client().drive_capture(DriveCaptureInput.model_validate({**self.post, 'topic_id': 4801}))
            self.assertIsInstance(first, Done)
            args = ['drive', 'capture']
            for key, value in {**self.post, 'topic_id': 4801, 'message_id': 2}.items():
                args.append('--' + key.replace('_', '-') + '=' + str(value))
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(args), 0)
            second = json.loads(output.getvalue())
            self.resolve([])
            self.resolve([])
        assert isinstance(first, Done)
        for capture in (first.result['capture'], second['result']['capture']):
            self.assertEqual(self.api('capture result', capture=capture)['result']['state'], 'exception')
        self.assertEqual(len(self.fake.calls), 4)

    def test_no_configured_topic_admits_posts_with_or_without_a_topic(self) -> None:
        for message, topic in enumerate((None, 4801, 4802), 1):
            with self.subTest(topic=topic):
                result = self.api('capture', **{**self.post, 'message_id': message, 'topic_id': topic})
                self.assertEqual(result['outcome'], 'done', result)
        self.assertEqual(len(self.queue.find('drive.capture.resolve')), 3)

    def test_undo_restores_unknown_when_drive_had_no_previous_events(self) -> None:
        values = dict(label='Synthetic new', serial='SAMPLE-NEW', idempotency_key='synthetic-new')
        preview = self.call('add', **values)
        self.call('add', **values, confirm=preview.token)
        capture = self.api('capture', **self.post)['result']['capture']
        self.resolve([{'label': 'Synthetic new', 'serial': None, 'confidence': 0.9}])
        self.api('capture undo', capture=capture)
        drive = next(d for d in self.drives() if d['serial'] == 'SAMPLE-NEW')
        self.assertEqual(drive['status'], 'unknown')
        self.assertEqual(self.call('get', drive=drive['id']).result['drive']['status'], 'unknown')

    def test_undo_in_restores_release_and_does_not_readd_undone_capture(self) -> None:
        out = self.api('capture', **self.post)['result']['capture']
        self.resolve([{'label': 'Synthetic first', 'serial': None, 'confidence': 0.9}])
        returned = self.api('capture', **{**self.post, 'message_id': 2, 'text': 'IN TABLE',
                                        'posted_at': '2026-11-25T10:00:00+08:00'})['result']['capture']
        self.resolve([{'label': 'Synthetic first', 'serial': None, 'confidence': 0.9}])
        self.api('capture undo', capture=returned)
        self.assertEqual(self.drives()[0]['status'], 'released')
        self.api('capture undo', capture=out)
        self.assertEqual(self.drives()[0]['status'], 'in custody')
        from t6.verbs import VERBS, invoke
        entry = next(v for v in VERBS if (v.noun, v.verb) == ('movement', 'list'))
        self.assertEqual(invoke(entry, entry.input_model(needs_project=True), actor='synthetic-keeper',
                                people=self.people, open_notion=lambda _: None).result['movements'], [])  # type: ignore[union-attr]

    def test_late_delivery_keeps_latest_post_status(self) -> None:
        self.api('capture', **{**self.post, 'message_id': 2, 'text': 'IN',
                              'posted_at': '2026-11-25T10:00:00+08:00'})
        self.resolve([{'label': 'Synthetic first', 'serial': None, 'confidence': 0.9}])
        old = self.api('capture', **self.post)['result']['capture']
        self.resolve([{'label': 'Synthetic first', 'serial': None, 'confidence': 0.9}])
        self.assertEqual(self.drives()[0]['status'], 'in custody')
        self.api('capture undo', capture=old)
        self.assertEqual(self.drives()[0]['status'], 'in custody')

    def test_jpeg_photo_bytes_are_sent_with_their_image_type(self) -> None:
        image = self.folder / 'synthetic.jpg'
        image.write_bytes(b'\xff\xd8\xffsynthetic-jpeg-bytes')
        self.api('capture', **{**self.post, 'photo': base64.b64encode(image.read_bytes()).decode()})
        self.resolve([])
        self.assertEqual(self.fake.calls[1].messages[-1]['image'].media_type.value, 'image/jpeg')

    # The debug topic, against a fake gateway transport (this class is the transport).
    DEBUG_CHAT, DEBUG_TOPIC = -1004352220086, 632

    def send(self, method: str, path: str, headers: Any, body: bytes) -> tuple[int, dict[str, Any]]:
        if self.gateway_down:
            return 500, {'detail': 'down'}
        message = json.loads(body)
        self.sent.append((path, message))
        return 202, {'message_ref': message.get('message_ref', 41)}

    def run_debug(self, enabled: bool = True, readings: str = 'Synthetic first', text: str = 'OUT Synthetic borrower password: hunter2') -> str:
        from t6.drives.capture import CaptureResolver
        from t6.drives.debug import DebugNotifier
        from t6.worker import Worker
        from t6.worker.gateway import GatewayClient
        from t6.worker.internal_queue import InternalQueueClient
        capture = self.api('capture', **{**self.post, 'text': text})['result']['capture']
        self.fake.decisions({'model': 'synthetic-jev', 'answers': {'c0': {'type': 'choice', 'choice': 'match',
                             'probabilities': {'match': 0.9, 'maybe': 0.1, 'no': 0}}}})
        self.fake.reply(json.dumps({'drives': [{'label': readings, 'serial': None, 'confidence': 0.9}]}))
        notifier = DebugNotifier(self.registry, GatewayClient(self, 'secret'), self.DEBUG_CHAT, self.DEBUG_TOPIC, lambda: enabled)
        self.resolver = CaptureResolver(self.registry, self.fake, decisions=self.fake, debug=notifier)
        self.worker = Worker(InternalQueueClient(self.queue), 'main', {'drive.capture.resolve': self.resolver.handle,
                                                                      'drive.capture.debug': self.resolver.refresh_debug})
        self.assertTrue(self.worker.run_once())
        return str(capture)

    def test_one_notify_then_edits_of_the_same_message_with_the_whole_story(self) -> None:
        self.run_debug()
        self.assertEqual([path for path, _ in self.sent][0], '/notify')
        self.assertEqual({path for path, _ in self.sent[1:]}, {'/edit'})
        self.assertEqual(len([1 for path, _ in self.sent if path == '/notify']), 1)
        first, last = self.sent[0][1], self.sent[-1][1]
        self.assertEqual((first['to'], first['topic'], first['buttons']), (self.DEBUG_CHAT, self.DEBUG_TOPIC, []))
        self.assertEqual(last['message_ref'], 41)
        text = last['text']
        for expected in ('✅ resolved', 'Telegram 900010', 'https://t.me/c/900001/1', 'OUT Synthetic borrower', 'match 0.90 maybe 0.10 no 0.00',
                         'synthetic-jev', 'Synthetic first', 'direction OUT, person Synthetic borrower', 'Matched drives: Synthetic first',
                         'State: resolved', 'Undo until:'):
            self.assertIn(expected, text)
        self.assertNotIn('hunter2', text)
        self.assertNotIn('photo', {key for _, message in self.sent for key in message})

    def test_switch_off_sends_nothing(self) -> None:
        capture = self.run_debug(enabled=False)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.api('capture result', capture=capture)['result']['state'], 'resolved')

    def test_a_failing_gateway_never_changes_the_capture(self) -> None:
        self.gateway_down = True
        with self.assertLogs('t6.drives.debug', 'ERROR'):
            capture = self.run_debug()
        self.assertEqual(self.api('capture result', capture=capture)['result']['state'], 'resolved')

    def test_undo_through_the_api_queues_one_job_that_refreshes_the_message(self) -> None:
        capture = self.run_debug()
        before = len(self.sent)
        self.api('capture undo', capture=capture)
        self.api('capture undo', capture=capture)
        self.assertEqual(len(self.queue.find('drive.capture.debug')), 1)
        self.assertTrue(self.worker.run_once())
        self.assertEqual(len(self.sent), before + 1)
        self.assertIn('⚠️ undone', self.sent[-1][1]['text'])

    def test_unreadable_photo_and_a_failed_jev_show_the_exception_and_the_error(self) -> None:
        from t6.drives.capture import CaptureResolver
        from t6.drives.debug import DebugNotifier
        from t6.llm.testing import FakeProvider
        from t6.worker import Worker
        from t6.worker.gateway import GatewayClient
        from t6.worker.internal_queue import InternalQueueClient
        self.api('capture', **self.post)
        jev = FakeProvider()
        jev.timeout()
        self.fake.reply(json.dumps({'drives': []}))
        notifier = DebugNotifier(self.registry, GatewayClient(self, 'secret'), self.DEBUG_CHAT, None, lambda: True)
        resolver = CaptureResolver(self.registry, self.fake, decisions=jev, debug=notifier)
        Worker(InternalQueueClient(self.queue), 'main', {'drive.capture.resolve': resolver.handle}).run_once()
        self.assertIn('⛔ error', self.sent[-1][1]['text'])
        self.assertIn('Jev failed: timeout', self.sent[-1][1]['text'])
        self.assertIn('reason unreadable_label', self.sent[-1][1]['text'])
        self.assertNotIn('topic', self.sent[0][1])

    def test_switch_defaults_to_practice_mode_and_needs_a_chat(self) -> None:
        from t6.worker.__main__ import debug_notifier
        env = {'T6_GATEWAY_URL': 'https://synthetic.invalid', 'T6_GATEWAY_SECRET': 's', 'T6_DRIVE_DEBUG_CHAT_ID': '-1004352220086'}
        notifier = debug_notifier(env, self.registry)
        assert notifier is not None
        self.assertTrue(notifier.enabled())
        with self.registry._transaction() as db:
            db.execute('UPDATE drive_practice SET practice=0 WHERE id=1')
        self.assertFalse(notifier.enabled())
        on = debug_notifier({**env, 'T6_DRIVE_DEBUG': 'on'}, self.registry)
        assert on is not None
        self.assertTrue(on.enabled())
        self.assertIsNone(debug_notifier({**env, 'T6_DRIVE_DEBUG': 'off'}, self.registry))
        self.assertIsNone(debug_notifier({k: v for k, v in env.items() if k != 'T6_DRIVE_DEBUG_CHAT_ID'}, self.registry))
