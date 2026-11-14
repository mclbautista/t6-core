import sqlite3
import unittest
from typing import Any

from t6.deliverable import MILESTONES, FILM_PIPELINE, film_stage
from t6.verbs import Done, Preview, Refused, catalogue
from test_person import MemoryRegistry
from t6.deliverable import DeliverableStore
from t6.ids import EntityKind, T6Id
from t6.preview import PreviewTokens
from t6.registry import Registered


class MilestoneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = MemoryRegistry()
        self.addCleanup(self.registry.db.close)
        self.registry._new_id = lambda: str(T6Id.mint(EntityKind.PROJECT))
        project = self.registry.register(actor='setup', idempotency_key='setup', notion_page_id='project-page')
        assert isinstance(project, Registered)
        self.project = project.project.id
        self.store = DeliverableStore(self.registry, PreviewTokens('secret'))
        plan = self.store.migrate([{'page_id': 'd', 'project_page': 'project-page', 'code': 'FTR'}], actor='setup')
        result = self.store.migrate([{'page_id': 'd', 'project_page': 'project-page', 'code': 'FTR'}], actor='setup', apply=True, digest=plan['digest'])
        self.identifier = result['entries'][0]['t6_id']

    def record(self, name: str, key: str) -> Any:
        values = {'deliverable': self.identifier, 'milestone': name, 'on': '2026-11-14'}
        preview = self.store.record_milestone(values, actor='alice', idempotency_key=key)
        assert isinstance(preview, Preview)
        return self.store.record_milestone(values, actor='alice', idempotency_key=key, confirm=preview.token)

    def _get(self) -> dict[str, Any]:
        record = self.store.get(self.identifier)
        assert record is not None
        return record

    def test_film_rules_and_independent_tracks(self) -> None:
        self.assertIsNone(film_stage([])['position'])
        for milestone in FILM_PIPELINE:
            self.assertEqual(film_stage([{'milestone': milestone}])['position'], milestone)
        for name, track in [('color delivered', 'color'), ('VFX approved', 'VFX'), ('printmaster in', 'sound')]:
            stage = film_stage([{'milestone': name}])
            self.assertEqual(stage['tracks'][track], 'complete')
            self.assertEqual(sum(value == 'complete' for value in stage['tracks'].values()), 1)
        self.record('delivered', 'one')
        self.assertEqual(self._get()['stage']['position'], 'delivered')
        self.record('ingested', 'two')
        self.assertEqual(self._get()['stage']['position'], 'delivered')

    def test_history_replay_and_recomputation(self) -> None:
        first = self.record('piclock', 'one')
        self.assertIsInstance(first, Done)
        self.assertEqual(first, self.record('piclock', 'one'))
        self.record('piclock', 'two')
        record = self._get()
        self.assertEqual(len(record['milestones']), 2)
        self.assertNotEqual(record['milestones'][0]['id'], record['milestones'][1]['id'])
        with self.registry._transaction() as db:
            self.assertNotIn('stage', [r[1] for r in db.execute('PRAGMA table_info(deliverables)')])
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute('DELETE FROM milestones')
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE milestones SET milestone='delivered'")
            db.execute('INSERT INTO milestones VALUES (?, ?, ?, ?, ?, ?)', ('t6s_external', self.identifier, 'archived', '2026-11-14', 'alice', 1))
        self.assertEqual(self._get()['stage']['position'], 'archived')
        self.assertEqual(self.registry.activity(self.project)[0].action, 'milestone_record')

    def test_invalid_and_catalogue(self) -> None:
        self.assertIsInstance(self.store.record_milestone({'deliverable': self.identifier, 'milestone': 'unknown'}, actor='alice'), Refused)
        self.assertEqual(len(MILESTONES), 11)
        self.assertTrue(any(v['noun'] == 'deliverable' and v['verb'] == 'milestone record' for v in catalogue()))
