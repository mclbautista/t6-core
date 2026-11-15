import sqlite3
import unittest

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
