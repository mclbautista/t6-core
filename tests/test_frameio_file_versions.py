"""A Frame.io file event for a Deliverable's T6 folder records a Version (#265). Fake reader, in-memory t6 database."""
import tempfile
import unittest
from pathlib import Path
from typing import Any

from t6.deliverable import DeliverableStore
from t6.frameio.file_versions import FileLocation, FrameioFileVersions
from t6.frameio.requests import Failed
from t6.frameio.worker import FrameioWorker
from t6.ids import EntityKind, T6Id
from t6.media import MediaStore
from t6.preview import PreviewTokens
from t6.registry import Registered
from t6.version import VersionStore
from t6.verbs import Done, Preview
from t6.worker.__main__ import WorkerSettings, configured_handlers
from t6.worker.client import WorkerJob
from test_person import MemoryRegistry

KIND = 'frameio.file_versioned'


class FakeReader:
    """What the Frame.io worker's read API says about a file: its folder, and its version stack if it has one."""

    def __init__(self) -> None:
        self.locations: dict[str, FileLocation | Failed] = {}
        self.asked: list[str] = []

    def file_location(self, file_id: str) -> FileLocation | Failed:
        self.asked.append(file_id)
        return self.locations.get(file_id, Failed('Frame.io gave no file ' + file_id))


def event(file_id: str, kind: str = 'file.versioned') -> dict[str, Any]:
    return {'type': kind, 'resource': {'id': file_id, 'type': 'file'}, 'project': {'id': 'fp-1'}, 'account': {'id': 'acct'}}


def job(key: str, body: dict[str, Any]) -> WorkerJob:
    return WorkerJob(1, 'main', KIND, key, body, 'token', 1)


class FileVersionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = MemoryRegistry()
        self.addCleanup(self.registry.db.close)
        self.registry._new_id = lambda: str(T6Id.mint(EntityKind.PROJECT))
        project = self.registry.register(actor='setup', idempotency_key='setup', notion_page_id='p')
        assert isinstance(project, Registered)
        self.project = project.project.id
        deliverables = DeliverableStore(self.registry, PreviewTokens('secret'))
        rows = [{'page_id': 'd', 'project_page': 'p', 'code': 'FTR'}, {'page_id': 'e', 'project_page': 'p', 'code': 'TRL'}]
        preview = deliverables.migrate(rows, actor='setup')
        entries = deliverables.migrate(rows, actor='setup', apply=True, digest=preview['digest'])['entries']
        self.deliverable, self.other = entries[0]['t6_id'], entries[1]['t6_id']
        self.media = MediaStore(self.registry, PreviewTokens('secret'))
        self.link(self.deliverable, 'frameio-folder', 'folder-1', 'review')
        self.reader = FakeReader()
        self.reader.locations['file-1'] = FileLocation(folder_id='folder-1')
        self.handler = FrameioFileVersions(self.registry, self.reader).handle
        self.versions = VersionStore(self.registry, PreviewTokens('secret'))

    def link(self, owner: str, kind: str, ref: str, role: str) -> None:
        values = {'owner': owner, 'kind': kind, 'ref': ref, 'role': role}
        key = f'link-{owner}-{kind}-{ref}'
        preview = self.media.write('link', values, actor='person', idempotency_key=key)
        assert isinstance(preview, Preview)
        assert isinstance(self.media.write('link', values, actor='person', idempotency_key=key, confirm=preview.token), Done)

    def count(self) -> int:
        with self.registry._transaction() as db:
            return int(db.execute('SELECT COUNT(*) FROM versions').fetchone()[0])

    def entries(self) -> list[Any]:
        return [e for e in self.registry.activity(self.project) if e.action == 'version_add']

    def test_a_file_in_the_deliverables_folder_records_one_version(self) -> None:
        self.handler(job('edge:/frameio:evt-1', event('file-1')))
        version = self.versions.for_file('file-1')
        assert version is not None
        self.assertEqual((version['deliverable_id'], version['frameio_stack_id'], version['locked']), (self.deliverable, None, False))
        self.assertEqual(self.count(), 1)
        self.assertEqual([(e.subject_id, e.actor) for e in self.entries()], [(version['id'], 'frameio')])

    def test_an_upload_completed_event_records_the_stack_the_file_is_in(self) -> None:
        self.reader.locations['file-1'] = FileLocation(folder_id='folder-1', stack_id='stack-1')
        self.handler(job('edge:/frameio:evt-1', event('file-1', 'file.upload.completed')))
        version = self.versions.for_file('file-1')
        assert version is not None
        self.assertEqual(version['frameio_stack_id'], 'stack-1')

    def test_the_same_event_again_creates_nothing(self) -> None:
        self.handler(job('edge:/frameio:evt-1', event('file-1')))
        self.reader.asked.clear()
        self.handler(job('edge:/frameio:evt-1', event('file-1')))
        self.assertEqual((self.count(), len(self.entries()), self.reader.asked), (1, 1, []))

    def test_another_event_for_the_same_file_creates_nothing(self) -> None:
        self.handler(job('edge:/frameio:evt-1', event('file-1')))
        self.handler(job('edge:/frameio:evt-2', event('file-1', 'file.upload.completed')))
        self.assertEqual((self.count(), len(self.entries())), (1, 1))

    def test_a_file_outside_any_t6_folder_is_ignored(self) -> None:
        self.reader.locations['file-2'] = FileLocation(folder_id='folder-elsewhere')
        self.reader.locations['file-3'] = FileLocation(folder_id=None)
        self.assertEqual(self.handler(job('edge:/frameio:evt-2', event('file-2'))), {'outcome': 'ignored', 'reason': 'not a T6 folder'})
        self.handler(job('edge:/frameio:evt-3', event('file-3')))
        self.assertEqual((self.count(), self.entries()), (0, []))

    def test_only_an_active_frameio_folder_with_role_review_counts(self) -> None:
        self.link(self.deliverable, 'frameio-project', 'project-ref', 'review')
        self.link(self.deliverable, 'nas-path', '/nas/cuts', 'review')
        self.reader.locations['file-2'] = FileLocation(folder_id='project-ref')
        self.reader.locations['file-3'] = FileLocation(folder_id='/nas/cuts')
        self.handler(job('edge:/frameio:evt-2', event('file-2')))
        self.handler(job('edge:/frameio:evt-3', event('file-3')))
        with self.registry._transaction() as db:
            db.execute("UPDATE media_locations SET active = 0 WHERE kind = 'frameio-folder'")
        self.handler(job('edge:/frameio:evt-4', event('file-1')))
        self.assertEqual(self.count(), 0)

    def test_a_file_already_recorded_for_another_deliverable_is_left_there(self) -> None:
        self.link(self.other, 'frameio-folder', 'folder-2', 'review')
        self.handler(job('edge:/frameio:evt-1', event('file-1')))
        self.reader.locations['file-1'] = FileLocation(folder_id='folder-2')
        self.handler(job('edge:/frameio:evt-2', event('file-1')))
        version = self.versions.for_file('file-1')
        assert version is not None
        self.assertEqual((version['deliverable_id'], self.count()), (self.deliverable, 1))

    def test_other_event_types_and_bodies_are_ignored_without_a_read(self) -> None:
        for body in (event('file-1', 'comment.created'), {'type': 'file.versioned'}, {'type': 'file.versioned', 'resource': {}}, {}):
            self.handler(job('edge:/frameio:other', body))
        self.assertEqual((self.count(), self.reader.asked), (0, []))

    def test_a_failed_read_is_retried_and_records_nothing_until_it_works(self) -> None:
        self.reader.locations['file-1'] = Failed('Frame.io sign-in needed')
        with self.assertRaises(RuntimeError):
            self.handler(job('edge:/frameio:evt-1', event('file-1')))
        self.assertEqual(self.count(), 0)
        self.reader.locations['file-1'] = FileLocation(folder_id='folder-1')
        self.handler(job('edge:/frameio:evt-1', event('file-1')))
        self.assertEqual(self.count(), 1)


class WiringTests(unittest.TestCase):
    ENVIRON = {'T6_SERVER_URL': 'http://edge', 'T6_WORKER_SECRET': 's', 'T6_WORKER_QUEUE': 'main', 'T6_API_URL': 'http://api'}

    def handlers(self, **extra: str) -> dict[str, Any]:
        environ = {**self.ENVIRON, **extra}
        return configured_handlers(environ, WorkerSettings.from_env(environ), object()).get('main', {})  # type: ignore[arg-type]

    def test_the_main_worker_with_the_database_handles_the_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertIn(KIND, self.handlers(T6_DATABASE=str(Path(directory) / 't6.sqlite3')))

    def test_a_worker_without_the_database_does_not(self) -> None:
        self.assertNotIn(KIND, self.handlers())

    def test_the_frameio_worker_does_not_handle_it(self) -> None:
        worker = FrameioWorker(object(), lambda request: None, lambda text, key: None)  # type: ignore[arg-type]
        self.assertNotIn(KIND, worker.handlers())
