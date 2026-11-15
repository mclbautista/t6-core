"""Frame.io project and folder creation for Project Start (#275), on the fake Frame.io API.

The Frame.io worker makes the project and one folder per Deliverable code, once: a request that is
delivered again, or asked again after a crash, finds what exists by name and creates nothing.
Names below are invented.
"""
import unittest
from typing import Any

from t6.frameio import CreateProjectFolders, Failed, FrameioWorker
from t6.frameio.api import FrameioApi, FrameioError
from t6.frameio.project_folders import LocalFrameioQueue, ProjectFolders, QueueFrameioWriter
from t6.frameio.reads import FrameioReads
from t6.frameio.requests import from_job, to_payload
from t6.frameio.testing import ACCOUNT, FakeFrameio, StaticTokens
from t6.worker import QueueClient, Worker
from test_frameio_worker import FakeTokens, FrameioWorkerTestCase
from test_server_app import FRAMEIO_WORKER_SECRET, InProcess

REQUEST = CreateProjectFolders(project_id='t6p_1', name='Example Show', folders=('Ep01', 'Ep02'))


class FolderTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.frameio = FakeFrameio()
        self.api = FrameioApi(StaticTokens(), self.frameio.fetch, lambda _: None, send=self.frameio.send)
        self.reads = FrameioReads(self.api)

    def run_request(self, request: CreateProjectFolders = REQUEST) -> dict[str, Any]:
        found = self.reads(request)
        assert isinstance(found, dict)
        return found


class CreateTests(FolderTestCase):
    def test_a_new_project_gets_the_project_and_one_folder_per_code(self) -> None:
        found = self.run_request()
        self.assertEqual(len(self.frameio.projects), 1)
        project = next(iter(self.frameio.projects.values()))
        self.assertEqual(project['name'], 'Example Show')
        self.assertEqual(found['frameio_project_id'], project['id'])
        self.assertEqual(set(found['folders']), {'Ep01', 'Ep02'})
        children = self.frameio.folders[project['root_folder_id']]
        self.assertEqual(sorted(child['name'] for child in children), ['Ep01', 'Ep02'])
        self.assertEqual({child['id'] for child in children}, set(found['folders'].values()))
        self.assertTrue(found['created_project'])
        self.assertEqual(sorted(found['created_folders']), ['Ep01', 'Ep02'])

    def test_the_same_request_again_creates_nothing(self) -> None:
        first = self.run_request()
        writes = len(self.frameio.posts)
        second = self.run_request()
        self.assertEqual(len(self.frameio.posts), writes)
        self.assertEqual(second['frameio_project_id'], first['frameio_project_id'])
        self.assertEqual(second['folders'], first['folders'])
        self.assertFalse(second['created_project'])
        self.assertEqual(second['created_folders'], [])

    def test_a_crash_after_the_project_was_made_adopts_it_and_makes_only_the_missing_folders(self) -> None:
        self.frameio.add_project('fp9', 'Example Show')
        self.frameio.add_folder('root-fp9', 'f-old', 'Ep01')
        found = self.run_request()
        self.assertEqual(found['frameio_project_id'], 'fp9')
        self.assertEqual(found['folders']['Ep01'], 'f-old')
        self.assertEqual(found['created_folders'], ['Ep02'])
        self.assertEqual(len(self.frameio.projects), 1)
        self.assertEqual(sum(1 for path, _ in self.frameio.posts if path.endswith('/projects')), 0)

    def test_a_stored_project_id_is_used_whatever_the_name_is_and_no_project_is_made(self) -> None:
        self.frameio.add_project('fp7', 'Hand Linked Name')
        found = self.run_request(CreateProjectFolders(project_id='t6p_1', name='Example Show', folders=('Ep01',), known_project_id='fp7'))
        self.assertEqual(found['frameio_project_id'], 'fp7')
        self.assertFalse(found['created_project'])
        self.assertEqual([child['name'] for child in self.frameio.folders['root-fp7']], ['Ep01'])
        self.assertEqual(len(self.frameio.projects), 1)

    def test_a_stored_project_id_that_frameio_does_not_know_is_a_failure_not_a_new_project(self) -> None:
        with self.assertRaises(FrameioError):
            self.run_request(CreateProjectFolders(project_id='t6p_1', name='Example Show', folders=('Ep01',), known_project_id='gone'))
        self.assertEqual((self.frameio.projects, self.frameio.posts), ({}, []))

    def test_a_project_that_belongs_to_another_t6_project_is_not_adopted_by_its_name(self) -> None:
        self.frameio.add_project('fp-other', 'Example Show')
        found = self.run_request(CreateProjectFolders(project_id='t6p_1', name='Example Show', folders=('Ep01',), taken=('fp-other',)))
        self.assertNotEqual(found['frameio_project_id'], 'fp-other')
        self.assertTrue(found['created_project'])
        self.assertEqual(self.frameio.folders.get('root-fp-other', []), [])

    def test_two_projects_with_the_name_are_never_guessed_between_and_nothing_is_made(self) -> None:
        self.frameio.add_project('fp1', 'Example Show')
        self.frameio.add_project('fp2', 'Example Show')
        with self.assertRaisesRegex(ValueError, 'more than one'):
            self.run_request()
        self.assertEqual(self.frameio.posts, [])

    def test_the_name_is_matched_exactly_not_by_prefix_or_case(self) -> None:
        self.frameio.add_project('fp1', 'Example Show 2')
        self.frameio.add_project('fp2', 'example show')
        found = self.run_request()
        self.assertEqual(len(self.frameio.projects), 3)
        self.assertNotIn(found['frameio_project_id'], ('fp1', 'fp2'))

    def test_the_project_goes_in_the_configured_account_and_workspace(self) -> None:
        self.frameio.workspaces.append({'id': 'ws-2'})
        with self.assertRaisesRegex(RuntimeError, 'T6_FRAMEIO_WORKSPACE_ID'):
            self.run_request()
        named = FrameioReads(self.api, account_id=ACCOUNT, workspace_id='ws-2')
        found = named(REQUEST)
        assert isinstance(found, dict)
        self.assertEqual(self.frameio.projects[str(found['frameio_project_id'])]['workspace_id'], 'ws-2')
        self.assertTrue(all(path.startswith(f'/v4/accounts/{ACCOUNT}/') for path, _ in self.frameio.posts))

    def test_a_refused_folder_write_fails_the_request_and_a_retry_finishes_it(self) -> None:
        self.frameio.refuse_posts(500, after=2)  # the project and the first folder go through
        with self.assertRaises(FrameioError):
            self.run_request()
        self.assertEqual(len(self.frameio.projects), 1)
        found = self.run_request()
        self.assertEqual(len(self.frameio.projects), 1)
        root = next(iter(self.frameio.projects.values()))['root_folder_id']
        self.assertEqual(sorted(child['name'] for child in self.frameio.folders[root]), ['Ep01', 'Ep02'])
        self.assertEqual(found['created_folders'], ['Ep02'])

    def test_no_share_is_ever_created(self) -> None:
        self.run_request()
        self.run_request()
        self.assertEqual(self.frameio.shares_created, 0)
        self.assertFalse([path for path, _ in self.frameio.posts if 'share' in path])
        self.assertFalse([path for path in self.frameio.requests if 'share' in path])
        self.assertTrue(all(path.endswith(('/projects', '/folders')) for path, _ in self.frameio.posts))

    def test_the_write_carries_the_name_and_the_token_of_the_worker_only(self) -> None:
        self.run_request()
        self.assertEqual(self.frameio.posts[0][1], {'data': {'name': 'Example Show'}})
        self.assertEqual(set(self.frameio.tokens_seen), {'Bearer access-0'})


class RequestShapeTests(unittest.TestCase):
    def test_the_request_survives_a_job_payload_and_needs_non_blank_text(self) -> None:
        self.assertEqual(from_job(REQUEST.kind, to_payload(REQUEST)), REQUEST)
        full = CreateProjectFolders(project_id='t6p_1', name='Show', folders=('Ep01',), known_project_id='fp1', taken=('fp2', 'fp3'))
        self.assertEqual(from_job(full.kind, to_payload(full)), full)
        for bad in (dict(name=' '), dict(known_project_id=' '), dict(taken=('fp2', ' ')), dict(folders=('Ep01', ' ')), dict(folders=('Ep01', 'Ep01'))):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                CreateProjectFolders(**{'project_id': 't6p_1', 'name': 'Show', 'folders': ('Ep01',), **bad})  # type: ignore[arg-type]

    def test_no_folders_is_a_project_alone(self) -> None:
        self.assertEqual(CreateProjectFolders(project_id='t6p_1', name='Show', folders=()).folders, ())


class QueueTests(FrameioWorkerTestCase):
    """The caller asks through the `frameio` queue; only the Frame.io worker holds the token."""

    def setUp(self) -> None:
        super().setUp()
        self.fake = FakeFrameio()
        tokens = FakeTokens()
        reads = FrameioReads(FrameioApi(StaticTokens(), self.fake.fetch, lambda _: None, send=self.fake.send))
        self.real = FrameioWorker(tokens, reads, self.notify, self.clock)
        self.real_worker = Worker(QueueClient(InProcess(self.app), FRAMEIO_WORKER_SECRET), 'frameio', self.real.handlers())

    def serve_real(self, seconds: float) -> None:
        self.real_worker.run_once()
        self.clock.advance(seconds)

    def writer(self) -> QueueFrameioWriter:
        return QueueFrameioWriter(LocalFrameioQueue(self.queue), timeout=30.0, clock=self.clock, sleep=self.serve_real)

    def test_the_writer_gets_the_ids_back_through_the_queue(self) -> None:
        found = self.writer().create_project_folders('t6p_1', 'Example Show', ('Ep01', 'Ep02'))
        assert isinstance(found, ProjectFolders)
        self.assertEqual(set(found.folders), {'Ep01', 'Ep02'})
        self.assertEqual(found.frameio_project_id, next(iter(self.fake.projects)))
        self.assertEqual(self.fake.shares_created, 0)

    def test_asking_twice_makes_one_project(self) -> None:
        first = self.writer().create_project_folders('t6p_1', 'Example Show', ('Ep01',))
        second = self.writer().create_project_folders('t6p_1', 'Example Show', ('Ep01',))
        assert isinstance(first, ProjectFolders) and isinstance(second, ProjectFolders)
        self.assertEqual((first.frameio_project_id, first.folders), (second.frameio_project_id, second.folders))
        self.assertEqual((second.created_project, second.created_folders), (False, ()))
        self.assertEqual(len(self.fake.projects), 1)
        self.assertEqual(len(self.fake.posts), 2)

    def test_a_worker_that_does_not_answer_is_a_failure_with_a_reason(self) -> None:
        writer = QueueFrameioWriter(LocalFrameioQueue(self.queue), timeout=5.0, clock=self.clock, sleep=lambda s: self.clock.advance(s))
        found = writer.create_project_folders('t6p_1', 'Example Show', ('Ep01',))
        assert isinstance(found, Failed)
        self.assertIn('no answer', found.reason)

    def test_a_failed_request_comes_back_as_failed(self) -> None:
        self.fake.refuse_posts(500)
        found = self.writer().create_project_folders('t6p_1', 'Example Show', ('Ep01',))
        assert isinstance(found, Failed)
        self.assertIn('500', found.reason)
        self.assertEqual(self.fake.projects, {})

    def test_the_read_api_has_no_route_for_it(self) -> None:
        from t6.frameio.readapi import ReadApi
        from t6.server.app import Request
        api = ReadApi(self.real, 'read-secret')
        for method in ('POST', 'PUT', 'GET'):
            answer = api.handle(Request(method, '/frameio/projects', {'Authorization': 'Bearer read-secret'}, b'{}'))
            self.assertEqual(answer.status, 200 if method == 'GET' else 405)
        self.assertEqual(api.handle(Request('POST', '/frameio/create-project-folders', {'Authorization': 'Bearer read-secret'}, b'{}')).status, 405)
        self.assertEqual(self.fake.posts, [])


if __name__ == '__main__':
    unittest.main()
