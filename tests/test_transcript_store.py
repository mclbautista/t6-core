import sqlite3
import unittest

from t6.server.testing import FakeClock
from t6.server.queue import DEFAULT_MAX_ATTEMPTS
from t6.worker.internal_queue import InternalJobQueue

from t6.ids import EntityKind, T6Id
from t6.registry import Registered
from test_person import MemoryRegistry


from media_seed import link_media


class TranscriptStoreTests(unittest.TestCase):
    def test_history_and_project_activity(self) -> None:
        from t6.transcript import TranscriptStore
        registry = MemoryRegistry()
        self.addCleanup(registry.db.close)
        registry._new_id = lambda: str(T6Id.mint(EntityKind.PROJECT))
        project = registry.register(actor='setup', idempotency_key='setup')
        assert isinstance(project, Registered)
        media = link_media(registry, project.project.id, '/audio')
        store = TranscriptStore(registry)
        transcript = store.start(media, None, {'language': 'fil'}, actor='person')
        self.assertTrue(transcript.startswith('t6x_'))
        self.assertEqual(store.start(media, None, {'language': 'fil'}, actor='person'), transcript)
        store.fail(transcript, actor='person')
        result = store.get(transcript)
        assert result is not None
        self.assertEqual([entry['action'] for entry in result['history']], ['created', 're-run', 'failed'])
        self.assertEqual([e.action for e in registry.activity(project.project.id)][:3], ['transcript_failed', 'transcript_re-run', 'transcript_created'])
        with self.assertRaises(sqlite3.IntegrityError), registry._transaction() as db:
            db.execute('DELETE FROM transcript_history')

    def test_the_dead_job_sweep_buries_jobs_of_the_transcribe_queue_only(self) -> None:
        from t6.transcript import TranscriptStore
        registry = MemoryRegistry()
        self.addCleanup(registry.db.close)
        clock = FakeClock()
        other = InternalJobQueue(registry, clock)  # another owner's queue in the same table: its jobs are not the Transcript store's to bury
        job = other.submit('main', 'x', 'stuck')
        for _ in range(DEFAULT_MAX_ATTEMPTS):
            self.assertIsNotNone(other.claim('main', 10))
            clock.advance(10)
        TranscriptStore(registry).is_running(media='t6m_none')  # looks for dead runs
        self.assertIsNone(other.result(job.job_id, 'main'))  # still its owner's to bury when its own worker claims
