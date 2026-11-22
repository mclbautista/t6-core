"""The Resolve worker runs `resolve.stamp` jobs through the t6 API, against a fake Resolve and the in-memory t6 database (#581)."""
import contextlib
import io
import json
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from t6.person import People
from t6.preview import PreviewTokens
from t6.queue_dismiss import JobDismissals
from t6.resolve.stamp_jobs import ResolveStampJobs
from t6.resolve.timelines import ScriptingTimelines
from t6.resolve.worker import ResolveQueue, stamp_handler
from t6.server.app import Request, Response
from t6.server.queue import JobQueue
from t6.server.verbs import VerbAPI, request_line
from t6.worker.__main__ import main
from t6.worker.runner import Worker
from test_person import MemoryRegistry
from test_resolve_scripting import REF, STAMP, FakeManager, FakeProject, FakeResolve, FakeTimeline

NEW = 't6t_' + 'c' * 32


class Flow:
    """The API, the resolve queue and an in-memory registry, wired in process; the worker drives a fake Resolve."""

    def __init__(self, test: unittest.TestCase) -> None:
        self.registry = MemoryRegistry()
        test.addCleanup(self.registry.db.close)
        directory = tempfile.TemporaryDirectory()
        test.addCleanup(directory.cleanup)
        self.api = VerbAPI(People(self.registry, PreviewTokens('signing')), JobQueue(Path(directory.name) / 'queue'),
                           {'resolve-secret': 'resolve', 'transcribe-secret': 'transcribe'}, lambda _: None)
        self.jobs = ResolveStampJobs(self.registry)
        self.ep1, self.ep2 = FakeTimeline('u1', 'Ep1', STAMP), FakeTimeline('u2', 'Ep2')
        self.manager = FakeManager({REF: FakeProject([self.ep1, self.ep2])})
        self.resolve: FakeResolve | None = FakeResolve(self.manager)
        flow = self

        class InProcess:
            def send(self, method: str, path: str, headers: Any, body: bytes) -> tuple[int, dict[str, Any]]:
                response = flow.api(Request(method, path, headers, body))
                return response.status, dict(response.body) if response.body else {}
        self.worker = Worker(ResolveQueue(InProcess(), 'resolve-secret'), 'resolve', {'resolve.stamp': stamp_handler(ScriptingTimelines(lambda: flow.resolve))})

    def enqueue(self, timeline: str = 'Ep2', stamp: str = NEW, replaces: str | None = None) -> int:
        with self.registry._transaction() as db:
            return self.jobs.enqueue(db, project='t6p_synthetic', resolve_project=REF, timeline=timeline, stamp=stamp, replaces=replaces)

    def post(self, path: str, data: dict[str, Any], secret: str = 'resolve-secret') -> tuple[int, Any]:
        response = self.api(Request('POST', path, {'Authorization': 'Bearer ' + secret}, json.dumps(data).encode()))
        return response.status, response.body

    def job(self, job_id: int) -> Any:
        return self.jobs.queue.get(job_id)


class StampJobTests(unittest.TestCase):
    def setUp(self) -> None:
        self.flow = Flow(self)

    def test_worker_claims_stamps_and_finishes_the_job(self) -> None:
        job_id = self.flow.enqueue()
        self.assertEqual(self.flow.job(job_id).payload, {'project': 't6p_synthetic', 'resolve_project': REF, 'timeline': 'Ep2', 'stamp': NEW})
        self.assertTrue(self.flow.worker.run_once())
        self.assertEqual(self.flow.ep2.markers[0]['customData'], NEW)
        job = self.flow.job(job_id)
        self.assertEqual(job.state, 'done')
        self.assertEqual(job.result, {'status': 'ok', 'value': {'outcome': 'done', 'result': {'job_id': job_id, 'timeline': {'uuid': 'u2', 'name': 'Ep2', 'stamp': NEW}}}})
        self.assertFalse(self.flow.worker.run_once())

    def test_a_restamp_job_replaces_the_duplicated_stamp_on_the_copy(self) -> None:
        copy = FakeTimeline('u3', 'Ep1 copy', STAMP)
        self.flow.manager.projects[REF].timelines.append(copy)
        job_id = self.flow.enqueue('Ep1 copy', NEW, replaces=STAMP)
        self.assertEqual(self.flow.job(job_id).payload['replaces'], STAMP)
        self.assertTrue(self.flow.worker.run_once())
        self.assertEqual((copy.markers[0]['customData'], self.flow.ep1.markers[0]['customData']), (NEW, STAMP))
        self.assertEqual(self.flow.job(job_id).state, 'done')

    def test_resolve_not_running_fails_the_job_for_good_and_queue_failed_shows_it(self) -> None:
        self.flow.resolve = None
        job_id = self.flow.enqueue()
        self.assertTrue(self.flow.worker.run_once())
        self.assertFalse(self.flow.worker.run_once())  # never re-run
        dismissals = JobDismissals(self.flow.registry, PreviewTokens('signing'))
        self.assertEqual(dismissals.failed(), [{'job': job_id, 'queue': 'resolve', 'kind': 'resolve.stamp', 'dedupe_key': self.flow.job(job_id).dedupe_key, 'attempts': 1,
                                                'failure': {'outcome': 'refused', 'rule': 'resolve', 'reason': 'Resolve is not running'}}])
        preview = dismissals.write({'job': job_id, 'reason': 'Resolve was closed'}, actor='operator', idempotency_key='k')
        confirmed = dismissals.write({'job': job_id, 'reason': 'Resolve was closed'}, actor='operator', idempotency_key='k', confirm=preview.model_dump()['token'])
        self.assertEqual(confirmed.model_dump()['outcome'], 'done')
        self.assertEqual(self.flow.job(job_id).state, 'dismissed')

    def test_locked_project_fails_the_job_with_its_reason(self) -> None:
        self.flow.manager.locked = True
        job_id = self.flow.enqueue()
        self.flow.worker.run_once()
        job = self.flow.job(job_id)
        self.assertEqual(job.state, 'dead')
        self.assertEqual(job.result['value']['reason'], 'The Resolve project cannot be loaded (locked or open elsewhere)')

    def test_a_handler_crash_also_ends_the_job_without_retry(self) -> None:
        def broken() -> Any:
            raise RuntimeError('scripting bridge gone')
        self.flow.resolve = None
        self.flow.worker = Worker(self.flow.worker._clients[0], 'resolve', {'resolve.stamp': stamp_handler(ScriptingTimelines(broken))})
        job_id = self.flow.enqueue()
        self.flow.worker.run_once()
        self.assertEqual(self.flow.job(job_id).state, 'dead')
        self.assertEqual(self.flow.job(job_id).result['value']['reason'], 'RuntimeError: scripting bridge gone')

    def test_only_the_resolve_identity_may_claim_or_finish(self) -> None:
        job_id = self.flow.enqueue()
        self.assertEqual(self.flow.post('/v1/resolve/claim', {}, 'transcribe-secret')[0], 403)
        self.assertEqual(self.flow.post('/v1/transcribe/claim', {}, 'transcribe-secret'), (200, {'job': None}))  # the transcribe queue never holds it
        status, body = self.flow.post('/v1/resolve/claim', {})
        self.assertEqual((status, body['job']['job_id'], body['job']['kind']), (200, job_id, 'resolve.stamp'))
        token = body['job']['token']
        self.assertEqual(self.flow.post('/v1/resolve/fail', {'job_id': job_id, 'token': token, 'reason': 'x'}, 'transcribe-secret')[0], 403)
        self.assertEqual(self.flow.post('/v1/resolve/fail', {'job_id': job_id, 'token': 'stale', 'reason': 'x'})[0], 409)
        self.assertEqual(self.flow.post('/v1/resolve/extend', {'job_id': job_id, 'token': token}), (200, {'accepted': True}))
        self.assertEqual(self.flow.job(job_id).state, 'claimed')
        self.assertIn(' verb=resolve ', request_line('/v1/resolve/claim', 'resolve', Response(200, {}), 0.0, {}))

    def test_a_report_must_carry_the_job_stamp(self) -> None:
        job_id = self.flow.enqueue()
        token = self.flow.post('/v1/resolve/claim', {})[1]['job']['token']
        self.assertEqual(self.flow.post('/v1/resolve/report', {'job_id': job_id, 'token': token, 'data': {'uuid': 'u2', 'name': 'Ep2', 'stamp': STAMP}})[0], 400)
        self.assertEqual(self.flow.post('/v1/resolve/fail', {'job_id': job_id, 'token': token, 'reason': ' '})[0], 400)
        self.assertEqual(self.flow.post('/v1/resolve/report', {'job_id': job_id + 1, 'token': token, 'data': {}})[0], 404)
        self.assertEqual(self.flow.job(job_id).state, 'claimed')


class ResolveRoleTests(unittest.TestCase):
    settings = {'T6_SERVER_URL': 'http://example.invalid', 'T6_WORKER_SECRET': 'secret', 'T6_WORKER_QUEUE': 'resolve', 'T6_DATABASE': '/unavailable'}

    class Transport:
        def send(self, *args: object) -> object:
            raise AssertionError('no requests expected')

    def test_resolve_role_never_opens_database(self) -> None:
        stop = threading.Event()
        stop.set()
        with patch.dict(sys.modules, {'DaVinciResolveScript': types.ModuleType('DaVinciResolveScript')}), \
                patch('t6.worker.internal_queue.InternalJobQueue', side_effect=AssertionError('database opened')), \
                patch('sqlite3.connect', side_effect=AssertionError('database opened')):
            self.assertEqual(main(self.settings, transport=self.Transport(), stop=stop), 0)  # type: ignore[arg-type]

    def test_exits_2_without_the_resolve_scripting_module(self) -> None:
        stop = threading.Event()
        stop.set()
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            self.assertEqual(main(self.settings, transport=self.Transport(), stop=stop), 2)  # type: ignore[arg-type]
        self.assertIn('DaVinciResolveScript', errors.getvalue())

    def broken_module(self) -> None:
        """A `DaVinciResolveScript` that is found but cannot load `fusionscript`, as Resolve's own module fails."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        (Path(directory.name) / 'DaVinciResolveScript.py').write_text('raise ImportError("Could not locate module dependencies")\n')
        patcher = patch.object(sys, 'path', [directory.name, *sys.path])
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(sys.modules.pop, 'DaVinciResolveScript', None)

    def test_exits_2_when_the_scripting_module_is_found_but_cannot_load_fusionscript(self) -> None:
        self.broken_module()
        stop = threading.Event()
        stop.set()
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            self.assertEqual(main(self.settings, transport=self.Transport(), stop=stop), 2)  # type: ignore[arg-type]
        self.assertIn('Could not locate module dependencies', errors.getvalue())

    def test_a_broken_scripting_module_is_not_reported_as_resolve_not_running(self) -> None:
        self.broken_module()
        with self.assertRaisesRegex(ImportError, 'Could not locate module dependencies'):
            ScriptingTimelines().read(REF)


if __name__ == '__main__':
    unittest.main()
