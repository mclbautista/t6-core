import sqlite3
import unittest
from typing import Any

from t6.concern import ConcernStore, WEEK
from t6.ids import EntityKind, T6Id
from t6.preview import PreviewTokens
from t6.registry import Registered
from t6.verbs import Done, Preview, Refused, catalogue
from test_person import MemoryRegistry


class ConcernTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = MemoryRegistry()
        self.addCleanup(self.registry.db.close)
        self.now = 1000000.0
        self.registry._clock = lambda: self.now
        self.registry._new_id = lambda: str(T6Id.mint(EntityKind.PROJECT))
        project = self.registry.register(actor='setup', idempotency_key='setup', notion_page_id='project')
        assert isinstance(project, Registered)
        self.project = project.project.id
        with self.registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES ('coordinator', 'staff', '[]', '[]', NULL, 0, 1)")
        self.store = ConcernStore(self.registry, PreviewTokens('secret'))

    def write(self, verb: str, values: dict[str, Any], key: str) -> dict[str, Any]:
        preview = self.store.write(verb, values, actor='person', idempotency_key=key)
        assert isinstance(preview, Preview)
        done = self.store.write(verb, values, actor='person', idempotency_key=key, confirm=preview.token)
        assert isinstance(done, Done)
        retry = self.store.write(verb, values, actor='person', idempotency_key=key)
        assert isinstance(retry, Preview)
        self.assertEqual(done, self.store.write(verb, values, actor='person', idempotency_key=key, confirm=retry.token))
        result = done.result
        assert isinstance(result, dict)
        return result

    def raise_concern(self) -> str:
        concern = self.write('raise', {'project': self.project, 'owner': 'coordinator', 'waiting_on': 'client', 'anchor': 'Waiting kay client — raw line', 'as_of': '2026-11-01'}, 'raise')['concern_id']
        assert isinstance(concern, str)
        return concern

    def test_lifecycle_timers_and_append_only(self) -> None:
        concern = self.raise_concern()
        opened = self.now
        self.now += 2 * WEEK
        self.assertTrue(self.store.silence(concern, opened, 'quiet'))
        self.assertFalse(self.store.silence(concern, opened, 'quiet'))
        self.write('note', {'concern': concern, 'text': 'May update'}, 'note')
        self.assertFalse(self.store.silence(concern, opened, 'expired'))
        evidence = self.now
        self.now += 2 * WEEK
        self.assertTrue(self.store.silence(concern, evidence, 'quiet'))
        self.now += 2 * WEEK
        self.assertTrue(self.store.silence(concern, evidence, 'expired'))
        self.assertFalse(self.store.silence(concern, evidence, 'expired'))
        self.write('reopen', {'concern': concern}, 'reopen')
        record = self.store.get(concern)
        assert record is not None
        self.assertEqual(record['age_seconds'], 6 * WEEK)
        self.assertEqual(record['anchor'], 'Waiting kay client — raw line')
        self.assertFalse(self.store.silence(concern, evidence, 'expired'))
        self.write('resolve', {'concern': concern}, 'resolve')
        self.now += 10 * WEEK
        self.assertFalse(self.store.silence(concern, evidence, 'expired'))
        record = self.store.get(concern)
        assert record is not None
        self.assertEqual(record['state'], 'closed')
        actions = [a.action for a in self.registry.activity(self.project) if a.subject_id == concern]
        for action in ('raise', 'note', 'resolve', 'reopen'):
            self.assertEqual(actions.count('concern_' + action), 1)
        with self.registry._transaction() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM audit WHERE project_id = ?', (concern,)).fetchone()[0], len(actions))
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE concern_notes SET text = 'changed'")
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute('DELETE FROM concern_notes')
            self.assertEqual(db.execute("SELECT COUNT(*) FROM internal_jobs WHERE kind = 'concern_silence'").fetchone()[0], 4)

    def test_manual_resolve_blocks_pending_timers_and_stale_preview(self) -> None:
        concern = self.raise_concern()
        opened = self.now
        stale = self.store.write('resolve', {'concern': concern}, actor='person', idempotency_key='stale')
        assert isinstance(stale, Preview)
        self.write('note', {'concern': concern, 'text': 'Still waiting'}, 'fresh-note')
        refused = self.store.write('resolve', {'concern': concern}, actor='person', idempotency_key='stale', confirm=stale.token)
        self.assertIsInstance(refused, Refused)
        self.write('resolve', {'concern': concern}, 'closed')
        self.now += 4 * WEEK
        self.assertFalse(self.store.silence(concern, opened, 'quiet'))
        self.assertFalse(self.store.silence(concern, opened, 'expired'))
        record = self.store.get(concern)
        assert record is not None
        self.assertEqual(record['state'], 'closed')

    def test_validation_and_catalogue(self) -> None:
        result = self.store.write('raise', {'project': self.project, 'owner': 'coordinator', 'waiting_on': '', 'anchor': 'hello', 'as_of': '2026-01-01'}, actor='person')
        self.assertIsInstance(result, Refused)
        self.assertTrue({'raise', 'note', 'resolve', 'reopen'} <= {v['verb'] for v in catalogue() if v['noun'] == 'concern'})

    def test_brief_reply_is_the_only_chat_text_kept(self) -> None:
        concern = self.raise_concern()
        self.write('record-message', {'concern': concern, 'chat': '-100', 'message_id': '42'}, 'brief')
        self.assertEqual(self.store.for_message('-100', '42'), [concern])
        self.assertEqual(self.store.for_message('-100', '43'), [])
        ignored = self.store.write('note', {'concern': concern, 'text': 'unrelated chat text', 'from_reply': '-100:43', 'message_id': '45'}, actor='replier', idempotency_key='ignored')
        self.assertEqual(ignored, Done(result={'ignored': True}))
        values = {'concern': concern, 'text': 'The client replied', 'from_reply': '-100:42', 'message_id': '44'}
        preview = self.store.write('note', values, actor='replier', idempotency_key='reply')
        assert isinstance(preview, Preview)
        result = self.store.write('note', values, actor='replier', idempotency_key='reply', confirm=preview.token)
        self.assertIsInstance(result, Done)
        record = self.store.get(concern)
        assert record is not None
        self.assertEqual([(n['text'], n['actor'], n['message_id']) for n in record['notes']], [('The client replied', 'replier', '44')])
        with self.registry._transaction() as db:
            dump = '\n'.join(db.iterdump())
            self.assertNotIn('unrelated chat text', dump)

    def test_brief_can_carry_multiple_concerns(self) -> None:
        first = self.raise_concern()
        second = self.write('raise', {'project': self.project, 'owner': 'coordinator', 'waiting_on': 'studio', 'anchor': 'Waiting', 'as_of': '2026-11-01'}, 'second')['concern_id']
        for concern in (first, second):
            self.write('record-message', {'concern': concern, 'chat': 'chat', 'message_id': '1'}, 'mapping-' + concern)
        self.assertEqual(set(self.store.for_message('chat', '1')), {first, second})

class ConcernQueueTests(unittest.TestCase):
    def test_scheduled_jobs_are_claimed_and_applied_once(self) -> None:
        import tempfile
        from pathlib import Path
        from t6.registry.sqlite import SqliteProjectRegistry
        from t6.worker.internal_queue import InternalJobQueue

        now = [1000000.0]
        clock = lambda: now[0]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'db'
            registry = SqliteProjectRegistry(path, clock=clock)
            project = registry.register(actor='setup', idempotency_key='setup', notion_page_id='project')
            assert isinstance(project, Registered)
            with registry._transaction() as db:
                db.execute("INSERT INTO persons VALUES ('coordinator', 'staff', '[]', '[]', NULL, 0, 1)")
            store = ConcernStore(registry, PreviewTokens('secret'))
            values = {'project': project.project.id, 'owner': 'coordinator', 'waiting_on': 'client', 'anchor': 'waiting on client', 'as_of': '2026-01-01'}
            preview = store.write('raise', values, actor='person', idempotency_key='key')
            assert isinstance(preview, Preview)
            result = store.write('raise', values, actor='person', idempotency_key='key', confirm=preview.token)
            assert isinstance(result, Done)
            queue = InternalJobQueue(path, clock)
            self.assertIsNone(queue.claim('main'))
            for state in ('quiet', 'expired'):
                now[0] += 2 * WEEK
                job = queue.claim('main')
                assert job is not None
                concern, evidence_at, target = job.payload['concern'], job.payload['evidence_at'], job.payload['target']
                assert isinstance(concern, str) and isinstance(evidence_at, (int, float)) and isinstance(target, str)
                self.assertTrue(store.silence(concern, evidence_at, target))
                self.assertFalse(store.silence(concern, evidence_at, target))
                self.assertTrue(queue.ack(job.job_id, job.token))
                record = store.get(result.result['concern_id'])
                assert record is not None
                self.assertEqual(record['state'], state)
