"""PM Notes automation through the Concern processing boundary."""
import unittest
from typing import Any

from t6.concern import ConcernStore, WEEK
from t6.ids import EntityKind, T6Id
from t6.preview import PreviewTokens
from t6.registry import Registered
from t6.verbs import Done, Preview
from test_person import MemoryRegistry


class ConcernNotesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = MemoryRegistry()
        self.addCleanup(self.registry.db.close)
        self.now = 1800000000.0
        self.registry._clock = lambda: self.now
        self.registry._new_id = lambda: str(T6Id.mint(EntityKind.PROJECT))
        result = self.registry.register(actor='setup', idempotency_key='setup', notion_page_id='project')
        assert isinstance(result, Registered)
        self.project = result.project.id
        with self.registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES ('coordinator', 'staff', '[]', '[]', NULL, 0, 1)")
        self.store = ConcernStore(self.registry, PreviewTokens('secret'))

    def process(self, line: str, when: str, answer: dict[str, Any]) -> None:
        self.store.process_notes(self.project, 'coordinator', [{'text': line, 'as_of': when}], lambda _: answer)

    def get_concern(self, concern: str) -> dict[str, Any]:
        result = self.store.get(concern)
        assert result is not None
        return result

    def test_announce_then_close_and_person_reopens(self) -> None:
        self.process('Waiting on client for KDM', '2026-11-01', {'choice': 'new', 'quote': 'Waiting on client for KDM', 'waiting_on': 'client'})
        changes = self.store.changes(self.project, '2026-01-01')
        concern = changes[0]['concern_id']
        self.process('Client sent KDM', '2026-11-08', {'choice': 'settles', 'concern_id': concern, 'quote': 'Client sent KDM'})
        self.assertEqual(self.get_concern(concern)['state'], 'open')
        changes = self.store.changes(self.project, '2026-01-01')
        self.assertEqual([c['action'] for c in changes], ['concern_auto_raise', 'concern_auto_announce'])
        self.now += WEEK - 1
        self.store.apply_pending(self.project)
        self.assertEqual(self.get_concern(concern)['state'], 'open')
        self.now += 1
        self.store.apply_pending(self.project)
        self.assertEqual(self.get_concern(concern)['state'], 'closed')
        values = {'concern': concern}
        preview = self.store.write('reopen', values, actor='person', idempotency_key='undo')
        assert isinstance(preview, Preview)
        self.assertIsInstance(self.store.write('reopen', values, actor='person', idempotency_key='undo', confirm=preview.token), Done)
        self.assertEqual(self.get_concern(concern)['state'], 'open')
        self.assertEqual(len(self.store.changes(self.project, '2026-01-01')), 3)

    def test_bad_answers_and_old_evidence_do_not_change_concerns(self) -> None:
        self.process('Waiting on client', '2026-11-01', {'choice': 'new', 'quote': 'Waiting on client', 'waiting_on': 'client'})
        concern = self.store.changes(self.project, '2026-01-01')[0]['concern_id']
        self.process('Client delivered', '2026-11-08', {'choice': 'settles', 'concern_id': 'invented', 'quote': 'Client delivered'})
        self.process('Client delivered', '2026-11-08', {'choice': 'settles', 'concern_id': concern, 'quote': 'fabricated'})
        self.process('Client delivered', '2026-10-31', {'choice': 'settles', 'concern_id': concern, 'quote': 'Client delivered'})
        self.assertEqual(len(self.store.changes(self.project, '2026-01-01')), 1)
        self.assertEqual(self.get_concern(concern)['state'], 'open')

    def test_person_can_reject_announcement_before_close(self) -> None:
        self.process('Waiting on client', '2026-11-01', {'choice': 'new', 'quote': 'Waiting on client', 'waiting_on': 'client'})
        concern = self.store.changes(self.project, '2026-01-01')[0]['concern_id']
        self.process('Client delivered', '2026-11-08', {'choice': 'settles', 'concern_id': concern, 'quote': 'Client delivered'})
        preview = self.store.write('reopen', {'concern': concern}, actor='person', idempotency_key='reject')
        assert isinstance(preview, Preview)
        self.assertIsInstance(self.store.write('reopen', {'concern': concern}, actor='person', idempotency_key='reject', confirm=preview.token), Done)
        self.now += WEEK
        self.store.apply_pending(self.project)
        self.assertEqual(self.get_concern(concern)['state'], 'open')

    def test_reworded_line_continues_with_same_id_and_age(self) -> None:
        self.process('Waiting on client for KDM', '2026-11-01', {'choice': 'new', 'quote': 'Waiting on client for KDM', 'waiting_on': 'client'})
        concern = self.store.changes(self.project, '2026-01-01')[0]['concern_id']
        self.now += 100
        self.process('KDM still pending from client', '2026-11-08', {'choice': 'continues', 'concern_id': concern, 'quote': 'KDM still pending from client'})
        self.assertEqual(self.get_concern(concern)['age_seconds'], 100)
        self.assertEqual([c['concern_id'] for c in self.store.changes(self.project, '2026-01-01')], [concern, concern])

    def test_same_line_in_new_block_never_creates_duplicate(self) -> None:
        self.process('Waiting on client', '2026-11-01', {'choice': 'new', 'quote': 'Waiting on client', 'waiting_on': 'client'})
        self.process('Waiting on client', '2026-11-08', {'choice': 'new', 'quote': 'Waiting on client', 'waiting_on': 'client'})
        self.assertEqual(len(self.store.changes(self.project, '2026-01-01')), 1)

    def test_no_waiting_cue_rejects_a_false_new_answer(self) -> None:
        self.process('Color looks good', '2026-11-01', {'choice': 'new', 'quote': 'Color looks good', 'waiting_on': 'Color'})
        self.assertEqual(self.store.changes(self.project, '2026-01-01'), [])
