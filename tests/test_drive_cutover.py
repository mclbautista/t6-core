"""Practice and cutover through the Drive verbs on a temporary database."""
from t6.person import People
from t6.preview import PreviewTokens
from t6.registry import SqliteProjectRegistry
from t6.verbs import Done, Preview, Refused
from test_drive_reload import DriveReloadFixture, HEADER


class PracticeTests(DriveReloadFixture):
    def test_practice_is_default_persisted_and_marks_reads_and_reload_replies(self) -> None:
        self.assertTrue(self.call('list').result['practice'])
        self.workbook({'SYNTHETIC PRACTICE': [HEADER, ['Synthetic original', 'SAMPLE-PRACTICE']]})
        preview = self.call('reload', workbook=str(self.path), idempotency_key='practice')
        self.assertTrue(preview.plan['practice'])
        result = self.call('reload', workbook=str(self.path), idempotency_key='practice', confirm=preview.token)
        self.assertTrue(result.result['practice'])
        drive = self.drives()[0]['id']
        self.assertTrue(self.call('get', drive=drive).result['practice'])
        self.registry = SqliteProjectRegistry(self.folder / 't6.sqlite')
        self.people = People(self.registry, PreviewTokens('test'))
        self.assertTrue(self.call('list').result['practice'])
        self.workbook({'SYNTHETIC REPLACEMENT': [HEADER, ['Synthetic new', 'SAMPLE-NEW']]})
        self.reload('practice-wipe')
        self.assertEqual([d['serial'] for d in self.drives()], ['SAMPLE-NEW'])

    def test_configured_sheets_read_failure_preserves_last_load_and_reports_failure(self) -> None:
        import json
        import os
        from unittest.mock import patch
        from test_drive_sheets import SheetsHttp, account
        credential = self.folder / 'synthetic-account.json'
        key = account()
        credential.write_text(json.dumps({'client_email': key.email, 'private_key': key.private_key,
                                          'token_uri': key.token_uri}))
        http = SheetsHttp()
        self.workbook({'SYNTHETIC BEFORE': [HEADER, ['Synthetic original', 'SAMPLE-ORIGINAL']]})
        self.reload()
        before = self.drives()
        with patch.dict(os.environ, {'T6_DRIVE_PROJECT_SPREADSHEET_ID': 'synthetic-projects',
                                     'T6_DRIVE_TRACKER_SPREADSHEET_ID': 'synthetic-tracker',
                                     'GOOGLE_APPLICATION_CREDENTIALS': str(credential)}), \
                patch('t6.google.http.UrllibHttp.send', side_effect=http.send):
            http.fail = True
            failed = self.call('reload', idempotency_key='failed-read')
            self.assertIsInstance(failed, Refused)
            self.assertEqual(failed.rule, 'sheet')
            self.assertIn('503', failed.reason)
            self.assertEqual(self.drives(), before)
            http.fail = False
            preview = self.call('reload', idempotency_key='configured-read')
            self.assertIsInstance(preview, Preview)
            self.assertEqual(http.ids, ['synthetic-projects', 'synthetic-tracker'])

    def test_nightly_queues_once_per_manila_day_and_failed_job_keeps_data(self) -> None:
        from datetime import datetime
        from t6.drives.nightly import NightlyReload
        from t6.worker import Worker
        from t6.worker.internal_queue import InternalJobQueue, InternalQueueClient
        now = [datetime.fromisoformat('2026-11-23T23:59:00+08:00').timestamp()]
        self.workbook({'SYNTHETIC BEFORE': [HEADER, ['Synthetic original', 'SAMPLE-ORIGINAL']]})
        self.reload()
        before = self.drives()
        queue = InternalJobQueue(self.registry, clock=lambda: now[0])
        nightly = NightlyReload(self.registry, PreviewTokens('test'), queue, clock=lambda: now[0], environ={})
        nightly.schedule()
        nightly.schedule()  # worker restarts must not duplicate the day's job
        self.assertEqual(len(queue.find('drive.reload')), 1)
        self.assertIsNone(queue.claim('main', 60))
        now[0] += 60
        worker = Worker(InternalQueueClient(queue), 'main', {'drive.reload': nightly.handle})
        self.assertTrue(worker.run_once())  # missing sheet configuration fails the read
        jobs = queue.find('drive.reload')
        self.assertEqual(len(jobs), 2)  # next night still runs after a failed read
        result = queue.result(jobs[0].job_id, 'main')
        assert result is not None
        self.assertEqual(result.status, 'failed')
        self.assertIn('T6_DRIVE_PROJECT_SPREADSHEET_ID', str(result.value))
        self.assertEqual(self.drives(), before)
        self.assertFalse(worker.run_once())
        nightly.schedule()
        self.assertEqual(len(queue.find('drive.reload')), 2)

    def test_cutover_final_reload_persists_off_and_disables_even_queued_nightly_jobs(self) -> None:
        from t6.drives.nightly import NightlyReload
        from t6.worker.internal_queue import InternalJobQueue, InternalQueueClient
        from t6.worker import Worker
        with self.registry._transaction() as db:
            db.execute("INSERT INTO person_permissions VALUES ('synthetic-keeper', 'operator')")
        queue = InternalJobQueue(self.registry)
        nightly = NightlyReload(self.registry, PreviewTokens('test'), queue)
        nightly.schedule()
        self.workbook({'SYNTHETIC BEFORE': [HEADER, ['Synthetic old', 'SAMPLE-OLD']]})
        self.reload()
        self.workbook({'SYNTHETIC FINAL': [HEADER, ['Synthetic final', 'SAMPLE-FINAL']]})
        values = {'workbook': str(self.path), 'idempotency_key': 'cutover'}
        preview = self.call('cutover', **values)
        self.assertIsInstance(preview, Preview)
        self.assertTrue(self.call('list').result['practice'])
        self.assertEqual([d['serial'] for d in self.drives()], ['SAMPLE-OLD'])
        done = self.call('cutover', **values, confirm=preview.token)
        self.assertIsInstance(done, Done)
        self.assertFalse(done.result['practice'])
        self.assertFalse(done.result['nightly_enabled'])
        self.assertEqual([d['serial'] for d in self.drives()], ['SAMPLE-FINAL'])
        self.registry = SqliteProjectRegistry(self.folder / 't6.sqlite')
        self.people = People(self.registry, PreviewTokens('test'))
        self.assertFalse(self.call('list').result['practice'])
        self.path.unlink()  # refusal must precede any source read
        refused = self.call('reload', workbook=str(self.path), idempotency_key='too-late')
        self.assertIsInstance(refused, Refused)
        self.assertEqual(refused.rule, 'cutover')
        nightly.schedule()
        self.assertEqual(len(queue.find('drive.reload')), 1)
        queue.submit('main', 'drive.reload', 'synthetic-already-queued')
        worker = Worker(InternalQueueClient(queue), 'main', {'drive.reload': nightly.handle})
        self.assertTrue(worker.run_once())
        self.assertEqual([d['serial'] for d in self.drives()], ['SAMPLE-FINAL'])


    def test_worker_startup_registers_and_restores_nightly_schedule(self) -> None:
        from t6.worker.__main__ import main
        from t6.worker.internal_queue import InternalJobQueue
        from test_worker_main import ENVIRON
        # An already-stopped worker still configures its handlers and durable timers.
        import threading
        stop = threading.Event()
        stop.set()
        env = {**ENVIRON, 'T6_DATABASE': str(self.folder / 't6.sqlite'),
               'T6_API_URL': 'https://synthetic.invalid',
               'T6_PREVIEW_SIGNING_KEY': 'test',
               'T6_DRIVE_PROJECT_SPREADSHEET_ID': 'synthetic-projects',
               'T6_DRIVE_TRACKER_SPREADSHEET_ID': 'synthetic-tracker'}
        for _ in range(2):
            self.assertEqual(main(env, {'main': {}}, stop=stop), 0)
        self.assertEqual(len(InternalJobQueue(self.registry).find('drive.reload')), 1)

    def test_failed_final_read_does_not_end_practice_or_disable_nightly(self) -> None:
        from t6.drives.nightly import NightlyReload
        from t6.worker.internal_queue import InternalJobQueue
        with self.registry._transaction() as db:
            db.execute("INSERT INTO person_permissions VALUES ('synthetic-keeper', 'operator')")
        self.workbook({'SYNTHETIC FINAL': [HEADER, ['Synthetic final', 'SAMPLE-FINAL']]})
        self.reload()
        before = self.drives()
        values = {'workbook': str(self.path), 'idempotency_key': 'failed-cutover'}
        preview = self.call('cutover', **values)
        self.path.unlink()
        failed = self.call('cutover', **values, confirm=preview.token)
        self.assertIsInstance(failed, Refused)
        self.assertEqual(failed.rule, 'sheet')
        self.assertTrue(self.call('list').result['practice'])
        self.assertEqual(self.drives(), before)
        queue = InternalJobQueue(self.registry)
        NightlyReload(self.registry, PreviewTokens('test'), queue).schedule()
        self.assertEqual(len(queue.find('drive.reload')), 1)

    def test_deployment_lists_sheet_configuration_keys(self) -> None:
        from pathlib import Path
        notes = (Path(__file__).parents[1] / 'docs/deployment.md').read_text()
        for key in ('T6_DRIVE_PROJECT_SPREADSHEET_ID', 'T6_DRIVE_TRACKER_SPREADSHEET_ID',
                    'GOOGLE_APPLICATION_CREDENTIALS', 'T6_DATABASE', 'T6_PREVIEW_SIGNING_KEY'):
            self.assertIn(key, notes)


    def test_nightly_success_replaces_data_from_both_configured_sources(self) -> None:
        import json
        from unittest.mock import patch
        from test_drive_sheets import SheetsHttp, account
        from t6.drives.nightly import NightlyReload
        from t6.worker import Worker
        from t6.worker.internal_queue import InternalJobQueue, InternalQueueClient
        self.workbook({'SYNTHETIC BEFORE': [HEADER, ['Synthetic previous', 'SAMPLE-PREVIOUS']]})
        self.reload()
        key = account()
        credential = self.folder / 'synthetic-credential.json'
        credential.write_text(json.dumps({'client_email': key.email, 'private_key': key.private_key,
                                          'token_uri': key.token_uri}))
        http = SheetsHttp()
        http.payload = {'sheets': [{'properties': {'title': 'SYNTHETIC PROJECT'}, 'data': [{'rowData': [
            {'values': [{'effectiveValue': {'stringValue': column}} for column in HEADER]},
            {'values': [{'effectiveValue': {'stringValue': 'Synthetic nightly'}},
                        {'effectiveValue': {'stringValue': 'SAMPLE-NIGHTLY'}}]}]}]}]}
        queue = InternalJobQueue(self.registry)
        job = queue.submit('main', 'drive.reload', 'synthetic-night')
        env = {'T6_DRIVE_PROJECT_SPREADSHEET_ID': 'synthetic-projects',
               'T6_DRIVE_TRACKER_SPREADSHEET_ID': 'synthetic-tracker',
               'GOOGLE_APPLICATION_CREDENTIALS': str(credential)}
        nightly = NightlyReload(self.registry, PreviewTokens('test'), queue, environ=env)
        with patch('t6.google.http.UrllibHttp.send', side_effect=http.send):
            worker = Worker(InternalQueueClient(queue), 'main', {'drive.reload': nightly.handle})
            self.assertTrue(worker.run_once())
        result = queue.result(job.job_id, 'main')
        assert result is not None
        self.assertEqual(result.status, 'ok')
        self.assertEqual(http.ids, ['synthetic-projects', 'synthetic-tracker'])
        self.assertEqual([d['serial'] for d in self.drives()], ['SAMPLE-NIGHTLY'])
        self.assertTrue(self.call('list').result['practice'])
