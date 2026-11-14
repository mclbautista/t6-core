import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from t6.cli import main
from t6.preview import PreviewTokens, finish_preview_job, submit_preview_job
from t6.project_writes import ProjectWrites
from t6.registry import Registered, SqliteProjectRegistry
from t6.verbs import Done, Outcome, Preview, Refused
from t6.worker.internal_queue import InternalJobQueue


class ProjectWritesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'db'
        self.now = 1000.0
        self.registry = SqliteProjectRegistry(self.path, clock=lambda: self.now)
        with self.registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES ('test-actor', 'staff', '[]', '[]', NULL, 0, 1)")
            db.execute("INSERT INTO person_permissions VALUES ('test-actor', 'operator')")
        registered = self.registry.register(actor='setup', idempotency_key='setup', code='SHOW')
        assert isinstance(registered, Registered)
        self.project = registered.project.id
        self.tokens = PreviewTokens('secret', clock=lambda: self.now)
        self.writes = ProjectWrites(self.registry, self.tokens)

    def call(self, verb: str, key: str, token: str | None = None, reason: str | None = 'weather', actor: str = 'alice') -> Outcome:
        return self.writes.run(verb, self.project, reason if verb == 'hold' else None,
                               actor=actor, idempotency_key=key, confirm=token)

    def test_hold_resume_atomic_and_idempotent(self) -> None:
        before_audit = len(self.registry.audit(self.project))
        before_activity = len(self.registry.activity(self.project))
        before_intents = len(self.registry.pending_sync())
        preview = self.call('hold', 'one')
        assert isinstance(preview, Preview)
        result = self.call('hold', 'one', preview.token)
        self.assertIsInstance(result, Done)
        reused = self.call('hold', 'one', preview.token)
        assert isinstance(reused, Refused)
        self.assertEqual(reused.rule, 'token_used')
        fresh = self.call('hold', 'one')
        assert isinstance(fresh, Preview)
        replay = self.call('hold', 'one', fresh.token)
        self.assertEqual(replay, result)
        held = self.call('hold', 'different')
        assert isinstance(held, Refused)
        self.assertEqual(held.rule, 'project_held')
        self.assertEqual(len(self.registry.audit(self.project)), before_audit + 1)
        self.assertEqual(len(self.registry.activity(self.project)), before_activity + 1)
        self.assertEqual(len(self.registry.pending_sync()), before_intents + 1)
        resume = self.call('resume', 'two')
        assert isinstance(resume, Preview)
        self.assertIsInstance(self.call('resume', 'two', resume.token), Done)
        self.assertEqual(len(self.registry.audit(self.project)), before_audit + 2)
        self.assertEqual(len(self.registry.activity(self.project)), before_activity + 2)
        self.assertEqual(len(self.registry.pending_sync()), before_intents + 2)
        status_intents = [intent for intent in self.registry.pending_sync() if intent.status_held is not None]
        self.assertEqual([intent.status_held for intent in status_intents], [True, False])
        self.assertTrue(all(intent.status_held is None for intent in self.registry.undispatched_sync()))
        self.registry.report_sync(self.project, 99, ok=True)
        self.assertEqual(len(self.registry.pending_sync()), 2)

    def test_expired_and_changed_input_or_actor_are_refused(self) -> None:
        preview = self.call('hold', 'key')
        assert isinstance(preview, Preview)
        for outcome in (self.call('hold', 'key', preview.token, reason='changed'),
                        self.call('hold', 'key', preview.token, actor='bob')):
            assert isinstance(outcome, Refused)
            self.assertEqual(outcome.rule, 'token_mismatch')
        self.now += 900
        expired = self.call('hold', 'key', preview.token)
        assert isinstance(expired, Refused)
        self.assertEqual(expired.rule, 'token_expired')

    def test_cli_preview_and_confirm(self) -> None:
        with patch.dict(os.environ, {'T6_PERSON_ID': 'test-actor', 'T6_REGISTRY_DB': str(self.path), 'T6_PREVIEW_SIGNING_KEY': 'secret'}):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(main(['project', 'hold', self.project, '--reason', 'pause']), 0)
            preview = json.loads(out.getvalue())
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(main(['project', 'hold', self.project, '--reason', 'pause', '--confirm', preview['token'],
                                       '--idempotency-key', preview['plan']['idempotency_key']]), 0)
            self.assertEqual(json.loads(out.getvalue())['outcome'], 'done')

    def test_preview_by_fake_job(self) -> None:
        queue = InternalJobQueue(self.path, clock=lambda: self.now)
        job_id = submit_preview_job(queue, queue_name='main', kind='fake.preview', dedupe_key='x',
                                    verb='fake write', actor='alice', inputs={'project': self.project})
        job = queue.claim('main')
        assert job is not None
        self.assertTrue(finish_preview_job(queue, job, self.tokens, 'fake plan'))
        result = queue.result(job_id, 'main')
        assert result is not None
        self.assertEqual(result.status, 'ok')
        preview = Preview.model_validate(result.value)
        self.assertEqual(preview.plan, 'fake plan')
        self.assertIsInstance(self.tokens.verify(preview.token, 'fake write', 'alice', {'project': self.project}), str)
