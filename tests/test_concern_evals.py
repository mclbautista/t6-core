"""Offline evals exercise the real masked LLM service and Concern gates."""
import json
import unittest
from datetime import date

from t6.concern_scan import ConcernScanner
from t6.concern import ConcernStore
from t6.concern_eval_cases import CASES
from t6.concern_task import classify
from t6.llm.evals import Attempt, RunRecord, run_evals
from t6.llm.testing import FakeProvider
from t6.preview import PreviewTokens
from t6.privacy import Registry
from t6.server.llm import LLMService
from t6.registry import Registered
from t6.ids import EntityKind, T6Id
from test_person import MemoryRegistry


class ConcernEvalTests(unittest.TestCase):
    def test_offline_cases(self) -> None:
        def evaluate(case):  # type: ignore[no-untyped-def]
            registry = MemoryRegistry()
            self.addCleanup(registry.db.close)
            registry._clock = lambda: 1800000000.0
            registry._new_id = lambda: str(T6Id.mint(EntityKind.PROJECT))
            project = registry.register(actor='setup', idempotency_key='setup', notion_page_id='project')
            assert isinstance(project, Registered)
            with registry._transaction() as db:
                db.execute("INSERT INTO persons VALUES ('coordinator', 'staff', '[]', '[]', NULL, 0, 1)")
            store = ConcernStore(registry, PreviewTokens('secret'))
            ConcernScanner(store).process_notes(project.project.id, 'coordinator', [{'text': 'Waiting on client for KDM', 'as_of': '2026-11-01'}],
                lambda _: {'choice': 'new', 'quote': 'Waiting on client for KDM', 'waiting_on': 'client'})
            concern = ConcernScanner(store).changes(project.project.id, '2020-01-01')[0]['concern_id']
            data = case.masked_input
            assert isinstance(data, dict)
            answer = dict(data['answer'])
            if answer.get('concern_id') == 'offered':
                answer['concern_id'] = concern
            provider = FakeProvider().reply(json.dumps(answer))
            service = LLMService({'fake': provider}, 'fake', Registry())
            ConcernScanner(store).process_notes(project.project.id, 'coordinator', [{'text': data['line'], 'as_of': data['as_of']}],
                lambda payload: classify(service, payload, actor='reports', registry=registry))
            changes = ConcernScanner(store).changes(project.project.id, '2020-01-01')
            action = changes[-1]['action'] if len(changes) > 1 else 'ignored'
            actual = {'concern_auto_raise': 'new', 'concern_auto_continue': 'continues', 'concern_auto_announce': 'settles'}.get(action, 'ignored')
            expected = data['expected']
            if expected == 'nothing':
                expected = 'ignored'
            return Attempt(actual == expected, f'{actual} expected {expected}')

        record = RunRecord(model_id='fake', provider='fake', prompt_hash='offline', schema_hash='offline', t6_version='test', date=date(2026, 11, 15), results=[])
        result = run_evals(CASES, record=record, evaluate=lambda *_: self.fail('live evaluator called'), provider=FakeProvider(), cost_cap=0, offline=True, offline_evaluate=evaluate)
        self.assertEqual(len(result.results), len(CASES))
        self.assertTrue(all(item.passed for item in result.results), result.results)
