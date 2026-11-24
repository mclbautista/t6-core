"""Vision matching at the HTTP verb seam; all posts and replies are synthetic."""
import json
from typing import Any
import base64
import io
import os
from unittest.mock import patch

from PIL import Image as PILImage

from t6.drives.capture import CaptureResolver
from t6.llm.testing import FakeProvider
from t6.worker import Worker
from t6.worker.internal_queue import InternalQueueClient

import test_drive_guesses as guess_tests
from test_drive_reload import HEADER


class DrivePhotoMatchTests(guess_tests.DriveGuessFixture):
    def test_vision_guesses_rank_the_inbox_and_move_guess_one(self) -> None:
        self.workbook({'SYNTHETIC SHOW': [HEADER, ['SYN D01', 'SAMPLE-01'], ['SYN D02', 'SAMPLE-02']]})
        self.reload()
        ids = self.inventory()
        self.fake.reply(json.dumps({'guesses': [
            {'letter': 'B', 'confidence': 0.88, 'reason': 'Same synthetic blue box'},
            {'letter': 'A', 'confidence': 0.4, 'reason': 'Similar tape'}], 'label_text': None}))
        result = self.capture('OUT SYN\nR: Synthetic recipient')
        guesses = self.inbox()[0]['guesses']
        self.assertEqual([(g['drive'], g['confidence'], g['reason']) for g in guesses], [
            (ids['SYN D02']['id'], 0.88, 'Same synthetic blue box'),
            (ids['SYN D01']['id'], 0.4, 'Similar tape')])
        self.assertEqual(result['items'][0]['guesses'], guesses)
        (movement,) = self.movements()
        self.assertEqual((movement['drive'], movement['to_review']), (ids['SYN D02']['id'], True))

    def test_vision_failure_keeps_capture_inbox_and_status_without_a_movement(self) -> None:
        self.workbook({'SYNTHETIC SHOW': [HEADER, ['SYN D01', 'SAMPLE-01']]})
        self.reload()
        for number, failure in enumerate(('no_provider', 'api_error', 'timeout', 'invalid', 'policy'), 1):
            with self.subTest(failure=failure):
                fake = FakeProvider(retention='unknown' if failure == 'policy' else 'zdr_enforced')
                if failure == 'api_error':
                    fake.api_error()
                elif failure == 'timeout':
                    fake.timeout()
                else:
                    fake.reply('{"guesses": "invalid", "label_text": null}')
                capture = self.http('drive/capture', chat_id=-900001, message_id=number, sender_telegram_id=900010,
                                    posted_at='2026-11-24T10:30:00+08:00', text='OUT SYN D01\nR: Synthetic recipient',
                                    photo=self.image())['capture']
                resolver = CaptureResolver(self.registry, None if failure == 'no_provider' else fake,
                                           environ={'T6_DRIVE_RECORDING_CHAT_ID': '-900001'})
                self.assertTrue(Worker(InternalQueueClient(self.queue), 'main', {'drive.capture.resolve': resolver.handle}).run_once())
                result = self.http('drive/capture/result', capture=capture)
                self.assertEqual(result['state'], 'resolved')
                self.assertEqual(result['items'][0]['guesses'], [])
                self.assertEqual(self.inbox()[0]['capture'], capture)
                self.assertEqual(self.movements(), [])
                self.assertEqual(self.inventory()['SYN D01']['status'], 'in custody')
                self.assertEqual(len(fake.calls), 0 if failure in ('no_provider', 'policy') else 1)
                self.assertEqual(self.http('drive/capture/photo', capture=capture)['photo'], self.image())

    def image(self, size: tuple[int, int] = (1, 1), color: str = 'white') -> str:
        output = io.BytesIO()
        PILImage.new('RGB', size, color).save(output, format='PNG')
        return base64.b64encode(output.getvalue()).decode()

    def teach_reference(self, drive: str, text: str, color: str = 'blue', times: int = 1) -> None:
        for _ in range(times):
            self.fake.reply('{"guesses": [], "label_text": null}')
            result = self.capture(f'OUT {text}\nR: Synthetic recipient', photo=self.image((800, 600), color))
            # A caption with no shortlist does not consume a reply.
            self.fake = guess_tests.OrderedShortlistProvider()
            self.http('drive/approve', capture=result['capture'], item=0, choice='drive', drive=drive)
            self.http('drive/capture/undo', capture=result['capture'])

    def test_vision_shrinks_the_post_and_sends_only_shortlisted_reference_thumbnails(self) -> None:
        self.enterContext(patch.dict(os.environ, {'T6_DRIVE_PHOTO_DIR': str(self.folder / 'photos')}))
        self.workbook({'SYNTHETIC SHOW': [HEADER, *[[f'SYN D0{i}', f'SAMPLE-0{i}'] for i in range(1, 8)]],
                       'OTHER': [HEADER, ['Unrelated zebra', 'SAMPLE-OTHER']]})
        self.reload()
        ids = self.inventory()
        for label in ('SYN D01', 'SYN D06', 'SYN D07', 'Unrelated zebra'):
            self.teach_reference(ids[label]['id'], label, 'red' if label == 'Unrelated zebra' else 'blue')
        self.fake.reply('{"guesses": [{"letter": "F", "confidence": 0.8, "reason": "Same blue box"}], "label_text": null}')
        self.capture('OUT SYN\nR: Synthetic recipient', photo=self.image((3200, 2000)))
        (call,) = self.fake.calls
        images = [m['image'] for m in call.messages if 'image' in m]
        with PILImage.open(io.BytesIO(images[0].data)) as image:
            self.assertEqual(image.size, (1600, 1000))
        entries = json.loads(call.messages[1]['content'])['shortlist']
        self.assertEqual([e['letter'] for e in entries], list('ABCDEF'))
        self.assertEqual({e['drive'] for e in entries}, {ids[f'SYN D0{i}']['id'] for i in range(1, 7)})
        self.assertEqual([json.loads(m['content'])['reference'] for m in call.messages[2:]], ['A', 'F'])
        self.assertEqual(len(images), 3)
        for thumbnail in images[1:]:
            with PILImage.open(io.BytesIO(thumbnail.data)) as image:
                self.assertLessEqual(max(image.size), 320)
                pixel = image.convert('RGB').getpixel((0, 0))
                assert isinstance(pixel, tuple)
                self.assertGreater(pixel[2], 200)
        self.assertEqual(self.inbox()[0]['guesses'][0]['drive'], ids['SYN D06']['id'])
        self.assertTrue(call.zdr)


    def test_vision_keeps_confirmed_exact_first_and_can_read_the_matching_label(self) -> None:
        self.enterContext(patch.dict(os.environ, {'T6_DRIVE_PHOTO_DIR': str(self.folder / 'photos')}))
        self.workbook({'SYNTHETIC SHOW': [HEADER, ['SYN D01', 'SAMPLE-01'], ['SYN D02', 'SAMPLE-02']]})
        self.reload()
        ids = self.inventory()
        self.teach_reference(ids['SYN D01']['id'], 'SYN D01', times=3)
        for text, label_text in (('SYN D01', None), ('SYN', 'SYN D01')):
            with self.subTest(text=text):
                self.fake.reply(json.dumps({'guesses': [
                    {'letter': 'B', 'confidence': 0.99, 'reason': 'Similar box'}], 'label_text': label_text}))
                result = self.capture(f'OUT {text}\nR: Synthetic recipient')
                guesses = self.inbox()[0]['guesses']
                self.assertEqual([(g['drive'], g['confidence'], g['exact']) for g in guesses], [
                    (ids['SYN D01']['id'], 0.95, True), (ids['SYN D02']['id'], 0.99, False)])
                self.assertEqual(self.movements()[-1]['drive'], ids['SYN D01']['id'])
                self.http('drive/capture/undo', capture=result['capture'])

    def test_vision_is_one_call_per_item_and_empty_shortlists_never_call(self) -> None:
        self.workbook({'SYNTHETIC SHOW': [HEADER, ['SYN D01', 'SAMPLE-01'], ['SYN D02', 'SAMPLE-02']]})
        self.reload()
        self.fake.reply('{"guesses": [], "label_text": null}').reply('{"guesses": [], "label_text": null}')
        result = self.capture('OUT SYN D01\nSYN D02\nR: Synthetic recipient')
        self.assertEqual(len(self.fake.calls), 2)
        self.assertEqual([json.loads(c.messages[1]['content'])['item'] for c in self.fake.calls], ['SYN D01', 'SYN D02'])
        self.assertEqual([i['guesses'] for i in result['items']], [[], []])
        self.capture('OUT Unrelated zebra\nR: Synthetic recipient')
        self.assertEqual(len(self.fake.calls), 2)
        self.assertEqual(self.movements(), [])

    def test_vision_invalid_outputs_never_partially_create_a_guess(self) -> None:
        self.workbook({'SYNTHETIC SHOW': [HEADER, ['SYN D01', 'SAMPLE-01']]})
        self.reload()
        valid = {'letter': 'A', 'confidence': 0.7, 'reason': 'Same box'}
        invalid: list[Any] = [
            {**valid, 'letter': 'B'}, {**valid, 'letter': 'Z'},
            {**valid, 'confidence': -0.1}, {**valid, 'confidence': 1.1},
            {**valid, 'confidence': float('nan')}, {**valid, 'confidence': True},
            {**valid, 'confidence': '0.8'}, {**valid, 'reason': 'two\nlines'}, {**valid, 'reason': 'trailing\n'},
            {**valid, 'reason': '  '}, {**valid, 'unrequested': True},
        ]
        replies = ['not JSON', '{}', '{"guesses": null, "label_text": null}']
        replies += [json.dumps({'guesses': [bad], 'label_text': None}) for bad in invalid]
        replies.append(json.dumps({'guesses': [valid, {**valid, 'letter': 'B'}], 'label_text': None}))
        replies += [json.dumps({'guesses': [valid] * count, 'label_text': None}) for count in (2, 4)]
        for reply in replies:
            with self.subTest(reply=reply):
                self.fake.reply(reply)
                result = self.capture('OUT SYN D01\nR: Synthetic recipient')
                self.assertEqual(result['state'], 'resolved')
                self.assertEqual(self.inbox()[0]['guesses'], [])
                self.assertEqual(self.movements(), [])

    def test_vision_bad_image_keeps_the_capture_without_calling_the_provider(self) -> None:
        self.workbook({'SYNTHETIC SHOW': [HEADER, ['SYN D01', 'SAMPLE-01']]})
        self.reload()
        result = self.capture('OUT SYN D01\nR: Synthetic recipient', photo=base64.b64encode(b'broken photo').decode())
        self.assertEqual(result['state'], 'resolved')
        self.assertEqual(self.inbox()[0]['guesses'], [])
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.movements(), [])

    def send(self, method: str, path: str, headers: Any, body: bytes) -> tuple[int, dict[str, Any]]:
        message = json.loads(body)
        self.sent.append(message)
        return 202, {'message_ref': 41}

    def test_vision_debug_shows_candidates_thumbnails_reasons_exact_checks_and_usage(self) -> None:
        from t6.drives.debug import DebugNotifier
        from t6.worker.gateway import GatewayClient
        self.workbook({'SYNTHETIC SHOW': [HEADER, ['SYN D01', 'SAMPLE-01']]})
        self.reload()
        self.sent: list[dict[str, Any]] = []
        self.notifier = DebugNotifier(self.registry, GatewayClient(self, 'synthetic'), -900002, 12, lambda: True)
        self.fake.reply('{"guesses": [{"letter": "A", "confidence": 0.8, "reason": "Same synthetic box"}], "label_text": "SYN D01"}',
                        prompt_tokens=100, completion_tokens=20, cost=0.00012)
        self.capture('OUT SYN D01\nR: Synthetic recipient')
        text = self.sent[-1]['text']
        for expected in ('Shortlist:', 'SYN D01', 'Thumbnails sent: none', 'Same synthetic box',
                         'Exact-match checks:', 'confirmations', 'unique', 'reference_photo', 'fake-model', 'Latency:', 'Cost: 0.00012', '100', '20'):
            self.assertIn(expected, text)
        self.assertNotIn('image/jpeg', text)
        self.assertEqual(self.movements()[0]['to_review'], True)

    def test_vision_boundary_rechecks_chat_topic_and_missing_configuration(self) -> None:
        self.workbook({'SYNTHETIC SHOW': [HEADER, ['SYN D01', 'SAMPLE-01']]})
        self.reload()
        for number, env in enumerate(({'T6_DRIVE_RECORDING_CHAT_ID': '-900002'},
                                      {'T6_DRIVE_RECORDING_CHAT_ID': '-900001', 'T6_DRIVE_RECORDING_TOPIC_ID': '99'}, {}), 1):
            with self.subTest(env=env):
                capture = self.http('drive/capture', chat_id=-900001, message_id=number, topic_id=12,
                                    sender_telegram_id=900010, posted_at='2026-11-24T10:30:00+08:00',
                                    text='OUT SYN D01\nR: Synthetic recipient', photo=self.image())['capture']
                resolver = CaptureResolver(self.registry, self.fake, environ=env)
                self.assertTrue(Worker(InternalQueueClient(self.queue), 'main', {'drive.capture.resolve': resolver.handle}).run_once())
                self.assertEqual(self.fake.calls, [])
                self.assertEqual(self.http('drive/capture/result', capture=capture)['state'], 'pending')
        # The same post in its configured topic is allowed through to vision.
        self.enterContext(patch.dict(os.environ, {'T6_DRIVE_RECORDING_TOPIC_ID': '12'}))
        capture = self.http('drive/capture', chat_id=-900001, message_id=10, topic_id=12, sender_telegram_id=900010,
                            posted_at='2026-11-24T10:30:00+08:00', text='OUT SYN D01\nR: Synthetic recipient', photo=self.image())['capture']
        resolver = CaptureResolver(self.registry, self.fake, environ={'T6_DRIVE_RECORDING_CHAT_ID': '-900001', 'T6_DRIVE_RECORDING_TOPIC_ID': '12'})
        self.assertTrue(Worker(InternalQueueClient(self.queue), 'main', {'drive.capture.resolve': resolver.handle}).run_once())
        self.assertEqual(self.http('drive/capture/result', capture=capture)['state'], 'resolved')
        self.assertEqual(len(self.fake.calls), 1)

    def test_vision_worker_without_provider_configuration_still_logs_the_item(self) -> None:
        from t6.worker.__main__ import WorkerSettings, configured_handlers
        from t6.worker.client import HttpTransport
        self.workbook({'SYNTHETIC SHOW': [HEADER, ['SYN D01', 'SAMPLE-01']]})
        self.reload()
        capture = self.http('drive/capture', chat_id=-900001, message_id=1, sender_telegram_id=900010,
                            posted_at='2026-11-24T10:30:00+08:00', text='OUT SYN D01\nR: Synthetic recipient', photo=self.image())['capture']
        env = dict(T6_SERVER_URL='https://synthetic.invalid', T6_WORKER_SECRET='synthetic', T6_WORKER_QUEUE='main',
                   T6_API_URL='https://synthetic.invalid', T6_DATABASE=str(self.folder / 't6.sqlite'),
                   T6_DRIVE_RECORDING_CHAT_ID='-900001')
        handlers = configured_handlers(env, WorkerSettings.from_env(env), HttpTransport('https://synthetic.invalid'))
        self.assertIn('drive.capture.resolve', handlers['main'])
        self.assertTrue(Worker(InternalQueueClient(self.queue), 'main', handlers['main']).run_once())
        self.assertEqual(self.http('drive/capture/result', capture=capture)['state'], 'resolved')
        self.assertEqual(self.inbox()[0]['guesses'], [])
        self.assertEqual(self.movements(), [])

    def test_vision_wait_does_not_block_undo_or_restore_an_undone_item(self) -> None:
        from urllib.request import Request, urlopen
        from t6.llm.transport import Completion, Request as LLMRequest, ToolSpec
        from collections.abc import Sequence
        self.workbook({'SYNTHETIC SHOW': [HEADER, ['SYN D01', 'SAMPLE-01']]})
        self.reload()
        capture = self.http('drive/capture', chat_id=-900001, message_id=1, sender_telegram_id=900010,
                            posted_at='2026-11-24T10:30:00+08:00', text='OUT SYN D01\nR: Synthetic recipient', photo=self.image())['capture']
        request = Request(f'http://127.0.0.1:{self.server.server_port}/v1/drive/capture/undo',
                          data=json.dumps({'capture': capture}).encode(), headers={'Authorization': 'Bearer synthetic-gateway'})

        class UndoDuringVision(FakeProvider):
            completed = False

            def complete(self, payload: LLMRequest, *, timeout: float, zdr: bool = False,
                         tools: Sequence[ToolSpec] = ()) -> Completion:
                with urlopen(request, timeout=1) as response:
                    self.completed = json.load(response)['outcome'] == 'done'
                return super().complete(payload, timeout=timeout, zdr=zdr, tools=tools)

        fake = UndoDuringVision()
        fake.reply('{"guesses": [{"letter": "A", "confidence": 0.8, "reason": "Same box"}], "label_text": null}')
        resolver = CaptureResolver(self.registry, fake, environ={'T6_DRIVE_RECORDING_CHAT_ID': '-900001'})
        self.assertTrue(Worker(InternalQueueClient(self.queue), 'main', {'drive.capture.resolve': resolver.handle}).run_once())
        self.assertTrue(fake.completed)
        self.assertEqual(self.http('drive/capture/result', capture=capture)['state'], 'undone')
        self.assertEqual(self.inbox(), [])
        self.assertEqual(self.movements(), [])

    def test_vision_synthetic_evals_through_http_and_fake_provider(self) -> None:
        from t6.llm.drive_photo_match_evals import CASES, OFFLINE_REPLIES, grade_match, synthetic_photo
        self.enterContext(patch.dict(os.environ, {'T6_DRIVE_PHOTO_DIR': str(self.folder / 'photos')}))
        self.workbook({'SYNTHETIC SHOW': [HEADER, ['SYN MASTER', 'SAMPLE-01'], ['SYN BACKUP', 'SAMPLE-02']]})
        self.reload()
        ids = self.inventory()
        self.teach_reference(ids['SYN MASTER']['id'], 'SYN MASTER', 'red')
        self.teach_reference(ids['SYN BACKUP']['id'], 'SYN BACKUP', 'blue')
        for case in CASES:
            with self.subTest(case=case.id):
                self.assertEqual(case.provenance.kind, 'synthetic')
                self.fake.reply(OFFLINE_REPLIES[case.id])
                result = self.capture('OUT SYN\nR: Synthetic recipient', photo=base64.b64encode(synthetic_photo(case.id)).decode())
                entries = json.loads(self.fake.calls[-1].messages[1]['content'])['shortlist']
                by_drive = {e['drive']: e['letter'] for e in entries}
                guesses = result['items'][0]['guesses']
                output = {'guesses': [{'letter': by_drive[g['drive']], 'confidence': g['confidence'], 'reason': g['reason']} for g in guesses],
                          'label_text': json.loads(OFFLINE_REPLIES[case.id])['label_text']}
                self.assertTrue(grade_match(case, output).passed)
                self.assertFalse(grade_match(case, {'guesses': 'invalid'}).passed)
                self.assertFalse(grade_match(case, {'guesses': [{'letter': 'F', 'confidence': 0.8, 'reason': 'Invented'}], 'label_text': None}).passed)
                if case.id != 'unrelated':
                    self.assertFalse(grade_match(case, {'guesses': [], 'label_text': None}).passed)
                self.http('drive/capture/undo', capture=result['capture'])
