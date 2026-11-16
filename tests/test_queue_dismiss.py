"""Dismissing a failed job: the internal jobs in the t6 database (verb `queue dismiss`) and the edge queue file (admin command)."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_person import MemoryRegistry
from t6.ops import health
from t6.person import People
from t6.preview import PreviewTokens
from t6.queue_dismiss import JobDismissals
from t6.server import JobQueue
from t6.server.testing import FakeClock
from t6.verbs import VERBS, Done, Preview, QueueDismissInput, QueueFailedInput, Refused, invoke
from t6.worker.internal_queue import InternalJobQueue


class InternalDismissTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = MemoryRegistry()
        self.addCleanup(self.registry.db.close)
        self.operator, self.staff = 't6n_' + '1' * 32, 't6n_' + '2' * 32
        with self.registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES (?, 'staff', '[]', '[]', 41, 0, 1)", (self.operator,))
            db.execute("INSERT INTO person_permissions VALUES (?, 'operator')", (self.operator,))
            db.execute("INSERT INTO persons VALUES (?, 'staff', '[]', '[]', 42, 0, 1)", (self.staff,))
        self.tokens = PreviewTokens('test')
        self.queue = InternalJobQueue(self.registry, clock=lambda: 1000.0, max_attempts=1)
        self.dismissals = JobDismissals(self.registry, self.tokens)

    def dead_job(self, key: str = 'k1') -> int:
        job_id = self.queue.submit('main', 'sample.kind', key, {'n': 1}).job_id
        claim = self.queue.claim('main', 60)
        assert claim is not None and claim.job_id == job_id
        self.assertTrue(self.queue.fail(job_id, claim.token, 'boom', retry_in=1))
        self.assertEqual(self.job_state(job_id), 'dead')
        return job_id

    def job_state(self, job_id: int) -> str:
        with self.registry._transaction() as db:
            return str(db.execute('SELECT state FROM internal_jobs WHERE id = ?', (job_id,)).fetchone()[0])

    def dismiss(self, job: int, reason: str = 'data fixed by hand', key: str = 'dismiss', actor: str | None = None) -> Done:
        actor = actor or self.operator
        values = {'job': job, 'reason': reason}
        preview = self.dismissals.write(values, actor=actor, idempotency_key=key)
        assert isinstance(preview, Preview)
        done = self.dismissals.write(values, actor=actor, idempotency_key=key, confirm=preview.token)
        assert isinstance(done, Done)
        return done

    def test_a_dead_job_is_dismissed_with_a_reason_and_audited_and_the_count_goes_to_zero(self) -> None:
        job = self.dead_job()
        listed = self.dismissals.failed()
        self.assertEqual([(item['job'], item['kind'], item['failure']) for item in listed], [(job, 'sample.kind', 'boom')])
        done = self.dismiss(job)
        self.assertEqual((done.result['job'], done.result['state']), (job, 'dismissed'))
        self.assertEqual(self.job_state(job), 'dismissed')
        self.assertEqual(self.dismissals.failed(), [])
        entries = [entry for entry in self.registry.audit(f'internal-job:{job}') if entry.action == 'queue_dismiss']
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].actor, self.operator)
        self.assertIn('data fixed by hand', json.dumps(entries[0].after))
        result = self.queue.result(job, 'main')
        assert result is not None
        self.assertEqual((result.status, result.value), ('failed', 'boom'))  # a caller that waits still reads the failure
        self.assertIsNone(self.queue.claim('main', 60))  # never run again
        self.assertEqual(self.queue.submit('main', 'sample.kind', 'k1').created, False)  # its dedupe key stays taken

    def test_health_stops_counting_a_dismissed_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'registry.sqlite'
            from t6.registry import SqliteProjectRegistry
            registry = SqliteProjectRegistry(path)
            queue = InternalJobQueue(registry, clock=lambda: 1000.0, max_attempts=1)
            queue.submit('main', 'sample.kind', 'k1')
            claim = queue.claim('main', 60)
            assert claim is not None
            queue.fail(claim.job_id, claim.token, 'boom', retry_in=1)
            self.assertEqual(health({'registry': path}, now=1000.0, required=('registry',))['dead_internal_jobs'], {'sample.kind': 1})
            self.assertTrue(queue.dismiss(claim.job_id))
            self.assertEqual(health({'registry': path}, now=1000.0, required=('registry',))['dead_internal_jobs'], {})

    def test_only_a_dead_job_with_a_reason_can_be_dismissed(self) -> None:
        job = self.dead_job('k2')
        queued = self.queue.submit('main', 'sample.kind', 'waiting').job_id
        refused = self.dismissals.write({'job': queued, 'reason': 'x'}, actor=self.operator, idempotency_key='a')
        assert isinstance(refused, Refused)
        self.assertEqual(refused.rule, 'state')
        missing = self.dismissals.write({'job': 999, 'reason': 'x'}, actor=self.operator, idempotency_key='b')
        assert isinstance(missing, Refused)
        self.assertEqual(missing.rule, 'job')
        for reason in ('', '   '):
            blank = self.dismissals.write({'job': job, 'reason': reason}, actor=self.operator, idempotency_key='c')
            assert isinstance(blank, Refused)
            self.assertEqual(blank.rule, 'reason')
        self.assertEqual(self.job_state(job), 'dead')

    def test_a_retry_with_the_same_key_acts_once_and_a_second_dismissal_is_refused(self) -> None:
        job = self.dead_job()
        first = self.dismiss(job)
        values = {'job': job, 'reason': 'data fixed by hand'}
        again = self.dismissals.write(values, actor=self.operator, idempotency_key='dismiss')
        assert isinstance(again, Preview)
        repeated = self.dismissals.write(values, actor=self.operator, idempotency_key='dismiss', confirm=again.token)
        self.assertEqual(repeated, first)
        self.assertEqual(len([e for e in self.registry.audit(f'internal-job:{job}') if e.action == 'queue_dismiss']), 1)
        other = self.dismissals.write(values, actor=self.operator, idempotency_key='other')
        assert isinstance(other, Refused)
        self.assertEqual(other.rule, 'state')

    def test_dismissing_a_dead_transcribe_job_sweeps_its_transcript_first(self) -> None:
        job = self.queue.submit('transcribe', 'transcript.run', 'run-1', {'transcript_id': 'none', 'actor': self.operator}).job_id
        claim = self.queue.claim('transcribe', 60)
        assert claim is not None
        self.queue.fail(claim.job_id, claim.token, 'boom', retry_in=1)
        self.assertIsInstance(self.dismiss(job), Done)

    def test_the_verbs_are_operator_only(self) -> None:
        job = self.dead_job()
        people = People(self.registry, self.tokens)
        dismiss = next(v for v in VERBS if (v.noun, v.verb) == ('queue', 'dismiss'))
        failed = next(v for v in VERBS if (v.noun, v.verb) == ('queue', 'failed'))
        self.assertEqual((dismiss.role, failed.role, failed.read_only), ('operator', 'operator', True))
        with patch.dict(os.environ, {'T6_PREVIEW_SIGNING_KEY': 'test'}):
            inputs = QueueDismissInput(job=job, reason='fixed', idempotency_key='v')
            self.assertIsInstance(invoke(dismiss, inputs, actor=self.staff, people=people, open_notion=lambda _: None), Refused)
            self.assertIsInstance(invoke(failed, QueueFailedInput(), actor=self.staff, people=people, open_notion=lambda _: None), Refused)
            self.assertEqual(self.job_state(job), 'dead')
            listed = invoke(failed, QueueFailedInput(), actor=self.operator, people=people, open_notion=lambda _: None)
            assert isinstance(listed, Done)
            self.assertEqual([item['job'] for item in listed.result['jobs']], [job])
            preview = invoke(dismiss, inputs, actor=self.operator, people=people, open_notion=lambda _: None)
            assert isinstance(preview, Preview)
            done = invoke(dismiss, inputs.model_copy(update={'confirm': preview.token}), actor=self.operator, people=people, open_notion=lambda _: None)
            self.assertIsInstance(done, Done)
        self.assertEqual(self.job_state(job), 'dismissed')


class EdgeDismissTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'queue.sqlite3'
        self.clock = FakeClock()
        self.queue = JobQueue(self.path, self.clock)

    def failed_job(self, key: str = 'k1') -> int:
        job_id = self.queue.submit('main', 'sample.kind', key, {'n': 1}).job_id
        claim = self.queue.claim('main', 60)
        assert claim is not None
        self.assertTrue(self.queue.fail(job_id, claim.token, 'boom'))
        return job_id

    def test_a_failed_job_is_dismissed_with_a_reason_and_kept_in_a_record(self) -> None:
        job = self.failed_job()
        self.assertEqual([(item.job_id, item.kind, item.reason) for item in self.queue.failed()], [(job, 'sample.kind', 'boom')])
        self.assertEqual(self.queue.dismiss(job, actor='ops', reason='data fixed by hand'), True)
        self.assertEqual(self.queue.failed(), [])
        self.assertEqual(self.queue.dismissals()[0].reason, 'data fixed by hand')
        self.assertEqual((self.queue.dismissals()[0].actor, self.queue.dismissals()[0].job_id), ('ops', job))
        self.assertIsNone(self.queue.claim('main', 60))
        self.assertFalse(self.queue.submit('main', 'sample.kind', 'k1').created)
        result = self.queue.result(job, 'main')
        assert result is not None
        self.assertEqual((result.status, result.value), ('failed', 'boom'))
        with sqlite3_connect(self.path) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM jobs WHERE state = ?', ('failed',)).fetchone()[0], 0)
        self.assertEqual(health({'queue': self.path}, now=0.0, required=('queue',))['queue'], {'pending_jobs': 0, 'failed_jobs': 0})

    def test_only_a_failed_job_with_a_reason_can_be_dismissed(self) -> None:
        job = self.failed_job('k2')
        waiting = self.queue.submit('main', 'sample.kind', 'waiting').job_id
        self.assertFalse(self.queue.dismiss(waiting, actor='ops', reason='x'))
        self.assertFalse(self.queue.dismiss(999, actor='ops', reason='x'))
        with self.assertRaises(ValueError):
            self.queue.dismiss(job, actor='ops', reason=' ')
        with self.assertRaises(ValueError):
            self.queue.dismiss(job, actor=' ', reason='x')
        self.assertTrue(self.queue.dismiss(job, actor='ops', reason='x'))
        self.assertFalse(self.queue.dismiss(job, actor='ops', reason='x'))
        self.assertEqual(len(self.queue.dismissals()), 1)

    def test_the_admin_command_lists_and_dismisses(self) -> None:
        from t6.cli import main
        job = self.failed_job()

        def run(*argv: str) -> tuple[int, str]:
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = main(['admin', 'queue', *argv])
            return code, out.getvalue()

        code, out = run('failed', '--queue-db', str(self.path))
        self.assertEqual((code, [item['job'] for item in json.loads(out)['jobs']]), (0, [job]))
        with self.assertRaises(SystemExit):
            run('dismiss', str(job), '--queue-db', str(self.path), '--by', 'ops')  # a reason is required
        code, out = run('dismiss', str(job), '--queue-db', str(self.path), '--by', 'ops', '--reason', 'fixed')
        self.assertEqual((code, json.loads(out)['state']), (0, 'dismissed'))
        self.assertEqual(run('dismiss', str(job), '--queue-db', str(self.path), '--by', 'ops', '--reason', 'fixed')[0], 1)
        self.assertEqual(self.queue.failed(), [])


def sqlite3_connect(path: Path) -> contextlib.closing:  # type: ignore[type-arg]
    import sqlite3
    return contextlib.closing(sqlite3.connect(path))
