"""Overdue Drive reads and morning delivery with synthetic, temporary records."""
from datetime import date, datetime

from test_drive_reload import DriveReloadFixture, HEADER


class DriveReminderTests(DriveReloadFixture):
    def setUp(self) -> None:
        super().setUp()
        self.now = [datetime.fromisoformat('2026-11-24T00:30:00+08:00').timestamp()]
        self.registry._clock = lambda: self.now[0]

    def seed_overdue(self) -> None:
        def row(label: str, serial: str, released: date | None) -> list[object]:
            return [label, serial, None, None, None, date(2026, 10, 1), released, 'Synthetic holder <A>']
        self.workbook({
            'SYNTHETIC PROJECT': [HEADER,
                row('T6 Synthetic fifteen', 'SAMPLE-FIFTEEN', date(2026, 11, 9)),
                row('T6 Synthetic fourteen', 'SAMPLE-FOURTEEN', date(2026, 11, 10)),
                row('Synthetic unknown owner', 'SAMPLE-UNKNOWN', date(2026, 11, 1)),
                row('T6 Synthetic oldest', 'SAMPLE-OLDEST', date(2026, 11, 1)),
                row('T6 Synthetic returned', 'SAMPLE-RETURNED', date(2026, 11, 1)),
                row('T6 Synthetic retired', 'SAMPLE-RETIRED', date(2026, 11, 1))],
            'SYNTHETIC RETURN': [HEADER, ['T6 Synthetic returned', 'SAMPLE-RETURNED', None, None, None, date(2026, 11, 20)]],
            'DEAD DRIVES': [HEADER, ['T6 Synthetic retired', 'SAMPLE-RETIRED']],
        })
        self.reload()

    def test_read_lists_only_t6_last_releases_older_than_fourteen_manila_days_oldest_first(self) -> None:
        self.seed_overdue()
        result = self.call('overdue').result
        ids = {drive['serial']: drive['id'] for drive in self.drives()}
        self.assertEqual(result['overdue'], [
            {'drive_id': ids['SAMPLE-OLDEST'], 'label': 'T6 Synthetic oldest', 'serial': 'SAMPLE-OLDEST',
             'person': 'Synthetic holder <A>', 'released_on': '2026-11-01', 'days_out': 23},
            {'drive_id': ids['SAMPLE-FIFTEEN'], 'label': 'T6 Synthetic fifteen', 'serial': 'SAMPLE-FIFTEEN',
             'person': 'Synthetic holder <A>', 'released_on': '2026-11-09', 'days_out': 15},
        ])
        self.assertTrue(result['practice'])

    def reminder_worker(self) -> None:
        from t6.drives.reminder import MorningReminder
        from t6.worker import Worker
        from t6.worker.gateway import GatewayClient
        from t6.worker.internal_queue import InternalJobQueue, InternalQueueClient
        from t6.preview import PreviewTokens
        from t6.person import People
        from typing import Any

        class Gateway:
            def __init__(self) -> None:
                self.calls: list[dict[str, Any]] = []

            def send(self, method: str, path: str, headers: object, body: bytes) -> tuple[int, dict[str, Any]]:
                import json
                self.calls.append(json.loads(body))
                return 202, {'message_ref': len(self.calls)}

        with self.registry._transaction() as db:
            db.execute("UPDATE persons SET telegram_id = 700000001 WHERE id = 'synthetic-keeper'")
        queue = InternalJobQueue(self.registry, clock=lambda: self.now[0])
        gateway = Gateway()
        reminder = MorningReminder(People(self.registry, PreviewTokens('test')), queue,
                                   GatewayClient(gateway, 'synthetic-secret'), 'synthetic-keeper', clock=lambda: self.now[0],
                                   environ={'T6_DRIVE_DM_CHAT_ID': '700000001'})
        worker = Worker(InternalQueueClient(queue), 'main', {'drive.reminder': reminder.handle})
        self.queue, self.gateway, self.reminder, self.worker = queue, gateway, reminder, worker

    def test_empty_read_sends_no_notification(self) -> None:
        self.reminder_worker()
        self.assertEqual(self.call('overdue').result['overdue'], [])
        self.reminder.schedule()
        self.now[0] = datetime.fromisoformat('2026-11-24T08:00:00+08:00').timestamp()
        self.assertTrue(self.worker.run_once())
        self.assertEqual(self.gateway.calls, [])

    def test_morning_sends_read_rows_once_per_manila_date_even_after_restart(self) -> None:
        self.seed_overdue()
        self.reminder_worker()
        self.reminder.schedule()
        self.reminder.schedule()
        self.assertFalse(self.worker.run_once())
        self.assertEqual(self.gateway.calls, [])
        self.now[0] = datetime.fromisoformat('2026-11-24T08:00:00+08:00').timestamp()
        expected = self.call('overdue').result['overdue']
        self.assertTrue(self.worker.run_once())
        self.assertEqual(len(self.gateway.calls), 1)
        sent = self.gateway.calls[0]
        self.assertEqual((sent['to'], sent['idempotency_key'], sent['buttons']),
                         (700000001, 'drive-reminder:2026-11-24', []))
        self.assertEqual(sent['text'], 'Practice\nT6 Drives OUT for more than 14 days\n'
                         'T6 Synthetic oldest — Synthetic holder &lt;A&gt; — 2026-11-01\n'
                         'T6 Synthetic fifteen — Synthetic holder &lt;A&gt; — 2026-11-09')
        job = self.queue.find('drive.reminder')[0]
        result = self.queue.result(job.job_id, 'main')
        assert result is not None
        assert isinstance(result.value, dict)
        self.assertEqual(result.value['overdue'], expected)
        self.reminder.schedule()  # A restarted worker keeps the date's durable job key.
        self.assertFalse(self.worker.run_once())
        self.assertEqual(len(self.gateway.calls), 1)
        self.now[0] = datetime.fromisoformat('2026-11-25T07:59:00+08:00').timestamp()
        self.assertFalse(self.worker.run_once())
        self.now[0] += 60
        self.assertTrue(self.worker.run_once())
        self.assertEqual(len(self.gateway.calls), 2)
        self.assertEqual(self.gateway.calls[1]['idempotency_key'], 'drive-reminder:2026-11-25')

    def test_startup_restores_one_morning_job_without_sheet_configuration(self) -> None:
        import threading
        from t6.worker.__main__ import main
        from t6.worker.internal_queue import InternalJobQueue
        from test_worker_main import ENVIRON
        stop = threading.Event()
        stop.set()
        env = {**ENVIRON, 'T6_DATABASE': str(self.folder / 't6.sqlite'),
               'T6_API_URL': 'https://synthetic.invalid', 'T6_GATEWAY_URL': 'https://synthetic.invalid',
               'T6_GATEWAY_SECRET': 'synthetic-secret', 'T6_DRIVE_CUSTODIAN_PERSON_ID': 'synthetic-keeper'}
        for _ in range(2):
            self.assertEqual(main(env, {'main': {}}, stop=stop), 0)
        self.assertEqual(len(InternalJobQueue(self.registry).find('drive.reminder')), 1)

    def test_missed_morning_jobs_do_not_duplicate_todays_reminder(self) -> None:
        self.seed_overdue()
        self.reminder_worker()
        self.reminder.schedule()
        self.now[0] = datetime.fromisoformat('2026-11-26T08:00:00+08:00').timestamp()
        self.reminder.schedule()
        self.assertTrue(self.worker.run_once())  # The missed date is obsolete.
        self.assertEqual(self.gateway.calls, [])
        self.assertTrue(self.worker.run_once())
        self.assertEqual(len(self.gateway.calls), 1)
        self.assertEqual(self.gateway.calls[0]['idempotency_key'], 'drive-reminder:2026-11-26')
        self.assertFalse(self.worker.run_once())
