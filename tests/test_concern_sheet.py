"""The Concerns sheet's Submit through the main worker handler: a real temporary t6 database and a fake VPS export endpoint (#735)."""
import contextlib
import csv
import io
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any

from t6.concern import ConcernStore
from t6.concern_export import ConcernsExportPush
from t6.concern_sheet import ConcernsSheetSubmit
from t6.domain.studio_time import MANILA
from t6.ids import EntityKind, T6Id
from t6.preview import PreviewTokens
from t6.registry import Registered
from t6.registry.sqlite import SqliteProjectRegistry
from t6.verbs import Done, Preview
from t6.worker.client import WorkerJob
from t6.worker.runner import Refused as JobRefused
from test_concern_export import PUSH, FakeVps

KEY = 'secret'
EMAIL = 'cyril@example.com'
STALE = 'Changed since your last refresh'


def manila(text: str) -> float:
    return datetime.fromisoformat(text).replace(tzinfo=MANILA).timestamp()


class FakeNames:
    def project(self, project: str) -> str:
        return 'Big Show'

    def person(self, person: str) -> str:
        return {'t6n_coordinator': 'Casey Coordinator'}.get(person, 'Someone')


class ConcernsSheetSubmitTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.now = manila('2026-11-24T10:00')
        self.registry = SqliteProjectRegistry(Path(tmp.name) / 't6.db', clock=lambda: self.now, new_id=lambda: str(T6Id.mint(EntityKind.PROJECT)))
        result = self.registry.register(actor='setup', idempotency_key='setup', notion_page_id='project')
        assert isinstance(result, Registered)
        self.project = result.project.id
        with self.registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES ('t6n_coordinator', 'staff', '[]', '[]', 101, 0, 1)")
            db.execute("UPDATE projects SET rotation = 'in' WHERE id = ?", (self.project,))
        self.vps = FakeVps(tmp.name)
        self.handler = self.make_handler(ConcernsExportPush(self.vps, PUSH))
        self.jobs = 0

    def make_handler(self, push: Any) -> ConcernsSheetSubmit:
        return ConcernsSheetSubmit(self.registry, PreviewTokens(KEY), push, FakeNames())

    def raise_concern(self, text: str = 'Waiting on client for KDM', **values: Any) -> str:
        store = ConcernStore(self.registry, PreviewTokens(KEY))
        values = {'project': self.project, 'owner': 't6n_coordinator', 'waiting_on': 'client', 'anchor': text, 'as_of': '2026-11-20', **values}
        preview = store.write('raise', values, actor='t6n_coordinator', idempotency_key='raise-' + text)
        assert isinstance(preview, Preview), preview
        done = store.write('raise', values, actor='t6n_coordinator', idempotency_key='raise-' + text, confirm=preview.token)
        assert isinstance(done, Done), done
        return str(done.result['concern_id'])

    def revision(self, concern: str) -> int:
        with self.registry._transaction() as db:
            return int(db.execute('SELECT revision FROM concerns WHERE id = ?', (concern,)).fetchone()[0])

    def row(self, concern: str, action: str = 'Keep', note: str = '', revision: int | None = None) -> dict[str, Any]:
        return {'concern': concern, 'revision': self.revision(concern) if revision is None else revision, 'action': action, 'note': note}

    def job(self, rows: list[dict[str, Any]], email: str = EMAIL, job_id: int | None = None) -> WorkerJob:
        self.jobs += 1
        payload: dict[str, Any] = {'id': f'click-{self.jobs}', 'email': email, 'rows': rows}
        return WorkerJob(job_id=job_id or self.jobs, queue='main', kind='sheets.concerns_submit', dedupe_key=f'k{self.jobs}', payload=payload, token='t', attempt=1)

    def submit(self, rows: list[dict[str, Any]], **kwargs: Any) -> list[str]:
        answer = self.handler.handle(self.job(rows, **kwargs))
        assert isinstance(answer, dict) and isinstance(answer['results'], list)
        return [str(item['result']) for item in answer['results']]  # type: ignore[index,call-overload]

    def pending(self) -> dict[str, str]:
        with self.registry._transaction() as db:
            return dict(db.execute('SELECT concern_id, actor FROM concern_pending_closes').fetchall())

    def exported(self) -> dict[str, dict[str, str]]:
        return {r['Concern id']: r for r in self.vps.served() or []}

    def notes(self, concern: str) -> list[tuple[str, str]]:
        with self.registry._transaction() as db:
            return [(r[0], r[1]) for r in db.execute('SELECT text, actor FROM concern_notes WHERE concern_id = ? ORDER BY id', (concern,))]

    def test_close_becomes_a_pending_close_by_the_sheet_actor(self) -> None:
        concern = self.raise_concern()
        self.assertEqual(self.submit([self.row(concern, 'Close')]), ['Closing after PSR'])
        self.assertEqual(self.pending(), {concern: 'sheet:' + EMAIL})
        self.assertEqual(self.exported()[concern]['State'], 'Closing after PSR')

    def test_undo_close_cancels_the_pending_close(self) -> None:
        concern = self.raise_concern()
        self.submit([self.row(concern, 'Close')])
        self.assertEqual(self.submit([self.row(concern, 'Undo close')]), ['Undone'])
        self.assertEqual(self.pending(), {})
        self.assertEqual(self.exported()[concern]['State'], 'Open')

    def test_a_note_alone_is_added_with_the_sheet_actor_and_text_is_kept_as_typed(self) -> None:
        concern = self.raise_concern()
        self.assertEqual(self.submit([self.row(concern, note='=SUM(A1) client called')]), ['Note added'])
        self.assertEqual(self.notes(concern), [('=SUM(A1) client called', 'sheet:' + EMAIL)])
        self.assertEqual(self.pending(), {})

    def test_a_note_with_a_close_is_added_first_then_the_close_is_pending(self) -> None:
        concern = self.raise_concern()
        self.assertEqual(self.submit([self.row(concern, 'Close', 'Client confirmed')]), ['Note added; Closing after PSR'])
        self.assertEqual((len(self.notes(concern)), list(self.pending())), (1, [concern]))

    def test_a_row_that_changed_since_the_refresh_is_refused_and_writes_nothing(self) -> None:
        concern = self.raise_concern()
        seen = self.revision(concern)
        self.submit([self.row(concern, note='someone else was first')])  # moves the revision on
        self.assertEqual(self.submit([self.row(concern, 'Close', 'late', revision=seen)]), [STALE])
        self.assertEqual((self.pending(), len(self.notes(concern))), ({}, 1))

    def test_a_date_rule_concern_is_refused_with_the_stores_own_reason(self) -> None:
        concern = self.raise_concern(source='date_rule', owner=None)  # raised as a rule's own Concern
        with self.registry._transaction() as db:
            db.execute('INSERT INTO concern_date_rules VALUES (?, ?, ?)', (self.project, 'submission_before_playdate', concern))
        self.assertEqual(self.submit([self.row(concern, 'Close')]), ['Date-sanity Concerns follow data only'])
        self.assertEqual(self.pending(), {})

    def test_the_stores_other_refusals_pass_through_unchanged(self) -> None:
        concern = self.raise_concern()
        self.submit([self.row(concern, 'Close')])
        self.assertEqual(self.submit([self.row(concern, 'Close')]), ['Concern cannot make that transition'])  # already pending
        other = self.raise_concern('Another client line')
        self.assertEqual(self.submit([self.row(other, 'Undo close')]), ['Concern cannot make that transition'])  # nothing to undo
        self.assertEqual(self.submit([self.row('t6c_missing', 'Close', revision=1)]), ['Concern not found'])

    def test_one_refused_row_does_not_stop_the_others(self) -> None:
        stale, fine, noted = (self.raise_concern(t) for t in ('client one', 'client two', 'client three'))
        results = self.submit([self.row(stale, 'Close', revision=99), self.row(fine, 'Close'), self.row(noted, note='hello')])
        self.assertEqual(results, [STALE, 'Closing after PSR', 'Note added'])
        self.assertEqual(list(self.pending()), [fine])

    def test_a_redelivered_job_writes_nothing_twice(self) -> None:
        concern = self.raise_concern()
        job = self.job([self.row(concern, 'Close', 'because')], job_id=7)
        first = self.handler.handle(job)
        audit, revision = self.count('audit'), self.revision(concern)
        again = self.handler.handle(job)
        self.assertEqual((again, self.count('audit'), self.revision(concern), len(self.notes(concern))), (first, audit, revision, 1))
        self.assertEqual(self.exported()[concern]['last result'], 'Note added; Closing after PSR')

    def test_a_job_that_stopped_after_the_note_finishes_the_close_when_retried(self) -> None:
        concern = self.raise_concern()
        job = self.job([self.row(concern, 'Close', 'because')], job_id=8)
        store = ConcernStore(self.registry, PreviewTokens(KEY))
        preview = store.write('note', {'concern': concern, 'text': 'because'}, actor='sheet:' + EMAIL, idempotency_key=f'sheet:8:{concern}:note')
        store.write('note', {'concern': concern, 'text': 'because'}, actor='sheet:' + EMAIL, idempotency_key=f'sheet:8:{concern}:note', confirm=preview.token)  # type: ignore[union-attr]
        self.assertEqual(self.handler.handle(job), {'results': [{'concern': concern, 'result': 'Note added; Closing after PSR'}]})
        self.assertEqual((len(self.notes(concern)), list(self.pending())), (1, [concern]))

    def count(self, table: str) -> int:
        with self.registry._transaction() as db:
            return int(db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0])

    def test_last_result_shows_in_the_next_export_by_concern_id_until_the_next_submit_for_it(self) -> None:
        shown, other = self.raise_concern('client shown'), self.raise_concern('client other')
        self.submit([self.row(shown, 'Close')])
        self.assertEqual({k: v['last result'] for k, v in self.exported().items()}, {shown: 'Closing after PSR', other: ''})
        self.submit([self.row(other, note='n')])
        self.assertEqual({k: v['last result'] for k, v in self.exported().items()}, {shown: 'Closing after PSR', other: 'Note added'})
        self.submit([self.row(shown, 'Undo close')])
        self.assertEqual(self.exported()[shown]['last result'], 'Undone')
        self.submit([self.row(shown, 'Undo close')])  # refused now: the refusal replaces it
        self.assertEqual(self.exported()[shown]['last result'], 'Concern cannot make that transition')

    def test_a_fresh_export_is_pushed_after_the_rows_with_names_and_the_sheet_email(self) -> None:
        concern = self.raise_concern()
        before = self.vps.pushes
        self.submit([self.row(concern, 'Close')])
        self.assertEqual(self.vps.pushes, before + 1)
        row = self.exported()[concern]
        self.assertEqual((row['Project code'], row['Project name'], row['owner'], row['close requested by'], row['State']),
                         ('', 'Big Show', 'Casey Coordinator', 'sheet:' + EMAIL, 'Closing after PSR'))

    def test_a_failed_push_is_logged_and_does_not_fail_the_job(self) -> None:
        concern = self.raise_concern()
        self.vps.down = True
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(self.submit([self.row(concern, 'Close')]), ['Closing after PSR'])
        self.assertIn('concerns export push failed: ConnectionRefusedError', err.getvalue())
        self.assertEqual(list(self.pending()), [concern])

    def test_with_the_push_off_the_rows_are_still_applied(self) -> None:
        concern = self.raise_concern()
        self.handler = self.make_handler(None)
        self.assertEqual(self.submit([self.row(concern, 'Close')]), ['Closing after PSR'])
        self.assertEqual(self.vps.pushes, 0)

    def test_a_malformed_submit_fails_the_job_for_good(self) -> None:
        payloads: list[dict[str, Any]] = [{'rows': []}, {'email': 'nobody', 'rows': []}, {'email': EMAIL}, {'email': EMAIL, 'rows': ['x']}]
        for payload in payloads:
            with self.subTest(payload=payload), self.assertRaises(JobRefused):
                self.handler.handle(WorkerJob(1, 'main', 'sheets.concerns_submit', 'k', payload, 't', 1))


class WorkerWiringTests(unittest.TestCase):
    def handlers(self, **extra: str) -> Any:
        from unittest.mock import patch
        from t6.worker.__main__ import WorkerSettings, configured_handlers
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        environ = {'T6_SERVER_URL': 'http://q', 'T6_WORKER_SECRET': 's', 'T6_WORKER_QUEUE': 'main', 'T6_API_URL': 'http://api', 'T6_DATABASE': tmp.name + '/t6.db', **extra}
        with patch('t6.worker.__main__.HttpTransport'), contextlib.redirect_stderr(io.StringIO()):
            return configured_handlers(environ, WorkerSettings.from_env(environ), None).get('main', {})  # type: ignore[arg-type]

    def test_the_main_worker_runs_the_submit_job_only_with_the_signing_key(self) -> None:
        self.assertIn('sheets.concerns_submit', self.handlers(T6_PREVIEW_SIGNING_KEY='k'))
        self.assertNotIn('sheets.concerns_submit', self.handlers())
