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
        api = VerbAPI(self.people, self.queue, {'synthetic-gateway': 'gateway', 'synthetic-other': 'worker', 'synthetic-keeper-secret': 't6n_synthetickeeper'}, lambda _: None,
                      notion_source=self.notion_source)
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

    def resolve(self, choice: str = "match") -> None:
        from t6.drives.capture import CaptureResolver
        from t6.worker import Worker
        from t6.worker.internal_queue import InternalQueueClient
        with self.registry._transaction() as db:  # Jev runs only on a first resolution, never on a follow-up answer or a caption edit
            first = any(not json.loads(row[0]).get('answered') and not json.loads(row[0]).get('edited') for row in db.execute("SELECT data FROM drive_captures") if json.loads(row[0])['state'] == 'pending')
        if first:
            self.fake.decisions({'model': 'synthetic-jev', 'answers': {'c0': {
                'type': 'choice', 'choice': choice,
                'probabilities': {'match': 1 if choice == 'match' else 0, 'maybe': 0, 'no': 1 if choice == 'no' else 0}}}})
        resolver = CaptureResolver(self.registry, self.fake, decisions=self.fake)
        worker = Worker(InternalQueueClient(self.queue), 'main', {'drive.capture.resolve': resolver.handle})
        self.assertTrue(worker.run_once())

    def test_not_transmittal_is_ignored_and_silent_without_retained_content(self) -> None:
        from t6.verbs import VERBS, invoke
        capture = self.api('capture', **{**self.post, 'text': 'Synthetic lunch photo'})['result']['capture']
        self.resolve('no')
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
            with self.subTest(failure=failure):
                capture = self.api('capture', **{**self.post, 'message_id': number})['result']['capture']
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
                self.assertEqual(result['state'], 'resolved')
                self.assertEqual(result['photo_reference'], capture)
                if failure != 'policy':
                    request = decisions.calls[0].request
                    self.assertIsInstance(request, DecisionsRequest)
                    assert isinstance(request, DecisionsRequest)
                    self.assertEqual(request.state, {
                        'text': 'OUT Synthetic borrower', 'has_photo': 'true',
                    })

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

    def test_an_ignored_capture_keeps_no_caption_reading(self) -> None:
        capture = self.read_caption('OUT SYN7 D21\nR: Marco')['capture']
        self.resolve('no')
        result = self.api('capture result', capture=capture)['result']
        self.assertEqual((result['state'], result['items'], result['grade'], result['problem']), ('ignored', [], None, None))

    def test_edit_of_a_resolved_undone_or_ignored_capture_changes_nothing_and_needs_the_channel(self) -> None:
        capture = self.api('capture', **{**self.post, 'text': 'IN Synthetic first'})['result']['capture']
        self.resolve()
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
        self.resolve()
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
        self.resolve()
        with self.registry._transaction() as db:
            data = json.loads(db.execute('SELECT data FROM drive_captures WHERE id=?', (capture,)).fetchone()[0])
            data.pop('resolved_at')
            db.execute('UPDATE drive_captures SET data=? WHERE id=?', (json.dumps(data), capture))
        self.assertIsNone(self.api('capture result', capture=capture)['result']['undo_until'])
        self.assertEqual(self.api('capture undo', capture=capture)['result']['state'], 'undone')

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

    def test_text_only_post_is_accepted_and_kept_for_the_inbox_without_an_image_read(self) -> None:
        text_only = {k: v for k, v in self.post.items() if k != 'photo'}
        self.assertEqual(self.api('capture', **{**text_only, 'chat_id': -900002})['rule'], 'recording_channel')
        capture = self.api('capture', **{**text_only, 'text': 'OUT Synthetic first\nR: Marco'})['result']['capture']
        self.resolve('match')
        self.assertEqual(len(self.fake.calls), 1)  # Jev only
        self.assertEqual(self.fake.calls[0].request.state['has_photo'], 'false')  # type: ignore[union-attr]
        result = self.api('capture result', capture=capture)['result']
        self.assertEqual((result['state'], result['reason'], result['may_ask']), ('resolved', None, False))
        self.assertEqual([(i['capture'], i['text'], i['photo_reference']) for i in self.inbox()], [(capture, 'Synthetic first', None)])
        (row,) = self.api('capture list', 'synthetic-keeper-secret', state='resolved')['result']['captures']
        self.assertEqual((row['capture'], row['text'], row['photo']), (capture, 'OUT Synthetic first\nR: Marco', None))

    def test_text_only_post_ignored_when_jev_says_no_and_empty_post_refused(self) -> None:
        text_only = {k: v for k, v in self.post.items() if k != 'photo'}
        capture = self.api('capture', **{**text_only, 'text': 'Synthetic chatter'})['result']['capture']
        self.resolve('no')
        self.assertEqual(self.api('capture result', capture=capture)['result']['state'], 'ignored')
        self.assertEqual(self.api('capture', **{**text_only, 'message_id': 9, 'text': ''})['rule'], 'input')

    def test_caption_edit_never_calls_jev(self) -> None:
        from t6.drives.capture import CaptureResolver
        from t6.worker import Worker
        from t6.worker.internal_queue import InternalQueueClient
        capture = self.api('capture', **{**self.post, 'text': 'Remote Master 3 Day 8'})['result']['capture']
        self.resolve()
        self.assertEqual(self.api('capture result', capture=capture)['result']['problem'], 'missing_direction')
        self.api('capture', capture=capture, text='IN Remote Master 3 Day 8', edit=True)
        decisions = FakeProvider()  # nothing queued: a Jev call would fail the run
        resolver = CaptureResolver(self.registry, self.fake, decisions=decisions)
        self.assertTrue(Worker(InternalQueueClient(self.queue), 'main', {'drive.capture.resolve': resolver.handle}).run_once())
        self.assertEqual(decisions.calls, [])
        result = self.api('capture result', capture=capture)['result']
        self.assertEqual((result['state'], result['problem'], [i['text'] for i in result['items']]), ('resolved', None, ['Remote Master 3 Day 8']))

    def test_no_jev_provider_keeps_the_post(self) -> None:
        from t6.drives.capture import CaptureResolver
        from t6.worker import Worker
        from t6.worker.internal_queue import InternalQueueClient
        text_only = {k: v for k, v in self.post.items() if k != 'photo'}
        capture = self.api('capture', **text_only)['result']['capture']
        resolver = CaptureResolver(self.registry, self.fake)
        self.assertTrue(Worker(InternalQueueClient(self.queue), 'main', {'drive.capture.resolve': resolver.handle}).run_once())
        self.assertEqual(self.api('capture result', capture=capture)['result']['state'], 'resolved')
        self.assertEqual(self.fake.calls, [])

    def test_capture_list_shows_unresolved_captures_with_text_and_photo_access(self) -> None:
        keeper = 'synthetic-keeper-secret'
        photo_bytes = base64.b64decode(self.post['photo'])
        pending = self.api('capture', **self.post)['result']['capture']
        self.assertEqual(self.api('capture list', keeper, state='resolved')['result']['captures'], [])
        self.resolve()
        second = self.api('capture', **{**self.post, 'message_id': 2, 'posted_at': '2026-11-24T11:30:00+08:00'})['result']['capture']
        self.assertEqual([c['capture'] for c in self.api('capture list', keeper, state='pending')['result']['captures']], [second])
        self.resolve()
        listed = self.api('capture list', keeper, state='resolved')['result']['captures']
        self.assertEqual([c['capture'] for c in listed], [second, pending])  # newest first
        self.assertEqual({k: listed[1][k] for k in ('state', 'text', 'posted_at', 'sender_telegram_id', 'photo')},
                         {'state': 'resolved', 'text': 'OUT Synthetic borrower', 'posted_at': '2026-11-24T10:30:00+08:00',
                          'sender_telegram_id': 900010, 'photo': {'size': len(photo_bytes), 'media_type': 'image/png'}})
        photo = self.api('capture photo', keeper, capture=pending)
        self.assertEqual(photo['outcome'], 'done', photo)
        self.assertEqual((base64.b64decode(photo['result']['photo']), photo['result']['media_type']), (photo_bytes, 'image/png'))
        self.assertEqual(self.api('capture list', keeper)['result']['captures'], [])
        self.assertEqual(self.api('capture photo', keeper, capture='capture_missing')['rule'], 'capture')

    def test_only_a_drive_caller_reads_the_capture_list_or_photo(self) -> None:
        capture = self.api('capture', **self.post)['result']['capture']
        self.resolve()
        for secret in ('synthetic-gateway', 'synthetic-other'):
            for result in (self.api('capture list', secret), self.api('capture photo', secret, capture=capture)):
                self.assertEqual((result['outcome'], result['rule']), ('refused', 'permission'), (secret, result))
        self.assertEqual(self.api('capture list', 'synthetic-keeper-secret')['outcome'], 'done')

    def test_an_ignored_capture_lists_with_no_text_and_no_photo(self) -> None:
        capture = self.api('capture', **{**self.post, 'text': 'Synthetic lunch photo'})['result']['capture']
        self.resolve('no')
        (row,) = self.api('capture list', 'synthetic-keeper-secret', state='ignored')['result']['captures']
        self.assertEqual((row['capture'], row['state'], row['text'], row['photo']), (capture, 'ignored', None, None))
        self.assertEqual(self.api('capture list', 'synthetic-keeper-secret')['result']['captures'], [])
        photo = self.api('capture photo', 'synthetic-keeper-secret', capture=capture)
        self.assertEqual((photo['outcome'], photo['rule']), ('refused', 'capture'))

    def test_undo_appends_reversals_and_restores_previous_status_once(self) -> None:
        capture = self.settle()
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
        capture = self.settle()
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
        self.settle()
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

    def test_reload_wipes_captures_and_their_inbox_items(self) -> None:
        capture = self.kept()
        self.assertEqual([i['capture'] for i in self.inbox()], [capture, capture])
        self.reload('synthetic-wipe-captures')
        self.assertEqual(self.api('capture result', capture=capture)['rule'], 'capture')
        self.assertEqual(self.inbox(), [])

    def test_production_worker_configuration_reaches_capture_resolution(self) -> None:
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

    def test_multiple_open_assignments_need_project_and_pending_undo_stops_resolution(self) -> None:
        self.workbook({'SYNTHETIC ALPHA': [HEADER, ['Synthetic first', 'SAMPLE-FIRST']],
                       'SYNTHETIC BETA': [HEADER, ['Synthetic first', 'SAMPLE-FIRST']]})
        self.reload('synthetic-multiple')
        capture = self.settle()
        from t6.verbs import VERBS, invoke
        entry = next(v for v in VERBS if (v.noun, v.verb) == ('movement', 'list'))
        rows = invoke(entry, entry.input_model(needs_project=True), actor='synthetic-keeper',
                      people=self.people, open_notion=lambda _: None).result['movements']  # type: ignore[union-attr]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['capture'], capture)
        pending = self.api('capture', **{**self.post, 'message_id': 2})['result']['capture']
        self.api('capture undo', capture=pending)
        before = len(self.fake.calls)
        self.resolve()
        self.assertEqual(len(self.fake.calls), before)
        self.assertEqual(self.api('capture result', capture=pending)['result']['state'], 'undone')

    def test_a_typed_drive_is_matched_exactly_and_never_by_a_suspected_serial(self) -> None:
        values = dict(label='Synthetic uncertain', suspected_serial='SAMPLE-SUSPECTED', idempotency_key='synthetic-uncertain')
        preview = self.call('add', **values)
        self.call('add', **values, confirm=preview.token)
        capture = self.kept('IN\nSynthetic first')
        for typed in ('Synthetic fir', 'SAMPLE-SUSPECTED'):
            with self.subTest(typed=typed):
                self.assertEqual(self.approve(capture, 0, 'drive', typed=typed)['rule'], 'drive')
        self.assertEqual(self.approve(capture, 0, 'drive', typed='Synthetic first')['result']['state'], 'approved')

    def test_resolution_rechecks_channel_before_sending_photo(self) -> None:
        capture = self.api('capture', **self.post)['result']['capture']
        with patch.dict(os.environ, {'T6_DRIVE_RECORDING_CHAT_ID': '-900002'}):
            self.resolve()
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.api('capture result', capture=capture)['result']['state'], 'pending')

    def test_resolution_rechecks_stored_topic_before_sending_photo(self) -> None:
        with patch.dict(os.environ, {'T6_DRIVE_RECORDING_TOPIC_ID': '4801'}):
            capture = self.api('capture', **{**self.post, 'topic_id': 4801})['result']['capture']
        with patch.dict(os.environ, {'T6_DRIVE_RECORDING_TOPIC_ID': '4802'}):
            self.resolve()
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
            self.resolve()
            self.resolve()
        assert isinstance(first, Done)
        for capture in (first.result['capture'], second['result']['capture']):
            self.assertEqual(self.api('capture result', capture=capture)['result']['state'], 'resolved')
        self.assertEqual(len(self.fake.calls), 2)

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
        capture = self.settle(label='Synthetic new')
        self.api('capture undo', capture=capture)
        drive = next(d for d in self.drives() if d['serial'] == 'SAMPLE-NEW')
        self.assertEqual(drive['status'], 'unknown')
        self.assertEqual(self.call('get', drive=drive['id']).result['drive']['status'], 'unknown')

    def test_undo_in_restores_release_and_does_not_readd_undone_capture(self) -> None:
        out = self.settle()
        returned = self.settle('IN\nSynthetic first', message_id=2, posted_at='2026-11-25T10:00:00+08:00')
        self.api('capture undo', capture=returned)
        self.assertEqual(self.drives()[0]['status'], 'released')
        self.api('capture undo', capture=out)
        self.assertEqual(self.drives()[0]['status'], 'in custody')
        from t6.verbs import VERBS, invoke
        entry = next(v for v in VERBS if (v.noun, v.verb) == ('movement', 'list'))
        self.assertEqual(invoke(entry, entry.input_model(needs_project=True), actor='synthetic-keeper',
                                people=self.people, open_notion=lambda _: None).result['movements'], [])  # type: ignore[union-attr]

    def test_late_delivery_keeps_latest_post_status(self) -> None:
        self.settle('IN\nSynthetic first', message_id=2, posted_at='2026-11-25T10:00:00+08:00')
        old = self.settle()
        self.assertEqual(self.drives()[0]['status'], 'in custody')
        self.api('capture undo', capture=old)
        self.assertEqual(self.drives()[0]['status'], 'in custody')

    # The debug topic, against a fake gateway transport (this class is the transport).
    DEBUG_CHAT, DEBUG_TOPIC = -1004352220086, 632

    def send(self, method: str, path: str, headers: Any, body: bytes) -> tuple[int, dict[str, Any]]:
        if self.gateway_down:
            return 500, {'detail': 'down'}
        message = json.loads(body)
        self.sent.append((path, message))
        return 202, {'message_ref': message.get('message_ref', 41)}

    def run_debug(self, enabled: bool = True, text: str = 'OUT Synthetic borrower password: hunter2') -> str:
        from t6.drives.capture import CaptureResolver
        from t6.drives.debug import DebugNotifier
        from t6.worker import Worker
        from t6.worker.gateway import GatewayClient
        from t6.worker.internal_queue import InternalQueueClient
        capture = self.api('capture', **{**self.post, 'text': text})['result']['capture']
        self.fake.decisions({'model': 'synthetic-jev', 'answers': {'c0': {'type': 'choice', 'choice': 'match',
                             'probabilities': {'match': 0.9, 'maybe': 0.1, 'no': 0}}}})
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
                         'synthetic-jev', 'State: resolved', 'Undo until:'):
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

    def test_a_failed_jev_shows_the_error_and_keeps_the_post(self) -> None:
        from t6.drives.capture import CaptureResolver
        from t6.drives.debug import DebugNotifier
        from t6.llm.testing import FakeProvider
        from t6.worker import Worker
        from t6.worker.gateway import GatewayClient
        from t6.worker.internal_queue import InternalQueueClient
        self.api('capture', **self.post)
        jev = FakeProvider()
        jev.timeout()
        notifier = DebugNotifier(self.registry, GatewayClient(self, 'secret'), self.DEBUG_CHAT, None, lambda: True)
        resolver = CaptureResolver(self.registry, self.fake, decisions=jev, debug=notifier)
        Worker(InternalQueueClient(self.queue), 'main', {'drive.capture.resolve': resolver.handle}).run_once()
        self.assertIn('⛔ error', self.sent[-1][1]['text'])
        self.assertIn('Jev failed: timeout', self.sent[-1][1]['text'])
        self.assertIn('State: resolved', self.sent[-1][1]['text'])
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

    # Every Item of a kept Capture waits in the Inbox until the custodian approves it (#724).
    keeper = 'synthetic-keeper-secret'
    two_items = 'OUT\nSynthetic first\nSynthetic second\nR: Marco'

    def kept(self, text: str = two_items, message_id: int = 1, **values: Any) -> str:
        capture = str(self.api('capture', **{**self.post, 'message_id': message_id, 'text': text,
                                             'posted_at': f'2026-11-24T{10 + message_id}:30:00+08:00', **values})['result']['capture'])
        self.resolve()
        return capture

    def settle(self, text: str = two_items, label: str = 'Synthetic first', message_id: int = 1, **values: Any) -> str:
        """Keep a post and approve its first Item with the Drive labelled `label`; the Capture ID."""
        capture = self.kept(text, message_id, **values)
        self.approve(capture, 0, 'drive', typed=label)
        return capture

    def inbox(self) -> list[dict[str, Any]]:
        result = self.api('inbox', self.keeper)
        self.assertEqual(result['outcome'], 'done', result)
        return list(result['result']['items'])

    def approve(self, capture: str, item: int, choice: str, **values: Any) -> dict[str, Any]:
        return self.api('approve', self.keeper, capture=capture, item=item, choice=choice, **values)

    def moved(self, **values: Any) -> list[dict[str, Any]]:
        from t6.verbs import VERBS, invoke
        entry = next(v for v in VERBS if (v.noun, v.verb) == ('movement', 'list'))
        return list(invoke(entry, entry.input_model.model_validate(values), actor='synthetic-keeper', people=self.people,
                           open_notion=lambda _: None).result['movements'])  # type: ignore[union-attr]

    def ids(self) -> dict[str, str]:
        return {str(d['label']): str(d['id']) for d in self.drives()}

    def test_a_two_item_capture_shows_two_inbox_items_and_makes_no_movement_until_approved(self) -> None:
        capture = self.kept()
        self.assertEqual(self.api('capture result', capture=capture)['result']['state'], 'resolved')
        items = self.inbox()
        self.assertEqual([(i['capture'], i['item'], i['text'], i['direction'], i['recipient'], i['guesses']) for i in items],
                         [(capture, 0, 'Synthetic first', 'OUT', 'Marco', []), (capture, 1, 'Synthetic second', 'OUT', 'Marco', [])])
        self.assertEqual((items[0]['caption'], items[0]['photo_reference'], items[0]['chat_id'], items[0]['message_id']),
                         (self.two_items, capture, -900001, 1))
        self.assertEqual(self.moved(), [])
        self.assertEqual([d['status'] for d in self.drives()], ['in custody', 'in custody'])
        self.assertEqual(self.call('exceptions').result['captures'], [])
        self.assertEqual(len(self.fake.calls), 1)  # Jev only: the photo is not read

    def test_approving_each_item_with_a_drive_appends_one_movement_each(self) -> None:
        capture = self.kept()
        drives = self.ids()
        first = self.approve(capture, 0, 'drive', drive=drives['Synthetic first'])
        self.assertEqual((first['outcome'], first['result']['state'], first['result']['drive']), ('done', 'approved', drives['Synthetic first']), first)
        self.assertEqual([i['item'] for i in self.inbox()], [1])
        self.approve(capture, 1, 'drive', drive=drives['Synthetic second'])
        self.assertEqual(self.inbox(), [])
        rows = self.moved()
        self.assertEqual([(m['drive'], m['direction'], m['person'], m['capture'], m['on']) for m in rows],
                         [(drives[label], 'OUT', 'Marco', capture, '2026-11-24') for label in ('Synthetic first', 'Synthetic second')])
        self.assertEqual(len({m['shipment_id'] for m in rows}), 1)
        self.assertEqual([d['status'] for d in self.drives()], ['released', 'released'])
        self.assertEqual([i['state'] for i in self.api('capture result', capture=capture)['result']['items']], ['approved', 'approved'])

    def test_a_drive_can_be_typed_by_label_or_serial(self) -> None:
        capture = self.kept('IN\nA\nB')
        self.assertEqual(self.approve(capture, 0, 'drive', typed='Synthetic second')['result']['state'], 'approved')
        self.assertEqual(self.approve(capture, 1, 'drive', typed='SAMPLE-FIRST')['result']['state'], 'approved')
        self.assertEqual(sorted(m['label'] for m in self.moved()), ['Synthetic first', 'Synthetic second'])
        other = self.kept('IN\nC', 2)
        refused = self.approve(other, 0, 'drive', typed='NO-SUCH-DRIVE')
        self.assertEqual((refused['outcome'], refused['rule']), ('refused', 'drive'))
        self.assertEqual(self.approve(other, 0, 'drive', drive='t6d_missing')['rule'], 'drive')
        self.assertEqual(self.approve(other, 0, 'drive')['rule'], 'input')
        self.assertEqual(len(self.moved()), 2)

    def test_an_in_is_received_by_the_poster(self) -> None:
        with self.registry._transaction() as db:
            db.execute("UPDATE persons SET telegram_id=900010 WHERE id='synthetic-keeper'")
        capture = self.kept('IN: Synthetic first')
        self.approve(capture, 0, 'drive', typed='Synthetic first')
        (row,) = self.moved()
        self.assertEqual((row['direction'], row['person'], row['recorder'], row['recorder_telegram_id']),
                         ('IN', 'synthetic-keeper', 'synthetic-keeper', 900010))

    def test_none_keeps_the_item_waiting_and_not_a_drive_closes_it_without_a_movement(self) -> None:
        capture = self.kept()
        for _ in range(2):
            none = self.approve(capture, 0, 'none')
            self.assertEqual((none['outcome'], none['result']['state']), ('done', 'open'))
        self.assertEqual([i['item'] for i in self.inbox()], [0, 1])
        closed = self.approve(capture, 1, 'not_a_drive')
        self.assertEqual((closed['result']['state'], closed['result']['movement']), ('not_a_drive', None))
        self.assertEqual([i['item'] for i in self.inbox()], [0])
        self.assertEqual(self.moved(), [])
        self.assertEqual([d['status'] for d in self.drives()], ['in custody', 'in custody'])

    def test_new_creates_the_drive_and_its_movement(self) -> None:
        capture = self.kept('IN\nSYN9 D01')
        made = self.approve(capture, 0, 'new', serial='SAMPLE-NEW')
        self.assertEqual((made['outcome'], made['result']['state']), ('done', 'approved'), made)
        drive = self.call('get', drive=made['result']['drive']).result['drive']
        self.assertEqual((drive['label'], drive['serial'], drive['status']), ('SYN9 D01', 'SAMPLE-NEW', 'in custody'))
        (row,) = self.moved()
        self.assertEqual((row['drive'], row['direction'], row['capture']), (drive['id'], 'IN', capture))
        self.assertEqual(self.inbox(), [])
        self.assertEqual(len(self.drives()), 3)
        self.assertEqual(self.approve(capture, 0, 'new', serial='SAMPLE-NEW')['result'], made['result'])
        self.assertEqual(len(self.drives()), 3)
        again = self.kept('IN\nSYN9 D02', 2)
        self.assertEqual(self.approve(again, 0, 'new', label='Synthetic named', serial='SAMPLE-NEW')['rule'], 'serial')

    def test_repeated_approvals_are_idempotent_and_a_settled_item_is_not_changed(self) -> None:
        capture = self.kept()
        drives = self.ids()
        first = self.approve(capture, 0, 'drive', drive=drives['Synthetic first'])
        self.assertEqual(self.approve(capture, 0, 'drive', drive=drives['Synthetic first'])['result'], first['result'])
        self.assertEqual(len(self.moved()), 1)
        for choice, values in (('drive', dict(drive=drives['Synthetic second'])), ('not_a_drive', {}), ('none', {})):
            refused = self.approve(capture, 0, choice, **values)
            self.assertEqual((refused['outcome'], refused['rule']), ('refused', 'item_settled'), choice)
        self.approve(capture, 1, 'not_a_drive')
        self.assertEqual(self.approve(capture, 1, 'not_a_drive')['outcome'], 'done')
        self.assertEqual(self.approve(capture, 1, 'drive', drive=drives['Synthetic first'])['rule'], 'item_settled')
        self.assertEqual(len(self.moved()), 1)

    def test_only_a_drive_caller_reads_the_inbox_or_approves(self) -> None:
        capture = self.kept()
        for secret in ('synthetic-gateway', 'synthetic-other'):
            for result in (self.api('inbox', secret), self.api('approve', secret, capture=capture, item=0, choice='none')):
                self.assertEqual((result['outcome'], result['rule']), ('refused', 'permission'), (secret, result))

    def test_unknown_captures_items_and_unusable_captures_are_refused(self) -> None:
        capture = self.kept()
        self.assertEqual(self.approve('capture_missing', 0, 'none')['rule'], 'capture')
        self.assertEqual(self.approve(capture, 5, 'none')['rule'], 'item')
        ignored = self.api('capture', **{**self.post, 'message_id': 2, 'text': 'Synthetic lunch photo'})['result']['capture']
        self.resolve('no')
        self.assertEqual(self.approve(ignored, 0, 'none')['rule'], 'capture')
        pending = self.api('capture', **{**self.post, 'message_id': 3})['result']['capture']
        self.assertEqual(self.approve(pending, 0, 'none')['rule'], 'capture')
        self.assertEqual([i['capture'] for i in self.inbox()], [capture, capture])

    def test_the_inbox_lists_the_newest_capture_first_and_leaves_out_count_lines_and_ignored_posts(self) -> None:
        older = self.kept('IN\nSYN7 D21\n48 FD', 1)
        newer = self.kept('OUT\nSYN7 D22\nR: Marco', 2)
        self.assertEqual([(i['capture'], i['text']) for i in self.inbox()], [(newer, 'SYN7 D22'), (older, 'SYN7 D21')])
        counted = self.api('capture result', capture=older)['result']
        self.assertEqual(counted['counts'], [{'count': 48, 'kind': 'FD'}])
        self.assertEqual(counted['items'], [{'text': 'SYN7 D21', 'state': 'open'}])
        photo_only = self.kept('IN', 3)
        self.assertEqual(self.api('capture result', capture=photo_only)['result']['state'], 'resolved')
        self.assertNotIn(photo_only, [i['capture'] for i in self.inbox()])

    def test_a_capture_with_a_problem_is_never_an_exception_and_joins_the_inbox_once_fixed(self) -> None:
        capture = self.kept('OUT\nSynthetic first')
        result = self.api('capture result', capture=capture)['result']
        self.assertEqual((result['state'], result['grade'], result['problem'], result['reason']), ('resolved', 'problem', 'missing_recipient', None))
        self.assertEqual(self.inbox(), [])
        self.assertEqual(self.approve(capture, 0, 'drive', typed='Synthetic first')['rule'], 'problem')
        self.assertEqual(self.call('exceptions').result['captures'], [])
        edited = self.api('capture', capture=capture, text='OUT\nSynthetic first\nR: Marco', edit=True)['result']
        self.assertEqual(edited['state'], 'pending')
        self.resolve()
        self.assertEqual([(i['capture'], i['recipient']) for i in self.inbox()], [(capture, 'Marco')])
        self.assertEqual(self.approve(capture, 0, 'drive', typed='Synthetic first')['result']['state'], 'approved')

    def test_no_capture_becomes_an_exception(self) -> None:
        text_only = {k: v for k, v in self.post.items() if k != 'photo'}
        for number, (text, extra) in enumerate((('Synthetic first', {}), ('IN', {}), ('OUT', {}), ('IN\nSynthetic first', text_only)), 1):
            with self.subTest(text=text, text_only=bool(extra)):
                capture = self.api('capture', **{**self.post, **extra, 'message_id': number, 'text': text})['result']['capture']
                self.resolve()
                self.assertEqual(self.api('capture result', capture=capture)['result']['state'], 'resolved')
        self.assertEqual(self.call('exceptions').result['captures'], [])
        self.assertEqual(self.api('capture list', self.keeper)['result']['captures'], [])

    def test_undo_reverses_the_movements_and_closes_the_inbox_items(self) -> None:
        capture = self.kept()
        drives = self.ids()
        self.approve(capture, 0, 'drive', drive=drives['Synthetic first'])
        self.assertEqual(self.drives()[0]['status'], 'released')
        undone = self.api('capture undo', capture=capture)['result']
        self.assertEqual(undone['state'], 'undone')
        self.assertEqual([i['state'] for i in undone['items']], ['approved', 'closed'])
        self.assertEqual(self.inbox(), [])
        self.assertEqual([d['status'] for d in self.drives()], ['in custody', 'in custody'])
        self.assertEqual(self.approve(capture, 1, 'drive', drive=drives['Synthetic second'])['rule'], 'item_settled')
        self.assertEqual(self.approve(capture, 1, 'none')['rule'], 'item_settled')
        self.assertEqual(len([m for m in self.moved() if m['capture'] == capture and m['corrects'] is None]), 1)

    def coordinated_drive(self) -> tuple[str, str, str, str]:
        """A mapped Project whose Notion page names `contact` as T6 Post Coordinator, and a Drive on it."""
        from t6.ids import EntityKind, T6Id
        from t6.notion.generated import ProjectsRow
        from t6.notion.testing import project_properties, relation
        contact, other = '0b000000-0000-0000-0000-00000000c001', '0b000000-0000-0000-0000-00000000c002'
        person = str(T6Id.mint(EntityKind.PERSON))
        self.registry.bind_entity('notion', contact, T6Id.parse(person, EntityKind.PERSON), actor='setup')
        page = self.notion.add_page('synthetic-projects', {**project_properties(name='Synthetic Show'),
                                                           ProjectsRow.notion_name('t6_post_coordinator'): relation(contact)})
        project = self.registry.register(actor='setup', idempotency_key='coordinated', notion_page_id=page['id']).project.id  # type: ignore[union-attr]
        mapping = self.folder / 'mapping.json'
        mapping.write_text(json.dumps([{'name': 'SYNTHETIC PROJECT', 'project': project, 'certain': True}]))
        values = {'file': str(mapping), 'idempotency_key': 'map-coordinated'}
        self.call('mapping apply', **values, confirm=self.call('mapping apply', **values).token)
        self.reload('reload-mapped')
        return str(page['id']), other, person, contact

    def test_the_movement_keeps_the_coordinator_it_had_at_the_move_after_the_project_coordinator_changes(self) -> None:
        from t6.notion.generated import ProjectsRow
        from t6.notion.testing import relation
        page, other, person, _ = self.coordinated_drive()
        capture = self.kept('OUT\nSynthetic first\nSynthetic second\nR: Marco')
        drives = self.ids()
        self.approve(capture, 0, 'drive', drive=drives['Synthetic first'])
        self.notion.update_page(page, {ProjectsRow.notion_name('t6_post_coordinator'): relation(other)})
        self.approve(capture, 1, 'drive', drive=drives['Synthetic second'])
        first, second = self.moved()
        self.assertEqual((first['coordinators'], first['person']), ([person], 'Marco'))  # the coordinator is not the recipient
        self.assertEqual(second['coordinators'], [other])  # a contact with no Person is kept by its page
        self.assertEqual(self.moved(drive=drives['Synthetic first'])[0]['coordinators'], [person])
        self.notion.update_page(page, {ProjectsRow.notion_name('t6_post_coordinator'): relation()})
        self.assertEqual(self.moved(drive=drives['Synthetic first'])[0]['coordinators'], [person])

    def test_a_movement_without_a_registered_project_or_notion_has_no_coordinators(self) -> None:
        capture = self.kept('IN\nSynthetic first')
        self.approve(capture, 0, 'drive', typed='Synthetic first')
        self.assertEqual(self.moved()[0]['coordinators'], [])
