import unittest
from typing import Any

from t6.deliverable import DeliverableStore
from t6.frameio.model import Version
from t6.ids import EntityKind, T6Id
from t6.preview import PreviewTokens
from t6.registry import Registered
from t6.version import VersionStore
from t6.verbs import Done, Preview, Refused, catalogue
from test_person import MemoryRegistry


class Reader:
    def versions(self, file_id: str) -> tuple[Version, ...]:
        return (Version(file_id=file_id, name='cut', stack_id='stack'),)


class VersionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = MemoryRegistry()
        self.addCleanup(self.registry.db.close)
        self.registry._new_id = lambda: str(T6Id.mint(EntityKind.PROJECT))
        project = self.registry.register(actor='setup', idempotency_key='setup', notion_page_id='p')
        assert isinstance(project, Registered)
        deliverables = DeliverableStore(self.registry, PreviewTokens('secret'))
        rows = [{'page_id': 'd', 'project_page': 'p', 'code': 'FTR'}]
        preview = deliverables.migrate(rows, actor='setup')
        self.deliverable = deliverables.migrate(rows, actor='setup', apply=True, digest=preview['digest'])['entries'][0]['t6_id']
        self.deliverables = deliverables
        self.store = VersionStore(self.registry, PreviewTokens('secret'), Reader())

    def write(self, verb: str, values: dict[str, Any], key: str) -> Done:
        preview = self.store.write(verb, values, actor='alice', idempotency_key=key)
        assert isinstance(preview, Preview)
        done = self.store.write(verb, values, actor='alice', idempotency_key=key, confirm=preview.token)
        assert isinstance(done, Done)
        return done

    def version(self, file_id: str) -> dict[str, Any]:
        record = self.store.for_file(file_id)
        assert record is not None
        return record

    def test_add_and_resolve_review_file(self) -> None:
        values = {'deliverable': self.deliverable, 'frameio_file': 'file', 'note': 'rough'}
        result = self.write('add', values, 'one')
        self.assertEqual(result, self.write('add', values, 'one'))
        self.assertEqual(self.version('file')['id'], result.result['version_id'])
        self.assertEqual(self.version('file')['frameio_stack_id'], 'stack')
        with self.registry._transaction() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM versions').fetchone()[0], 1)

    def test_lock_move_and_stale_preview(self) -> None:
        ids = [self.write('add', {'deliverable': self.deliverable, 'frameio_file': f, 'note': None}, f).result['version_id'] for f in ('a', 'b')]
        self.write('lock', {'version': ids[0]}, 'lock-a')
        preview = self.store.write('lock', {'version': ids[1]}, actor='alice', idempotency_key='lock-b')
        assert isinstance(preview, Preview)
        self.assertIn(ids[0], preview.plan['message'])
        self.assertIn(ids[1], preview.plan['message'])
        self.assertIsInstance(self.store.write('lock', {'version': ids[1]}, actor='alice', idempotency_key='lock-b', confirm=preview.token), Done)
        deliverable = self.deliverables.get(self.deliverable)
        assert deliverable is not None
        self.assertEqual([m['milestone'] for m in deliverable['milestones']], ['piclock', 'piclock'])
        with self.registry._transaction() as db:
            self.assertEqual(db.execute('SELECT version_id FROM version_locks').fetchone()[0], ids[1])
        self.assertEqual({v['verb'] for v in catalogue() if v['noun'] == 'version'}, {'add', 'lock', 'find'})
