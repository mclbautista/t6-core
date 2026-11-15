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
from t6.frameio import CreateProjectFolders, Failed
from t6.frameio.api import FrameioApi
from t6.frameio.project_folders import ProjectFolders
from t6.frameio.reads import FrameioReads
from t6.frameio.testing import FakeFrameio, StaticTokens
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


class FakeFrameioWriter:
    """What the API asks through the `frameio` queue, answered by the real Frame.io reads on the fake Frame.io API."""

    def __init__(self, frameio: FakeFrameio) -> None:
        self.reads = FrameioReads(FrameioApi(StaticTokens(), frameio.fetch, lambda _: None, send=frameio.send))
        self.calls: list[tuple[str, str, tuple[str, ...]]] = []
        self.calls_known: list[str | None] = []
        self.calls_taken: list[tuple[str, ...]] = []
        self.failure: Failed | None = None

    def create_project_folders(self, project_id: str, name: str, folders: tuple[str, ...], *,
                               known_project_id: str | None = None, taken: tuple[str, ...] = ()) -> ProjectFolders | Failed:
        self.calls.append((project_id, name, folders))
        self.calls_known.append(known_project_id)
        self.calls_taken.append(taken)
        if self.failure is not None:
            return self.failure
        found = self.reads(CreateProjectFolders(project_id=project_id, name=name, folders=folders, known_project_id=known_project_id, taken=taken))
        assert isinstance(found, dict)
        return ProjectFolders.from_json(found)


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
        self.store = ProjectStartStore(self.registry, self.people.tokens, lambda page: ProjectsRow.from_page(self.client.retrieve_page(page)).project_type,
                                       lambda page: ProjectsRow.from_page(self.client.retrieve_page(page)).name)
        self.frameio = FakeFrameio()
        self.writer = FakeFrameioWriter(self.frameio)
        self.queue = InternalJobQueue(self.registry, clock=self.registry._clock)
        self.values: dict[str, Any] = {'project': self.project, 'deliverables': EPISODES, 'media': MEDIA, 'bookings': [
            {'bay': self.bays[0], 'person': 'editor', 'start': '2026-11-20T10:00:00+08:00', 'end': '2026-11-20T18:00:00+08:00'}]}

    def call(self, values: dict[str, Any], actor: str = 'starter', frameio: Any = 'default') -> Any:
        entry = next(e for e in VERBS if (e.noun, e.verb) == ('project', 'start'))
        return invoke(entry, entry.input_model.model_validate(values), actor=actor, people=self.people, open_notion=lambda _: None,
                      frameio=self.writer if frameio == 'default' else frameio)

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
        self.assertEqual([s['kind'] for s in steps], ['t6', 'manual', 'frameio', 'manual', 'manual', 't6', 't6', 't6'])
        self.assertEqual(steps[2]['label'], 'Frame.io project and per-Deliverable folder')
        self.assertEqual((steps[2]['project_name'], steps[2]['folders']), ('Show', ['Ep01', 'Ep02']))
        self.assertEqual(self.writer.calls, [])  # the preview asks Frame.io for nothing
        self.assertEqual(self.frameio.requests + [str(p) for p, _ in self.frameio.posts], [])
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
        self.assertEqual((self.count('approvals'), self.count('deliverables'), self.count('media_locations'), self.count('bookings')), (1, 2, 5, 1))
        self.assertEqual(len(done.result['deliverable_ids']), 2)
        self.assertTrue(done.result['approval_id'].startswith('t6a_'))
        with self.registry._transaction() as db:
            self.assertEqual(db.execute('SELECT gate, state, target FROM approvals').fetchone(), ('project start', 'requested', self.project))
            self.assertEqual({r[0] for r in db.execute('SELECT template FROM deliverable_templates')}, {'film'})
        used = self.count('used_preview_tokens')
        self.assertEqual(done, self.call(confirm))
        self.assertEqual((self.count('approvals'), self.count('deliverables'), self.count('media_locations'), self.count('bookings')), (1, 2, 5, 1))
        self.assertEqual(self.count('used_preview_tokens'), used)

    def locations(self, kind: str) -> list[tuple[str, str, str]]:
        with self.registry._transaction() as db:
            return [tuple(r) for r in db.execute('SELECT owner_id, ref, role FROM media_locations WHERE kind=? AND active=1 ORDER BY ref', (kind,))]

    def confirm(self, key: str = 'start') -> Any:
        return self.call({**self.values, 'idempotency_key': key, 'confirm': self.preview(key=key).token})

    def test_confirm_makes_one_frameio_project_and_one_folder_per_deliverable_and_records_them(self) -> None:
        done = self.confirm()
        assert isinstance(done, Done), done
        self.assertEqual(len(self.frameio.projects), 1)
        project = next(iter(self.frameio.projects.values()))
        self.assertEqual(project['name'], 'Show')
        self.assertEqual(self.locations('frameio-project'), [(self.project, project['id'], 'frameio-project')])
        folders = self.frameio.folders[project['root_folder_id']]
        self.assertEqual(sorted(f['name'] for f in folders), ['Ep01', 'Ep02'])
        by_code = {f['name']: f['id'] for f in folders}
        with self.registry._transaction() as db:
            owners = dict(db.execute("SELECT code, id FROM deliverables WHERE project_id=?", (self.project,)).fetchall())
        self.assertEqual(self.locations('frameio-folder'), sorted(((owners[code], by_code[code], 'review') for code in ('Ep01', 'Ep02')), key=lambda row: row[1]))
        self.assertEqual(len(done.result['frameio_media_ids']), 3)
        self.assertEqual(done.result['frameio_project_id'], project['id'])
        self.assertEqual(self.writer.calls, [(self.project, 'Show', ('Ep01', 'Ep02'))])

    def test_a_repeated_confirm_creates_nothing_new_in_frameio_or_t6(self) -> None:
        preview = self.preview()
        confirm = {**self.values, 'idempotency_key': 'start', 'confirm': preview.token}
        first = self.call(confirm)
        posts, media = len(self.frameio.posts), self.count('media_locations')
        self.assertEqual(self.call(confirm), first)
        self.assertEqual((len(self.frameio.posts), self.count('media_locations'), len(self.writer.calls)), (posts, media, 1))

    def test_a_crash_between_the_frameio_call_and_the_t6_record_creates_no_duplicate_on_retry(self) -> None:
        from t6.media import MediaStore
        preview = self.preview()
        confirm = {**self.values, 'idempotency_key': 'start', 'confirm': preview.token}
        real = MediaStore.write

        def crash(store: Any, verb: str, values: dict[str, Any], **kwargs: Any) -> Any:
            if values.get('kind') == 'frameio-folder':
                raise RuntimeError('process died after the Frame.io call')
            return real(store, verb, values, **kwargs)

        with patch.object(MediaStore, 'write', crash), self.assertRaises(RuntimeError):
            self.call(confirm)
        project = next(iter(self.frameio.projects.values()))
        made = len(self.frameio.posts)
        self.assertEqual(made, 3)  # one project and two folders
        self.assertEqual(self.locations('frameio-folder'), [])
        done = self.call(confirm)
        assert isinstance(done, Done), done
        self.assertEqual((len(self.frameio.projects), len(self.frameio.posts)), (1, made))
        self.assertEqual(self.locations('frameio-project')[0][1], project['id'])
        self.assertEqual(len(self.locations('frameio-folder')), 2)

    def later(self, seconds: float) -> None:
        """Move the signing clock on, so a token made before now is past its fifteen minutes (the verb builds its own PreviewTokens)."""
        import time
        from t6.preview import PreviewTokens
        real = PreviewTokens.__init__

        def init(tokens: Any, key: str, *, expiry: float = 900, clock: Any = None) -> None:
            real(tokens, key, expiry=expiry, clock=lambda: time.time() + seconds)

        patcher = patch.object(PreviewTokens, '__init__', init)
        patcher.start()
        self.addCleanup(patcher.stop)

    def crash_at_folders(self) -> Any:
        from t6.media import MediaStore
        real = MediaStore.write

        def crash(store: Any, verb: str, values: dict[str, Any], **kwargs: Any) -> Any:
            if values.get('kind') == 'frameio-folder':
                raise RuntimeError('process died after the Frame.io call')
            return real(store, verb, values, **kwargs)

        return patch.object(MediaStore, 'write', crash)

    def totals(self) -> tuple[int, int]:
        return self.count('approvals'), self.count('deliverables')

    def test_a_finished_confirm_retried_with_its_key_after_the_token_expired_returns_the_first_result(self) -> None:
        confirm = {**self.values, 'idempotency_key': 'start', 'confirm': self.preview().token}
        first = self.call(confirm)
        assert isinstance(first, Done)
        self.later(16 * 60)
        self.assertEqual(self.call(confirm), first)
        self.assertEqual(self.totals(), (1, 2))

    def test_an_expired_token_with_nothing_begun_is_still_refused(self) -> None:
        confirm = {**self.values, 'idempotency_key': 'start', 'confirm': self.preview().token}
        self.later(16 * 60)
        refused = self.call(confirm)
        assert isinstance(refused, Refused)
        self.assertEqual((refused.rule, self.totals()), ('token_expired', (0, 0)))

    def test_a_start_that_stopped_halfway_is_finished_with_its_key_after_the_token_expired(self) -> None:
        confirm = {**self.values, 'idempotency_key': 'start', 'confirm': self.preview().token}
        with self.crash_at_folders(), self.assertRaises(RuntimeError):
            self.call(confirm)
        self.assertEqual(self.totals(), (1, 2))
        self.later(16 * 60)
        done = self.call(confirm)
        assert isinstance(done, Done), done
        self.assertEqual((self.totals(), len(self.locations('frameio-folder')), len(self.frameio.projects)), ((1, 2), 2, 1))
        self.assertEqual(self.call(confirm), done)
        self.assertEqual(self.totals(), (1, 2))

    def test_a_different_key_for_a_project_whose_start_has_begun_is_refused_at_preview_and_at_confirm(self) -> None:
        first = {**self.values, 'idempotency_key': 'start', 'confirm': self.preview().token}
        second = {**self.values, 'idempotency_key': 'other', 'confirm': self.preview(key='other').token}  # both planned before either ran
        with self.crash_at_folders(), self.assertRaises(RuntimeError):
            self.call(first)
        refused = self.call(second)
        assert isinstance(refused, Refused)
        self.assertEqual(refused.rule, 'project_started')
        again = self.preview(key='third')
        assert isinstance(again, Refused)
        self.assertEqual(again.rule, 'project_started')
        self.assertEqual(self.totals(), (1, 2))
        self.later(16 * 60)
        self.assertIsInstance(self.call(first), Done)
        self.assertEqual(self.totals(), (1, 2))
        other = self.call(second)
        assert isinstance(other, Refused)
        self.assertEqual(self.totals(), (1, 2))

    def test_a_frameio_failure_refuses_the_confirm_and_changes_nothing_in_t6_so_a_retry_works(self) -> None:
        preview = self.preview()
        confirm = {**self.values, 'idempotency_key': 'start', 'confirm': preview.token}
        self.writer.failure = Failed('no answer from the Frame.io worker within 60 seconds')
        refused = self.call(confirm)
        assert isinstance(refused, Refused)
        self.assertEqual(refused.rule, 'frameio')
        self.assertIn('no answer', refused.reason)
        self.assertEqual((self.count('approvals'), self.count('deliverables'), self.count('media_locations'), self.count('used_preview_tokens')), (0, 0, 0, 0))
        self.writer.failure = None
        self.assertIsInstance(self.call(confirm), Done)
        self.assertEqual((len(self.frameio.projects), self.count('media_locations')), (1, 5))

    def test_with_no_frameio_worker_configured_confirm_is_refused_and_changes_nothing(self) -> None:
        preview = self.preview()
        refused = self.call({**self.values, 'idempotency_key': 'start', 'confirm': preview.token}, frameio=None)
        assert isinstance(refused, Refused)
        self.assertEqual(refused.rule, 'config')
        self.assertEqual((self.count('approvals'), self.count('media_locations')), (0, 0))

    def test_no_share_is_created_and_nothing_is_uploaded(self) -> None:
        self.confirm()
        self.assertEqual(self.frameio.shares_created, 0)
        self.assertFalse([path for path, _ in self.frameio.posts if 'share' in path])
        self.assertFalse([path for path in self.frameio.requests if 'share' in path])
        self.assertTrue(all(path.endswith(('/projects', '/folders')) for path, _ in self.frameio.posts))

    def link_by_hand(self, project: str, frameio_project: str) -> None:
        from t6.media import MediaStore
        store = MediaStore(self.registry, self.people.tokens)
        values = {'project': project, 'frameio_project': frameio_project}
        preview = store.frameio_project_write('link', values, actor='starter', idempotency_key='hand-' + frameio_project)
        assert isinstance(preview, Preview)
        assert isinstance(store.frameio_project_write('link', values, actor='starter', idempotency_key='hand-' + frameio_project, confirm=preview.token), Done)

    def test_a_frameio_project_that_belongs_to_another_project_is_not_adopted_by_its_name(self) -> None:
        other = self.registry.register(actor='setup', idempotency_key='other', notion_page_id=str(self.fake.add_page('projects', project_properties(name='Show', project_type='Feature Film'))['id']))
        assert isinstance(other, Registered)
        self.frameio.add_project('fp-existing', 'Show')
        self.link_by_hand(other.project.id, 'fp-existing')
        done = self.confirm()
        assert isinstance(done, Done), done
        self.assertEqual(len(self.frameio.projects), 2)
        self.assertNotEqual(done.result['frameio_project_id'], 'fp-existing')
        self.assertEqual(self.frameio.folders.get('root-fp-existing', []), [])
        self.assertEqual(self.writer.calls_taken, [('fp-existing',)])

    def test_a_project_already_linked_by_hand_keeps_its_frameio_project_and_gets_only_folders(self) -> None:
        self.frameio.add_project('fp-hand', 'A Different Name')
        self.link_by_hand(self.project, 'fp-hand')
        preview = self.preview()
        done = self.call({**self.values, 'idempotency_key': 'start', 'confirm': preview.token})
        assert isinstance(done, Done), done
        self.assertEqual(len(self.frameio.projects), 1)
        self.assertEqual(sorted(f['name'] for f in self.frameio.folders['root-fp-hand']), ['Ep01', 'Ep02'])
        self.assertEqual(self.locations('frameio-project'), [(self.project, 'fp-hand', 'frameio-project')])
        self.assertEqual(self.writer.calls_known, ['fp-hand'])

    def test_a_project_with_no_name_in_notion_cannot_be_planned(self) -> None:
        self.store.project_name = lambda page: None
        refused = self.preview(key='noname')
        assert isinstance(refused, Refused)
        self.assertEqual(refused.rule, 'project_name')

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
        for kind in ('frameio-project', 'frameio-folder'):  # the Frame.io locations come from the Frame.io step only
            frameio = self.preview({**self.values, 'media': [{'kind': kind, 'ref': 'abc', 'role': 'review'}]}, key='f' + kind)
            assert isinstance(frameio, Refused)
            self.assertEqual(frameio.rule, 'frameio')
        duplicate = self.preview({**self.values, 'deliverables': EPISODES[:1] * 2}, key='d')
        assert isinstance(duplicate, Refused)
        self.assertEqual(duplicate.rule, 'deliverable_code')
        self.assertEqual((self.count('approvals'), self.count('deliverables')), (0, 0))

    def define(self, code: str) -> None:
        with self.registry._transaction() as db:
            db.execute('INSERT INTO deliverables VALUES (?, ?, ?, NULL, NULL, NULL)', (str(T6Id.mint(EntityKind.DELIVERABLE)), self.project, code))

    def test_a_deliverable_code_the_project_already_has_is_refused_at_preview(self) -> None:
        """One Frame.io folder per code: a Start may not give a code a second owner."""
        self.define('Ep02')
        refused = self.preview()
        assert isinstance(refused, Refused)
        self.assertEqual(refused.rule, 'deliverable_code')
        self.assertIn('Ep02', refused.reason)
        self.assertEqual((self.count('approvals'), self.count('deliverables')), (0, 1))

    def test_a_deliverable_code_defined_after_the_preview_is_refused_at_confirm_and_nothing_is_made(self) -> None:
        confirm = {**self.values, 'idempotency_key': 'start', 'confirm': self.preview().token}
        self.define('Ep01')
        refused = self.call(confirm)
        assert isinstance(refused, Refused)
        self.assertEqual(refused.rule, 'deliverable_code')
        self.assertEqual((self.count('approvals'), self.count('deliverables'), len(self.frameio.projects)), (0, 1, 0))

    def test_a_resumed_start_is_not_refused_by_its_own_deliverables(self) -> None:
        confirm = {**self.values, 'idempotency_key': 'start', 'confirm': self.preview().token}
        with self.crash_at_folders(), self.assertRaises(RuntimeError):
            self.call(confirm)
        self.assertEqual(self.totals(), (1, 2))  # Ep01 and Ep02 now exist, made by this key
        self.later(16 * 60)
        done = self.call(confirm)
        assert isinstance(done, Done), done
        self.assertEqual(self.totals(), (1, 2))

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
        with patch('t6.notion.NotionClient', side_effect=lambda token: self.fake.client()):  # each read closes its own
            worker = Worker(InternalQueueClient(InternalJobQueue(self.path)), 'main', handlers)
            self.assertTrue(worker.run_once())
        preview = store.job_result(started.result['job_id'], actor='starter')
        assert isinstance(preview, Preview)
        template = next(step for step in preview.plan['steps'] if step['step'] == 'milestone template')
        self.assertEqual(template['template'], 'tvc')
        confirmed = store.confirm({'project': self.project, 'deliverables': [{'code': 'Spot', 'episode': None, 'due': None, 'air': None}],
                                   'media': [], 'bookings': []}, actor='starter', idempotency_key='k', token=preview.token, frameio=FakeFrameioWriter(FakeFrameio()))
        assert isinstance(confirmed, Done), confirmed
        self.assertEqual(len(confirmed.result['deliverable_ids']), 1)
        self.assertEqual(next(step['project_name'] for step in preview.plan['steps'] if step['step'] == 'frameio'), 'Show')  # read from Notion by the worker

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
