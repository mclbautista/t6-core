"""Custodian delivery through HTTP verbs and a fake gateway; synthetic records only."""
import base64
import json
import os
from collections.abc import Callable, Mapping
from typing import Any
from unittest.mock import patch

import test_drive_guesses as guess_tests
from test_drive_reload import HEADER
from test_llm_drive_photo import photo
from t6.drives.capture import CaptureResolver
from t6.worker import Worker
from t6.worker.__main__ import WorkerSettings, configured_handlers
from t6.worker.client import HttpTransport
from t6.worker.internal_queue import InternalQueueClient


class FakeGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.messages: dict[str, int] = {}
        self.on_notify: Callable[[dict[str, Any]], None] | None = None

    def send(self, method: str, path: str, headers: Mapping[str, str], body: bytes) -> tuple[int, dict[str, Any]]:
        value = json.loads(body)
        self.calls.append((path, value))
        ref = self.messages.setdefault(value['idempotency_key'], len(self.messages) + 1)
        if path == '/notify' and self.on_notify:
            self.on_notify(value)
        return 202, {'message_ref': ref}


class DriveNotificationTests(guess_tests.DriveGuessFixture):
    def setUp(self) -> None:
        super().setUp()
        self.gateway = FakeGateway()
        self.env = dict(T6_SERVER_URL='https://synthetic.invalid', T6_WORKER_SECRET='synthetic', T6_WORKER_QUEUE='main',
                        T6_API_URL='https://synthetic.invalid', T6_DATABASE=str(self.folder / 't6.sqlite'),
                        T6_DRIVE_RECORDING_CHAT_ID='-900001', T6_GATEWAY_URL='https://gateway.invalid', T6_GATEWAY_SECRET='synthetic',
                        T6_REVIEW_CHAT='-900003', T6_DRIVE_DM_CHAT_ID='-900002', T6_DRIVE_DM_TOPIC_ID='42')
        self.enterContext(patch.dict(os.environ, {'T6_DRIVE_PHOTO_DIR': str(self.folder / 'photos')}))
        self.restart_worker()

    def restart_worker(self) -> None:
        with patch('t6.worker.__main__.HttpTransport', return_value=self.gateway):
            handlers = configured_handlers(self.env, WorkerSettings.from_env(self.env), HttpTransport('https://synthetic.invalid'))['main']
        self.resolver = CaptureResolver(self.registry, self.fake, environ=self.env, source=self.notion_source)
        handlers['drive.capture.resolve'] = self.resolver.handle
        self.worker = Worker(InternalQueueClient(self.queue), 'main', handlers)

    def drain(self) -> None:
        for _ in range(20):
            if not self.worker.run_once():
                return
        self.fail('Worker did not drain')

    def capture(self, text: str, *, with_notion: bool = True, **values: Any) -> dict[str, Any]:
        self.sequence += 1
        captured = self.http('drive/capture', chat_id=-900001, message_id=self.sequence, sender_telegram_id=900010,
                             posted_at='2026-11-24T10:30:00+08:00', text=text,
                             **{'photo': base64.b64encode(photo(str(self.folder)).data).decode(), **values})
        self.drain()
        return self.http('drive/capture/result', capture=captured['capture'])

    def test_each_item_sends_one_dm_with_photo_caption_post_link_and_choices(self) -> None:
        self.workbook({'SYNTHETIC SHOW': [HEADER, *[[f'SYN D0{i}', f'SAMPLE-0{i}'] for i in range(1, 5)]]})
        self.reload()
        caption = 'OUT SYN\nUnrelated zebra\n48 FD\nR: Synthetic <recipient>'
        result = self.capture(caption)
        self.assertEqual(len(self.gateway.calls), 2)
        (_, first), (_, second) = self.gateway.calls
        self.assertEqual((first['to'], first['topic']), (-900002, 42))
        self.assertTrue(first['text'].startswith('Practice\n'))
        self.assertIn('OUT SYN\nUnrelated zebra\n48 FD\nR: Synthetic &lt;recipient&gt;', first['text'])
        self.assertIn('https://t.me/c/900001/1', first['text'])
        self.assertEqual(first['photo'], base64.b64encode(photo(str(self.folder)).data).decode())
        labels = [b['text'] for row in first['buttons'] for b in row]
        self.assertEqual(len(labels), 7)  # three guesses plus the four other choices
        self.assertEqual(labels[-4:], ['New drive', 'Not a drive', 'Type it', 'None'])
        self.assertIn('SYN D01', labels[0])
        self.assertIn('SYNTHETIC SHOW', labels[0])
        self.assertIn('Last seen:', first['text'])
        self.assertEqual([b['text'] for row in second['buttons'] for b in row], labels[-4:])
        self.restart_worker()
        self.http('drive/capture', chat_id=-900001, message_id=1, sender_telegram_id=900010,
                  posted_at='2026-11-24T10:30:00+08:00', text=caption)
        self.drain()
        self.assertEqual(len(self.gateway.calls), 2)
        self.assertEqual(len(self.inbox()), 2)
        self.assertEqual(result['items'][0]['state'], 'open')

    def test_inbox_approval_edits_original_dm_once_and_removes_buttons(self) -> None:
        self.workbook({'SYNTHETIC SHOW': [HEADER, ['SYN D01', 'SAMPLE-01'], ['SYN D02', 'SAMPLE-02']]})
        self.reload()
        result = self.capture('OUT SYN\nR: Synthetic recipient')
        drive = self.inventory()['SYN D02']['id']
        values = dict(capture=result['capture'], item=0, choice='drive', drive=drive)
        self.http('drive/approve', **values)
        self.drain()
        self.assertEqual([path for path, _ in self.gateway.calls], ['/notify', '/edit'])
        edit = self.gateway.calls[-1][1]
        self.assertEqual((edit['message_ref'], edit['buttons']), (1, []))
        self.assertIn('Approved: SYN D02', edit['text'])
        self.assertTrue(edit['text'].startswith('Practice\n'))
        self.assertEqual(self.inbox(), [])
        self.http('drive/approve', **values)
        self.restart_worker()
        self.drain()
        self.assertEqual(len(self.gateway.calls), 2)

    def tap(self, data: str, *, telegram: int = 900010) -> dict[str, Any]:
        from urllib.request import Request, urlopen
        request = Request(f'http://127.0.0.1:{self.server.server_port}/tap',
                          data=json.dumps({'data': data, 'from': {'id': telegram}, 'chat': -900002}).encode(),
                          headers={'Authorization': 'Bearer synthetic-gateway', 'X-Telegram-User-Id': str(telegram)})
        with urlopen(request) as response:
            return dict(json.load(response))

    def test_dm_guess_button_calls_approve_only_for_configured_custodian(self) -> None:
        self.enterContext(patch.dict(os.environ, {'T6_DRIVE_CUSTODIAN_PERSON_ID': 't6n_synthetickeeper'}))
        with self.registry._transaction() as db:
            db.execute("UPDATE persons SET telegram_id=900010 WHERE id='t6n_synthetickeeper'")
            db.execute("INSERT INTO persons VALUES ('t6n_syntheticother', 'staff', '[]', '[]', 900011, 0, 1)")
            db.execute("INSERT INTO person_permissions VALUES ('t6n_syntheticother', 'drives')")
        self.workbook({'SYNTHETIC SHOW': [HEADER, ['SYN D01', 'SAMPLE-01']]})
        self.reload()
        self.capture('OUT SYN\nR: Synthetic recipient')
        button = self.gateway.calls[0][1]['buttons'][0][0]
        self.assertLessEqual(len(('mod:t6core:' + button['payload']).encode()), 64)
        self.assertEqual(self.tap(button['payload'], telegram=900011)['outcome'], 'rejected')
        self.assertEqual(len(self.inbox()), 1)
        self.assertEqual(self.tap(button['payload'])['outcome'], 'done')
        self.drain()
        self.assertEqual(self.inbox(), [])
        self.assertIn('Approved: SYN D01', self.gateway.calls[-1][1]['text'])

    def test_other_dm_choices_settle_or_request_typed_drive_without_another_dm(self) -> None:
        self.enterContext(patch.dict(os.environ, {'T6_DRIVE_CUSTODIAN_PERSON_ID': 't6n_synthetickeeper'}))
        with self.registry._transaction() as db:
            db.execute("UPDATE persons SET telegram_id=900010 WHERE id='t6n_synthetickeeper'")
        for label, state, wording in (('None', 'open', 'waiting in Inbox'), ('Not a drive', 'not_a_drive', 'Not a drive'),
                                       ('New drive', 'approved', 'Approved: Synthetic new box'), ('Type it', 'open', 'Type a Drive label or serial')):
            with self.subTest(label=label):
                result = self.capture('OUT Synthetic new box\nR: Synthetic recipient', photo='')
                sent = self.gateway.calls[-1][1]
                buttons = {b['text']: b['payload'] for row in sent['buttons'] for b in row}
                answer = self.tap(buttons[label])
                if label == 'Type it':
                    self.assertEqual(answer['outcome'], 'needs_person')
                    self.assertIn(wording, answer['reason'])
                    self.assertEqual(answer['inputs'], {'capture': result['capture'], 'item': 0, 'choice': 'drive'})
                    self.assertEqual(answer['verb'], 'drive approve')
                    self.assertEqual(self.http('drive/capture/result', capture=result['capture'])['items'][0]['state'], 'open')
                    # The gateway submits the typed answer through the same HTTP verb.
                    drive = next(iter(self.inventory().values()))['id']
                    serial = self.http('drive/get', drive=drive)['drive']['serial']
                    self.http('drive/approve', capture=result['capture'], item=0, choice='drive', typed=serial or 'Synthetic new box')
                else:
                    self.assertEqual((answer['outcome'], answer['result']['state']), ('done', state))
                self.drain()
                self.assertEqual(self.gateway.calls[-1][0], '/edit')
                if label != 'Type it':
                    self.assertIn(wording, self.gateway.calls[-1][1]['text'])
                self.assertEqual(sum(path == '/notify' for path, _ in self.gateway.calls), self.sequence)

    def test_cutover_routes_to_private_chat_even_if_practice_target_remains_configured(self) -> None:
        import threading
        from t6.worker.__main__ import main
        self.env['T6_DRIVE_CUSTODIAN_PERSON_ID'] = 't6n_synthetickeeper'
        with self.registry._transaction() as db:
            db.execute("UPDATE persons SET telegram_id=900010 WHERE id='t6n_synthetickeeper'")
        self.capture('OUT Practice box\nR: Synthetic recipient', photo='')
        stop = threading.Event()
        stop.set()
        startup = {k: v for k, v in self.env.items() if not k.startswith('T6_GATEWAY_')}
        self.assertEqual(main({**startup, 'T6_DRIVE_LIVE': 'true'}, {'main': {}}, stop=stop), 0)
        self.restart_worker()
        self.capture('OUT Live box\nR: Synthetic recipient', photo='')
        self.assertEqual(len(self.gateway.calls), 2)
        practice, live = [message for _, message in self.gateway.calls]
        self.assertEqual((practice['to'], practice['topic']), (-900002, 42))
        self.assertEqual(live['to'], 900010)
        self.assertNotIn('topic', live)
        self.assertNotIn('Practice', live['text'])
        self.assertFalse(self.http('drive/inbox')['practice'])

    def test_no_configured_practice_target_sends_no_dm_but_keeps_item(self) -> None:
        self.env.pop('T6_DRIVE_DM_CHAT_ID')
        self.restart_worker()
        self.capture('OUT Synthetic box\nR: Synthetic recipient', photo='')
        self.assertEqual(self.gateway.calls, [])
        self.assertEqual([i['text'] for i in self.inbox()], ['Synthetic box'])

    def test_auto_approve_only_suppresses_exact_095_dm_and_keeps_inbox_prepicked(self) -> None:
        self.workbook({'SYNTHETIC SHOW': [HEADER, ['Box 14', 'SAMPLE-14']]})
        self.reload()
        drive = self.inventory()['Box 14']['id']
        for _ in range(3):
            taught = self.capture('OUT MC7 D21\nR: Synthetic recipient')
            self.http('drive/approve', capture=taught['capture'], item=0, choice='drive', drive=drive)
            self.http('drive/capture/undo', capture=taught['capture'])
            self.drain()
        self.gateway.calls.clear()
        for setting, expected_messages in ((None, 1), ('true', 0), ('false', 1)):
            with self.subTest(setting=setting):
                if setting is None:
                    self.env.pop('T6_DRIVE_AUTO_APPROVE', None)
                else:
                    self.env['T6_DRIVE_AUTO_APPROVE'] = setting
                self.restart_worker()
                result = self.capture('OUT MC7 D21\nR: Synthetic recipient')
                self.assertEqual(len(self.gateway.calls), expected_messages)
                (item,) = self.inbox()
                self.assertEqual((item['guesses'][0]['drive'], item['guesses'][0]['confidence'], item['guesses'][0]['exact']), (drive, 0.95, True))
                self.assertEqual((result['items'][0]['state'], result['items'][0]['drive']), ('open', drive))
                self.assertTrue(self.movements()[-1]['to_review'])
                if expected_messages:
                    self.assertIn('✓ Box 14', self.gateway.calls[0][1]['buttons'][0][0]['text'])
                self.http('drive/capture/undo', capture=result['capture'])
                self.drain()
                self.gateway.calls.clear()

    def test_morning_combines_unfinished_items_and_overdue_drives_once_per_manila_day(self) -> None:
        from datetime import date, datetime
        from t6.drives.reminder import MorningReminder
        from t6.worker.gateway import GatewayClient
        from t6.worker.internal_queue import InternalJobQueue
        self.workbook({'SYNTHETIC SHOW': [HEADER, ['T6 Synthetic overdue', 'SAMPLE-OLD', None, None, None,
                        date(2026, 10, 1), date(2026, 11, 1), 'Synthetic holder']]})
        self.reload()
        waiting = self.capture('OUT Synthetic <waiting>\nR: Synthetic recipient', photo='')
        self.http('drive/approve', capture=waiting['capture'], item=0, choice='none')
        settled = self.capture('OUT Synthetic settled\nR: Synthetic recipient', photo='')
        self.http('drive/approve', capture=settled['capture'], item=0, choice='not_a_drive')
        self.capture('OUT Synthetic problem', photo='')
        self.drain()
        self.gateway.calls.clear()
        now = [datetime.fromisoformat('2026-11-24T07:59:00+08:00').timestamp()]
        queue = InternalJobQueue(self.registry, clock=lambda: now[0])
        with patch.dict(os.environ, self.env):
            reminder = MorningReminder(self.people, queue, GatewayClient(self.gateway, 'synthetic'), '', clock=lambda: now[0])
            reminder.schedule()
            worker = Worker(InternalQueueClient(queue), 'main', {'drive.reminder': reminder.handle})
            self.assertFalse(worker.run_once())
            now[0] += 60
            self.assertTrue(worker.run_once())
        self.assertEqual(len(self.gateway.calls), 1)
        path, message = self.gateway.calls[0]
        self.assertEqual((path, message['to'], message['topic']), ('/notify', -900002, 42))
        self.assertIn('T6 Synthetic overdue', message['text'])
        self.assertIn('Unfinished Items', message['text'])
        self.assertIn('Synthetic &lt;waiting&gt;', message['text'])
        self.assertNotIn('Synthetic settled', message['text'])
        self.assertNotIn('Synthetic problem', message['text'])
        self.assertIn('Practice', message['text'])
        self.assertEqual(message['idempotency_key'], 'drive-reminder:2026-11-24')
        with patch.dict(os.environ, self.env):
            restarted = MorningReminder(self.people, queue, GatewayClient(self.gateway, 'synthetic'), '', clock=lambda: now[0])
            restarted.schedule()
            self.assertFalse(Worker(InternalQueueClient(queue), 'main', {'drive.reminder': restarted.handle}).run_once())
        self.assertEqual(len(self.gateway.calls), 1)

    def test_approval_during_gateway_send_still_edits_the_sent_dm(self) -> None:
        def approve_while_sending(message: dict[str, Any]) -> None:
            data = message['buttons'][0][0]['payload'].split(':')
            self.http('drive/approve', capture='capture_' + data[1], item=int(data[2]), choice='not_a_drive')
        self.gateway.on_notify = approve_while_sending
        self.capture('OUT Synthetic box\nR: Synthetic recipient', photo='')
        self.assertEqual(self.inbox(), [])
        self.assertEqual([path for path, _ in self.gateway.calls], ['/notify', '/edit'])
        self.assertIn('Not a drive', self.gateway.calls[-1][1]['text'])

    def test_auto_approve_on_still_sends_nonexact_and_unmatched_items(self) -> None:
        self.env['T6_DRIVE_AUTO_APPROVE'] = 'true'
        self.restart_worker()
        self.workbook({'SYNTHETIC SHOW': [HEADER, ['SYN D01', 'SAMPLE-01']]})
        self.reload()
        for confidence in (0.95, 0.99):
            with self.subTest(confidence=confidence):
                self.fake.reply(json.dumps({'guesses': [{'letter': 'A', 'confidence': confidence, 'reason': 'Synthetic box'}], 'label_text': None}))
                result = self.capture('OUT SYN D01\nR: Synthetic recipient')
                self.assertFalse(result['items'][0]['guesses'][0]['exact'])
                self.assertEqual(self.gateway.calls[-1][0], '/notify')
                self.http('drive/capture/undo', capture=result['capture'])
        self.capture('OUT Unmatched box\nR: Synthetic recipient', photo='')
        self.assertEqual(sum(path == '/notify' for path, _ in self.gateway.calls), 3)
        self.assertEqual(len(self.inbox()), 1)

    def test_inbox_only_morning_uses_the_shared_target_and_missing_target_sends_nothing(self) -> None:
        from datetime import datetime
        from t6.drives.reminder import MorningReminder
        from t6.worker.gateway import GatewayClient
        from t6.worker.internal_queue import InternalJobQueue
        self.capture('OUT Synthetic waiting\nR: Synthetic recipient', photo='')
        self.gateway.calls.clear()
        now = [datetime.fromisoformat('2026-11-24T08:00:00+08:00').timestamp()]
        queue = InternalJobQueue(self.registry, clock=lambda: now[0])
        reminder = MorningReminder(self.people, queue, GatewayClient(self.gateway, 'synthetic'), '',
                                   clock=lambda: now[0], environ=self.env)
        worker = Worker(InternalQueueClient(queue), 'main', {'drive.reminder': reminder.handle})
        reminder.schedule()
        self.assertTrue(worker.run_once())
        self.assertEqual(len(self.gateway.calls), 1)
        self.assertIn('Unfinished Items\nSynthetic waiting', self.gateway.calls[0][1]['text'])
        self.assertEqual((self.gateway.calls[0][1]['to'], self.gateway.calls[0][1]['topic']), (-900002, 42))
        self.env.pop('T6_DRIVE_DM_CHAT_ID')
        now[0] += 86400
        self.assertTrue(worker.run_once())
        self.assertEqual(len(self.gateway.calls), 1)

    def test_guess_shows_project_code_and_last_seen_before_this_capture(self) -> None:
        from datetime import date
        project = self.project('Synthetic Nebula', 'SNB')
        self.workbook({'NEBULA': [HEADER, ['Box 14', 'SAMPLE-14', None, None, None,
                        date(2026, 10, 1), date(2026, 11, 1), 'Synthetic previous holder']]})
        self.mapped({'NEBULA': project})
        self.capture('IN SNB D01')
        sent = self.gateway.calls[0][1]
        self.assertIn('Box 14 — SNB', sent['buttons'][0][0]['text'])
        self.assertIn('Last seen: released — 2026-11-01 — Synthetic previous holder', sent['text'])
        self.assertNotIn('2026-11-24', sent['text'])

    def test_buttons_use_the_existing_gateway_payload_envelope(self) -> None:
        self.capture('OUT Synthetic box\nR: Synthetic recipient', photo='')
        buttons = self.gateway.calls[0][1]['buttons']
        self.assertTrue(all(set(button) == {'text', 'payload'} for row in buttons for button in row))
