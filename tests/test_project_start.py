"""`project start`: a previewed plan by job, and the t6 steps it applies once (#267)."""
import io
import json
import os
import unittest
from contextlib import redirect_stdout
from typing import Any
from unittest.mock import patch

from cli_api_fake import client_patch
from t6.bay import BayStore
from t6.ids import EntityKind, T6Id
from t6.notion.generated import BaysRow, ProjectsRow
from t6.notion.testing import FakeNotion, project_properties, title
from t6.person import People
from t6.preview import PreviewTokens
from t6.project_start import ProjectStartStore
from t6.registry import Registered
from t6.verbs import VERBS, Done, Preview, Refused, catalogue, invoke
from t6.worker import Worker
from t6.worker.internal_queue import InternalJobQueue, InternalQueueClient
from test_person import MemoryRegistry

EPISODES = [{'code': 'Ep01', 'episode': 'Episode 1', 'due': '2026-12-01', 'air': None},
            {'code': 'Ep02', 'episode': 'Episode 2', 'due': '2026-12-08', 'air': None}]
MEDIA = [{'kind': 'nas-path', 'ref': '/nas/Show', 'role': 'master'},
         {'kind': 'drive', 'ref': 't6d_0123456789abcdef0123456789abcdef', 'role': 'backup'}]


class ProjectStartTests(unittest.TestCase):
    def setUp(self) -> None:
        env = patch.dict(os.environ, {'T6_PREVIEW_SIGNING_KEY': 'test'})
        env.start()
        self.addCleanup(env.stop)
        self.registry = MemoryRegistry()
        self.addCleanup(self.registry.db.close)
        self.people = People(self.registry, PreviewTokens('test'))
        with self.registry._transaction() as db:
            for person in ('starter', 'lead', 'reader', 'editor'):
                db.execute("INSERT INTO persons VALUES (?, 'staff', '[]', '[]', NULL, 0, 1)", (person,))
            db.execute("INSERT INTO person_permissions VALUES ('starter', 'operator')")
            db.execute("INSERT INTO person_permissions VALUES ('starter', 'bookings')")
            db.execute("INSERT INTO person_permissions VALUES ('lead', 'operator')")
        self.fake = FakeNotion()
        self.fake.add_snapshot_data_source('projects', 'projects')
        self.fake.add_snapshot_data_source('bays', 'bays')
        self.client = self.fake.client()
        self.addCleanup(self.client.close)
        self.registry._new_id = lambda: str(T6Id.mint(EntityKind.PROJECT))
        self.page = str(self.fake.add_page('projects', project_properties(name='Show', project_type='Feature Film'))['id'])
        project = self.registry.register(actor='setup', idempotency_key='project', notion_page_id=self.page)
        assert isinstance(project, Registered)
        self.project = project.project.id
        bays = BayStore(self.registry)
        rows = [BaysRow.from_page(self.fake.add_page('bays', {BaysRow.notion_name('name'): title('Bay')})) for _ in range(2)]
        self.bays = [e['t6_id'] for e in bays.migrate(rows, actor='setup', apply=True, digest=bays.migrate(rows, actor='setup')['digest'])['entries']]
        self.store = ProjectStartStore(self.registry, self.people.tokens, lambda page: ProjectsRow.from_page(self.client.retrieve_page(page)).project_type)
        self.queue = InternalJobQueue(self.registry, clock=self.registry._clock)
        self.values: dict[str, Any] = {'project': self.project, 'deliverables': EPISODES, 'media': MEDIA, 'bookings': [
            {'bay': self.bays[0], 'person': 'editor', 'start': '2026-11-20T10:00:00+08:00', 'end': '2026-11-20T18:00:00+08:00'}]}

    def call(self, values: dict[str, Any], actor: str = 'starter') -> Any:
        entry = next(e for e in VERBS if (e.noun, e.verb) == ('project', 'start'))
        return invoke(entry, entry.input_model.model_validate(values), actor=actor, people=self.people, open_notion=lambda _: None)

    def work(self) -> None:
        worker = Worker(InternalQueueClient(self.queue), 'main', {'project_start.preview': lambda job: self.store.run_preview(job.payload)})
        while worker.run_once():
            pass

    def preview(self, values: dict[str, Any] | None = None, actor: str = 'starter', key: str = 'start') -> Any:
        started = self.call({**(values or self.values), 'idempotency_key': key}, actor)
        assert isinstance(started, Done), started
        self.work()
        return self.call({'project': self.project, 'job': started.result['job_id']}, actor)

    def count(self, table: str) -> int:
        with self.registry._transaction() as db:
            return int(db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0])

    def test_start_returns_a_job_id_and_the_job_result_is_the_preview(self) -> None:
        started = self.call({**self.values, 'idempotency_key': 'start'})
        assert isinstance(started, Done)
        job = started.result['job_id']
        self.assertEqual(self.call({'project': self.project, 'job': job}).result['state'], 'pending')
        self.assertEqual((self.count('approvals'), self.count('deliverables'), self.count('media_locations'), self.count('bookings')), (0, 0, 0, 0))
        self.work()
        preview = self.call({'project': self.project, 'job': job})
        assert isinstance(preview, Preview)
        self.assertTrue(preview.token)
        self.assertEqual(preview.plan['idempotency_key'], 'start')

    def test_the_plan_is_in_order_and_lists_the_manual_steps_it_does_not_do(self) -> None:
        preview = self.preview()
        steps = preview.plan['steps']
        self.assertEqual([s['step'] for s in steps], ['approval', 'nas folders', 'frameio', 'resolve project', 'resolve timelines', 'media locations', 'milestone template', 'bookings'])
        self.assertEqual([s['kind'] for s in steps], ['t6', 'manual', 'later', 'manual', 'manual', 't6', 't6', 't6'])
        self.assertIn('#275', steps[2]['detail'])
        self.assertEqual(preview.plan['manual_steps'], ['NAS folders', 'Resolve project', 'Resolve timelines with their stamps'])
        template = steps[6]
        self.assertEqual(template['template'], 'film')
        self.assertIn('piclock', template['milestones'])
        self.assertEqual([d['code'] for d in template['deliverables']], ['Ep01', 'Ep02'])
        self.assertEqual(len(steps[5]['locations']), 2)
        self.assertEqual(steps[7]['bookings'][0]['bay_id'], self.bays[0])
        self.assertEqual(self.count('deliverables'), 0)

    def test_confirm_creates_each_t6_step_once_and_a_retry_changes_nothing(self) -> None:
        preview = self.preview()
        confirm = {**self.values, 'idempotency_key': 'start', 'confirm': preview.token}
        done = self.call(confirm)
        assert isinstance(done, Done), done
        self.assertEqual((self.count('approvals'), self.count('deliverables'), self.count('media_locations'), self.count('bookings')), (1, 2, 2, 1))
        self.assertEqual(len(done.result['deliverable_ids']), 2)
        self.assertTrue(done.result['approval_id'].startswith('t6a_'))
        with self.registry._transaction() as db:
            self.assertEqual(db.execute('SELECT gate, state, target FROM approvals').fetchone(), ('project start', 'requested', self.project))
            self.assertEqual({r[0] for r in db.execute('SELECT template FROM deliverable_templates')}, {'film'})
        used = self.count('used_preview_tokens')
        self.assertEqual(done, self.call(confirm))
        self.assertEqual((self.count('approvals'), self.count('deliverables'), self.count('media_locations'), self.count('bookings')), (1, 2, 2, 1))
        self.assertEqual(self.count('used_preview_tokens'), used)

    def test_a_spent_token_cannot_start_another_key(self) -> None:
        preview = self.preview()
        self.call({**self.values, 'idempotency_key': 'start', 'confirm': preview.token})
        refused = self.call({**self.values, 'idempotency_key': 'other', 'confirm': preview.token})
        assert isinstance(refused, Refused)
        self.assertEqual(refused.rule, 'token_mismatch')

    def test_activity_records_the_start_and_each_manual_step(self) -> None:
        done = self.call({**self.values, 'idempotency_key': 'start', 'confirm': self.preview().token})
        assert isinstance(done, Done)
        entries = [(i.action, i.summary) for i in self.registry.activity(self.project) if i.action.startswith('project_start')]
        self.assertIn(('project_start', 'project started'), entries)
        manual = [summary for action, summary in entries if action == 'project_start_manual']
        self.assertEqual(sorted(manual), ['manual step: NAS folders', 'manual step: Resolve project', 'manual step: Resolve timelines with their stamps'])

    def test_confirming_with_a_changed_input_is_refused_and_creates_nothing(self) -> None:
        preview = self.preview()
        changes: list[dict[str, Any]] = [{'deliverables': EPISODES[:1]}, {'media': MEDIA[:1]}, {'bookings': []}]
        for change in changes:
            refused = self.call({**self.values, **change, 'idempotency_key': 'start', 'confirm': preview.token})
            assert isinstance(refused, Refused)
            self.assertEqual(refused.rule, 'token_mismatch')
        other_key = self.call({**self.values, 'idempotency_key': 'other', 'confirm': preview.token})
        assert isinstance(other_key, Refused)
        self.assertEqual((self.count('approvals'), self.count('deliverables'), self.count('media_locations'), self.count('bookings')), (0, 0, 0, 0))

    def test_confirm_refuses_when_the_project_changed_since_the_preview(self) -> None:
        preview = self.preview()
        with self.registry._transaction() as db:
            db.execute('INSERT INTO project_archives VALUES (?)', (self.project,))
        refused = self.call({**self.values, 'idempotency_key': 'start', 'confirm': preview.token})
        assert isinstance(refused, Refused)
        self.assertEqual(refused.rule, 'project_archived')
        self.assertEqual(self.count('approvals'), 0)

    def test_a_started_project_is_refused_a_second_start(self) -> None:
        self.call({**self.values, 'idempotency_key': 'start', 'confirm': self.preview().token})
        second = self.preview(key='again')
        assert isinstance(second, Refused)
        self.assertEqual(second.rule, 'project_started')
        self.assertEqual(self.count('deliverables'), 2)

    def test_same_key_same_input_is_one_job_and_same_key_other_input_is_refused(self) -> None:
        first = self.call({**self.values, 'idempotency_key': 'k'})
        second = self.call({**self.values, 'idempotency_key': 'k'})
        self.assertEqual(first.result['job_id'], second.result['job_id'])
        self.assertEqual(self.count('internal_jobs'), 1)
        clash = self.call({**self.values, 'deliverables': [], 'idempotency_key': 'k'})
        assert isinstance(clash, Refused)
        self.assertEqual(clash.rule, 'idempotency_key')
        self.assertNotEqual(first.result['job_id'], self.call({**self.values, 'idempotency_key': 'k2'}).result['job_id'])

    def test_a_job_can_be_read_only_by_the_person_who_started_it(self) -> None:
        started = self.call({**self.values, 'idempotency_key': 'start'})
        self.work()
        stranger = self.call({'project': self.project, 'job': started.result['job_id']}, actor='lead')
        assert isinstance(stranger, Refused)
        self.assertEqual(stranger.rule, 'job')
        missing = self.call({'project': self.project, 'job': 999})
        assert isinstance(missing, Refused)

    def test_roles_and_the_bookings_permission(self) -> None:
        refused = self.call({**self.values, 'idempotency_key': 'a'}, actor='reader')
        assert isinstance(refused, Refused)
        no_bookings = self.call({**self.values, 'idempotency_key': 'b'}, actor='lead')
        assert isinstance(no_bookings, Refused)
        self.assertIn('bookings', no_bookings.reason)
        self.assertEqual(self.count('internal_jobs'), 0)
        without = self.preview({**self.values, 'bookings': []}, actor='lead', key='c')
        assert isinstance(without, Preview)

    def test_plan_refusals_come_back_as_the_job_result(self) -> None:
        clash = {'bay': self.bays[0], 'person': 'editor', 'start': '2026-11-20T12:00:00+08:00', 'end': '2026-11-20T14:00:00+08:00'}
        overlap = self.preview({**self.values, 'bookings': self.values['bookings'] + [clash]}, key='o')
        assert isinstance(overlap, Refused)
        self.assertEqual(overlap.rule, 'overlap')
        frameio = self.preview({**self.values, 'media': [{'kind': 'frameio-project', 'ref': 'abc', 'role': 'review'}]}, key='f')
        assert isinstance(frameio, Refused)
        self.assertEqual(frameio.rule, 'frameio')
        duplicate = self.preview({**self.values, 'deliverables': EPISODES[:1] * 2}, key='d')
        assert isinstance(duplicate, Refused)
        self.assertEqual(duplicate.rule, 'deliverable_code')
        self.assertEqual((self.count('approvals'), self.count('deliverables')), (0, 0))

    def test_a_project_type_without_a_template_is_refused(self) -> None:
        page = str(self.fake.add_page('projects', project_properties(name='Other', project_type='Music Video'))['id'])
        other = self.registry.register(actor='setup', idempotency_key='other', notion_page_id=page)
        assert isinstance(other, Registered)
        refused = self.preview({**self.values, 'project': other.project.id}, key='t')
        assert isinstance(refused, Refused)
        self.assertEqual(refused.rule, 'template')

    def test_a_dead_job_is_refused_with_its_reason(self) -> None:
        started = self.call({**self.values, 'idempotency_key': 'start'})
        claim = self.queue.claim('main', 60)
        assert claim is not None
        self.assertTrue(self.queue.fail(claim.job_id, claim.token, 'boom'))
        failed = self.call({'project': self.project, 'job': started.result['job_id']})
        assert isinstance(failed, Refused)
        self.assertEqual(failed.rule, 'job_failed')

    def test_the_verb_is_in_the_catalogue_and_runs_from_the_cli(self) -> None:
        entry = {(v['noun'], v['verb']): v for v in catalogue()}[('project', 'start')]
        self.assertEqual(entry['role'], 'operator')
        self.assertFalse(entry['read_only'])
        self.assertTrue({'project', 'deliverables', 'media', 'bookings', 'job', 'idempotency_key', 'confirm'} <= set(entry['input_schema']['properties']))
        from t6.cli import main
        out = io.StringIO()
        with patch.dict(os.environ, {'T6_PERSON_ID': 'starter'}), patch('t6.cli._open_registry', return_value=self.registry), client_patch(lambda _: None), redirect_stdout(out):
            status = main(['project', 'start', self.project, '--deliverable', json.dumps(EPISODES[0]), '--media', json.dumps(MEDIA[0]), '--idempotency-key', 'cli'])
        self.assertEqual(status, 0)
        self.assertIsInstance(json.loads(out.getvalue())['result']['job_id'], int)


class ProjectStartWorkerTests(unittest.TestCase):
    """The main worker with `T6_DATABASE` and the signing key plans a Start; without the key it says so."""

    def setUp(self) -> None:
        import tempfile
        from pathlib import Path
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 't6.sqlite3'
        from t6.registry.sqlite import SqliteProjectRegistry
        self.registry = SqliteProjectRegistry(self.path)
        with self.registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES ('starter', 'staff', '[]', '[]', NULL, 0, 1)")
        self.fake = FakeNotion()
        self.fake.add_snapshot_data_source('projects', 'projects')
        self.page = str(self.fake.add_page('projects', project_properties(name='Show', project_type='TVC'))['id'])
        self.registry._new_id = lambda: str(T6Id.mint(EntityKind.PROJECT))
        project = self.registry.register(actor='setup', idempotency_key='project', notion_page_id=self.page)
        assert isinstance(project, Registered)
        self.project = project.project.id
        self.environ = {'T6_SERVER_URL': 'http://edge', 'T6_API_URL': 'http://api', 'T6_WORKER_SECRET': 'secret', 'T6_WORKER_QUEUE': 'main',
                        'T6_DATABASE': str(self.path), 'T6_NOTION_TOKEN': 'fake', 'T6_PREVIEW_SIGNING_KEY': 'test'}

    def handlers(self, environ: dict[str, str]) -> Any:
        from t6.worker.__main__ import WorkerSettings, configured_handlers
        with patch('t6.worker.__main__.HttpTransport'):
            return configured_handlers(environ, WorkerSettings.from_env(environ), None).get('main', {})  # type: ignore[arg-type]

    def test_the_worker_plans_a_start_with_the_project_type_read_from_notion(self) -> None:
        tokens = PreviewTokens('test')
        store = ProjectStartStore(self.registry, tokens)
        started = store.start({'project': self.project, 'deliverables': [{'code': 'Spot', 'episode': None, 'due': None, 'air': None}],
                               'media': [], 'bookings': []}, actor='starter', idempotency_key='k')
        assert isinstance(started, Done)
        handlers = self.handlers(self.environ)
        with patch('t6.notion.NotionClient', return_value=self.fake.client()):  # the handler closes it
            worker = Worker(InternalQueueClient(InternalJobQueue(self.path)), 'main', handlers)
            self.assertTrue(worker.run_once())
        preview = store.job_result(started.result['job_id'], actor='starter')
        assert isinstance(preview, Preview)
        template = next(step for step in preview.plan['steps'] if step['step'] == 'milestone template')
        self.assertEqual(template['template'], 'tvc')
        confirmed = store.confirm({'project': self.project, 'deliverables': [{'code': 'Spot', 'episode': None, 'due': None, 'air': None}],
                                   'media': [], 'bookings': []}, actor='starter', idempotency_key='k', token=preview.token)
        assert isinstance(confirmed, Done)
        self.assertEqual(len(confirmed.result['deliverable_ids']), 1)

    def test_without_the_signing_key_plans_are_off_and_the_log_says_so(self) -> None:
        errors = io.StringIO()
        from contextlib import redirect_stderr
        environ = {name: value for name, value in self.environ.items() if name != 'T6_PREVIEW_SIGNING_KEY'}
        with redirect_stderr(errors):
            handlers = self.handlers(environ)
        self.assertNotIn('project_start.preview', handlers)
        self.assertIn('T6_PREVIEW_SIGNING_KEY', errors.getvalue())
        self.assertIn('project_start.preview', self.handlers(self.environ))


if __name__ == '__main__':
    unittest.main()
