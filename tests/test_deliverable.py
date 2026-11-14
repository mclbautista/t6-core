"""Deliverable identity at the registry boundary."""
import unittest
from datetime import datetime, timedelta
from t6.domain.studio_time import MANILA
from t6.ids import EntityKind, T6Id
from t6.preview import PreviewTokens
from t6.registry import Registered
from t6.verbs import Done, Preview
from test_person import MemoryRegistry


class DeliverableTests(unittest.TestCase):
    def setUp(self) -> None:
        from t6.deliverable import DeliverableStore
        self.registry = MemoryRegistry()
        self.addCleanup(self.registry.db.close)
        self.registry._new_id = lambda: str(T6Id.mint(EntityKind.PROJECT))
        project = self.registry.register(actor='setup', idempotency_key='setup', notion_page_id='project-page')
        assert isinstance(project, Registered)
        self.project = project.project.id
        self.store = DeliverableStore(self.registry, PreviewTokens('secret'))

    def test_define_replay_creates_one_record_and_intent(self) -> None:
        values = {'project': self.project, 'code': 'Ep01', 'episode': 'Episode 1', 'due': '2026-12-01', 'air': None}
        preview = self.store.define(values, actor='alice', idempotency_key='key')
        assert isinstance(preview, Preview)
        done = self.store.define(values, actor='alice', idempotency_key='key', confirm=preview.token)
        assert isinstance(done, Done)
        identifier = done.result['deliverable_id']
        self.assertTrue(identifier.startswith('t6v_'))
        record = self.store.get(identifier)
        assert record is not None
        self.assertEqual(record['project_id'], self.project)
        self.assertEqual(self.store.list(self.project)[0]['id'], identifier)
        replay = self.store.define(values, actor='alice', idempotency_key='key')
        assert isinstance(replay, Preview)
        self.assertEqual(done, self.store.define(values, actor='alice', idempotency_key='key', confirm=replay.token))
        self.assertEqual(len([i for i in self.registry.pending_sync() if i.project_id == identifier]), 1)
        self.assertEqual(self.registry.activity(self.project)[0].subject_id, identifier)

    def test_migration_never_matches_names_and_rebinds_explicit_identity(self) -> None:
        rows = [{'page_id': 'one', 'project_page': 'project-page', 'code': 'Ep01'},
                {'page_id': 'two', 'project_page': 'project-page', 'code': 'Ep01'},
                {'page_id': 'unknown', 'project_page': 'missing', 'code': 'Ep02'}]
        preview = self.store.migrate(rows, actor='alice')
        applied = self.store.migrate(rows, actor='alice', apply=True, digest=preview['digest'])
        ids = {e['page_id']: e['t6_id'] for e in applied['entries']}
        self.assertNotEqual(ids['one'], ids['two'])
        self.assertIsNone(ids['unknown'])
        self.assertEqual(len(applied['review']), 1)
        again = self.store.migrate(rows, actor='alice', apply=True, digest=preview['digest'])
        self.assertEqual([e['action'] for e in again['entries']], ['keep', 'keep', 'review'])
        replacement = [{'page_id': 'new', 'project_page': 'project-page', 'code': 'Ep01', 't6_id': ids['one']}]
        self.assertEqual(self.store.migrate(replacement, actor='alice')['entries'][0]['action'], 'review')
        self.registry.bind_entity('notion', 'new', T6Id.parse(ids['one'], EntityKind.DELIVERABLE), actor='alice')
        plan = self.store.migrate(replacement, actor='alice')
        self.store.migrate(replacement, actor='alice', apply=True, digest=plan['digest'])
        new, old = self.store.get('new'), self.store.get('one')
        assert new is not None and old is not None
        self.assertEqual(new['id'], old['id'])
        self.assertEqual({b['page_id']: b['active'] for b in old['bindings']}, {'one': False, 'new': True})

    def test_rebind_requires_confirmation_preserves_legacy_and_replays(self) -> None:
        rows = [{'page_id': 'old', 'project_page': 'project-page', 'code': 'Ep01'}]
        plan = self.store.migrate(rows, actor='alice')
        result = self.store.migrate(rows, actor='alice', apply=True, digest=plan['digest'])
        identifier = result['entries'][0]['t6_id']
        values = {'deliverable_id': identifier, 'page_id': 'replacement'}
        preview = self.store.rebind(values, actor='alice', idempotency_key='rebind')
        assert isinstance(preview, Preview)
        self.assertIsNone(self.store.get('replacement'))
        done = self.store.rebind(values, actor='alice', idempotency_key='rebind', confirm=preview.token)
        assert isinstance(done, Done)
        found = self.store.get('old')
        assert found is not None
        self.assertEqual(self.store.get('replacement'), found)
        self.assertEqual({b['page_id']: b['active'] for b in found['bindings']}, {'old': False, 'replacement': True})
        replay = self.store.rebind(values, actor='alice', idempotency_key='rebind')
        assert isinstance(replay, Preview)
        self.assertEqual(done, self.store.rebind(values, actor='alice', idempotency_key='rebind', confirm=replay.token))
        self.assertEqual(self.registry.activity(self.project)[0].subject_id, identifier)

    def test_rebind_refuses_conflicts_and_changed_preview(self) -> None:
        from t6.verbs import Refused
        rows = [{'page_id': 'a', 'project_page': 'project-page', 'code': 'Ep01'},
                {'page_id': 'b', 'project_page': 'project-page', 'code': 'Ep02'}]
        preview = self.store.migrate(rows, actor='alice')
        result = self.store.migrate(rows, actor='alice', apply=True, digest=preview['digest'])
        identifier = result['entries'][0]['t6_id']
        refused = self.store.rebind({'deliverable_id': identifier, 'page_id': 'b'}, actor='alice')
        self.assertIsInstance(refused, Refused)
        values = {'deliverable_id': identifier, 'page_id': 'c'}
        preview_write = self.store.rebind(values, actor='alice', idempotency_key='stale')
        assert isinstance(preview_write, Preview)
        self.registry.bind_entity('notion', 'd', T6Id.parse(identifier, EntityKind.DELIVERABLE), actor='alice')
        outcome = self.store.rebind(values, actor='alice', idempotency_key='stale', confirm=preview_write.token)
        assert isinstance(outcome, Refused)
        self.assertEqual(outcome.rule, 'binding_changed')

    def test_delay_and_miss_are_distinct_audited_facts(self) -> None:
        from t6.verbs import Refused
        today = datetime.fromtimestamp(self.registry._clock(), MANILA).date()
        old = (today + timedelta(days=1)).isoformat()
        new = (today + timedelta(days=3)).isoformat()
        plan = self.store.define({'project': self.project, 'code': 'EP', 'due': old}, actor='alice', idempotency_key='define')
        done = self.store.define({'project': self.project, 'code': 'EP', 'due': old}, actor='alice', idempotency_key='define', confirm=plan.token)
        identifier = done.result['deliverable_id']
        values = {'deliverable': identifier, 'field': 'due', 'date': new, 'reason': 'client request'}
        preview = self.store.schedule_write('delay', values, actor='alice', idempotency_key='delay')
        result = self.store.schedule_write('delay', values, actor='alice', idempotency_key='delay', confirm=preview.token)
        self.assertIsInstance(result, Done)
        self.assertEqual(self.store.get(identifier)['due'], new)
        self.assertIsInstance(self.store.schedule_write('miss', {'deliverable': identifier, 'reason': 'late'}, actor='alice'), Refused)
        replay = self.store.schedule_write('delay', values, actor='alice', idempotency_key='delay')
        self.assertEqual(result, self.store.schedule_write('delay', values, actor='alice', idempotency_key='delay', confirm=replay.token))
        with self.registry._transaction() as db:
            self.assertEqual(db.execute('SELECT old_date, new_date, reason FROM deliverable_date_moves').fetchall(), [(old, new, 'client request')])
            self.assertEqual(db.execute('SELECT count(*) FROM deliverable_misses').fetchone()[0], 0)
        self.assertEqual(self.registry.activity(self.project)[0].action, 'deliverable_delay')

    def test_miss_and_delivery_between_preview_and_confirm(self) -> None:
        from t6.verbs import Refused
        values = {'project': self.project, 'code': 'EP', 'due': '2000-01-01'}
        preview = self.store.define(values, actor='alice', idempotency_key='define')
        identifier = self.store.define(values, actor='alice', idempotency_key='define', confirm=preview.token).result['deliverable_id']
        miss = {'deliverable': identifier, 'reason': 'late'}
        preview = self.store.schedule_write('miss', miss, actor='alice', idempotency_key='miss')
        result = self.store.schedule_write('miss', miss, actor='alice', idempotency_key='miss', confirm=preview.token)
        self.assertIsInstance(result, Done)
        self.assertEqual(self.registry.activity(self.project)[0].action, 'deliverable_miss')
        stale = self.store.schedule_write('miss', miss, actor='alice', idempotency_key='another')
        milestone = {'deliverable': identifier, 'milestone': 'delivered'}
        mp = self.store.record_milestone(milestone, actor='alice', idempotency_key='milestone')
        self.store.record_milestone(milestone, actor='alice', idempotency_key='milestone', confirm=mp.token)
        self.assertIsInstance(self.store.schedule_write('miss', miss, actor='alice', idempotency_key='another', confirm=stale.token), Refused)
        self.assertIsInstance(self.store.schedule_write('miss', miss, actor='alice'), Refused)
