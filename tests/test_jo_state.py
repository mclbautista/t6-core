"""The documented JO lifecycle table and its commit boundary."""
import json
import unittest

from t6.ids import EntityKind, T6Id
from t6.job_order import JobOrderStore
from t6.preview import PreviewTokens
from t6.registry import Registered
from t6.verbs import Done, Preview, Refused
from test_person import MemoryRegistry


class JoStateTests(unittest.TestCase):
    def setUp(self):
        self.registry = MemoryRegistry()
        self.addCleanup(self.registry.db.close)
        self.registry._new_id = lambda: str(T6Id.mint(EntityKind.PROJECT))
        project = self.registry.register(actor='setup', idempotency_key='setup', notion_page_id='project')
        assert isinstance(project, Registered)
        self.project = project.project.id
        self.store = JobOrderStore(self.registry, PreviewTokens('secret'))

    def test_transition_table_and_projection_intents(self):
        allowed = {
            'submit': {'Needs Intake Review': 'In progress', 'Pending': 'In progress'},
            'close': {'In progress': 'Done', 'Needs Completion Review': 'Done'},
            'block': {'In progress': 'In progress', 'Needs Completion Review': 'Needs Completion Review'},
            'unblock': {'In progress': 'In progress', 'Needs Completion Review': 'Needs Completion Review'},
            'delay': {s: s for s in ('Needs Intake Review', 'Pending', 'In progress', 'Needs Completion Review')},
            'cancel': {s: 'Cancelled' for s in ('Needs Intake Review', 'Pending', 'In progress', 'Needs Completion Review')},
        }
        statuses = ('Needs Intake Review', 'Pending', 'In progress', 'Needs Completion Review', 'Done', 'Cancelled')
        for verb, targets in allowed.items():
            for status in statuses:
                for blocked in (False, True):
                    with self.subTest(verb=verb, status=status, blocked=blocked):
                        key = f'{verb}-{status}-{blocked}'
                        with self.registry._transaction() as db:
                            db.execute('INSERT INTO job_orders VALUES (?, ?, NULL, NULL, ?)', (key, self.project, '{}'))
                            db.execute('INSERT INTO jo_state VALUES (?, ?, ?, NULL, NULL, NULL)', (key, status, int(blocked)))
                        values = {'jo': key, 'reason': 'reason', 'until': '2026-12-01'}
                        preview = self.store.write(verb, values, actor='alice', idempotency_key=key)
                        legal = status in targets and not (verb == 'block' and blocked or verb == 'unblock' and not blocked or verb in ('submit', 'close') and blocked)
                        if not legal:
                            self.assertIsInstance(preview, Refused)
                            self.assertIn(preview.rule, ('jo_transition', 'jo_blocked'))
                            continue
                        self.assertIsInstance(preview, Preview)
                        done = self.store.write(verb, values, actor='alice', idempotency_key=key, confirm=preview.token)
                        self.assertIsInstance(done, Done)
                        record = self.store.get(key)
                        self.assertEqual(record['state']['status'], targets[status])
                        self.assertEqual(len(record['history']), 1)
                        self.assertEqual(self.registry.activity(self.project)[0].action, 'jo_' + verb)
                        with self.registry._transaction() as db:
                            rows = db.execute('SELECT j.payload FROM jo_sync_intents j JOIN sync_intents s ON s.id = j.intent_id WHERE s.project_id = ?', (key,)).fetchall()
                        self.assertEqual([json.loads(row[0]) for row in rows], [{'status': targets[status]}])
                        retry = self.store.write(verb, values, actor='alice', idempotency_key=key)
                        # A replay can be confirmed without a second transition.
                        self.assertIsInstance(retry, Preview)
                        self.assertIsInstance(self.store.write(verb, values, actor='alice', idempotency_key=key, confirm=retry.token), Done)
                        self.assertEqual(len(self.store.get(key)['history']), 1)

    def test_required_reason_and_date(self):
        with self.registry._transaction() as db:
            db.execute('INSERT INTO job_orders VALUES (?, ?, NULL, NULL, ?)', ('jo', self.project, '{"status":"In progress"}'))
        for verb in ('block', 'delay', 'cancel'):
            result = self.store.write(verb, {'jo': 'jo', 'reason': '  ', 'until': '2026-12-01'}, actor='alice')
            self.assertEqual(result.rule, 'reason')
        result = self.store.write('delay', {'jo': 'jo', 'reason': 'late', 'until': 'tomorrow'}, actor='alice')
        self.assertEqual(result.rule, 'until')

    def test_changed_state_invalidates_preview(self):
        with self.registry._transaction() as db:
            db.execute('INSERT INTO job_orders VALUES (?, ?, NULL, NULL, ?)', ('jo', self.project, '{}'))
        values = {'jo': 'jo', 'reason': 'late', 'until': '2026-12-01'}
        first = self.store.write('delay', values, actor='alice', idempotency_key='first')
        second = self.store.write('delay', values, actor='alice', idempotency_key='second')
        self.assertIsInstance(first, Preview)
        self.assertIsInstance(second, Preview)
        self.assertIsInstance(self.store.write('delay', values, actor='alice', idempotency_key='first', confirm=first.token), Done)
        result = self.store.write('delay', values, actor='alice', idempotency_key='second', confirm=second.token)
        self.assertEqual(result.rule, 'jo_state_changed')
