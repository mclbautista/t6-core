import json
import random
import tempfile
import unittest
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter
from t6.llm.evals import Case, CaseResult, LiveEvalConfig, RunRecord, run_live_evals
from t6.llm.testing import FakeProvider
from t6.person import People
from t6.preview import PreviewTokens
from t6.privacy import Registry
from t6.registry import SqliteProjectRegistry
from t6.server.app import Request
from t6.server.llm import LLMService
from t6.server.verbs import VerbAPI
from t6.verbs import Outcome


def case(i: int, real: bool = False) -> Case:
    return Case.model_validate({'id': str(i), 'task': 'test', 'masked_input': str(i),
        'expected_properties': ['ok'], 'grader': 'code', 'provenance':
        {'kind': 'real', 'reviewer': 'A', 'date': '2026-11-14'} if real else {'kind': 'synthetic'}})


class LiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.registry = SqliteProjectRegistry(self.tmp.name + '/db')
        self.fake = FakeProvider()
        people = People(self.registry, PreviewTokens('test'), {'reports': frozenset({'llm'})})
        self.api = VerbAPI(people, None, {'secret': 'reports'}, lambda *args: None,
            llm_service=LLMService({'fake': self.fake}, 'fake', Registry(), require_zdr=False))
        class Client:
            def llm_run(inner, inputs: Any) -> Any:
                response = self.api(Request('POST', '/v1/llm/run', {'Authorization': 'Bearer secret'},
                                            inputs.model_dump_json().encode()))
                return TypeAdapter(Outcome).validate_python(response.body)
        self.client = Client()
        self.config = LiveEvalConfig('test', 'Do {input}', {'type': 'object'}, 'names', 100, 0.1, 0.1)

    def run_live(self, cases: list[Case], **kwargs: Any) -> RunRecord:
        return run_live_evals(cases, config=kwargs.pop('config', self.config), client=self.client,
            grade=lambda c, o: CaseResult(case_id=c.id, passed=isinstance(o, dict) and o.get('ok') is True, reason='checked'),
            t6_version='test', run_date=date(2026, 11, 14), **kwargs)

    def test_real_cases_review_and_api_metadata(self) -> None:
        for i in range(24):
            self.fake.reply(json.dumps({'ok': i // 3 != 0}), prompt_tokens=1, completion_tokens=1)
        path = Path(self.tmp.name) / 'real.json'
        path.write_text(json.dumps([case(i, True).model_dump(mode='json') for i in range(8)]))
        from dataclasses import replace
        previous = RunRecord(model_id='x', provider='x', prompt_hash='x', schema_hash='x', t6_version='x',
            date=date(2026, 1, 1), results=[CaseResult(case_id='1', passed=False, reason='old')], status='failed')
        record = self.run_live([], config=replace(self.config, real_cases_path=path), history=[previous], rng=random.Random(1))
        self.assertEqual((record.model_id, record.provider), ('fake-model', 'fake'))
        self.assertEqual(len(record.prompt_hash), 64)
        self.assertEqual(len(record.schema_hash), 64)
        self.assertEqual(len(record.human_review), 7)
        self.assertTrue({'0', '1'} <= set(record.human_review))
        self.assertTrue(all(call.zdr for call in self.fake.calls))
        record = record.model_copy(update={'reviewer': 'Cyril', 'review_date': date(2026, 11, 14)})
        out = Path(self.tmp.name) / 'record.json'
        record.write(out)
        self.assertEqual(RunRecord.read(out).reviewer, 'Cyril')
        self.assertEqual(RunRecord.read(out).review_date, date(2026, 11, 14))

    def test_cap_stops_calls(self) -> None:
        from dataclasses import replace
        self.fake.reply('{"ok": true}', prompt_tokens=1, completion_tokens=1)
        result = self.run_live([case(0), case(1)], config=replace(self.config, cost_cap=0.2))
        self.assertEqual(result.status, 'incomplete')
        self.assertTrue(result.cost_cap_reached)
        self.assertEqual(len(self.fake.calls), 1)

    def test_non_zdr_is_refused_even_when_server_policy_is_relaxed(self) -> None:
        self.api.llm_service = LLMService({'fake': FakeProvider(retention='unknown')}, 'fake', Registry(), require_zdr=False)
        with self.assertRaisesRegex(RuntimeError, 'refused'):
            self.run_live([case(0)])

    def test_invalid_output_is_costed_and_reviewed(self) -> None:
        self.fake.reply('not json with secret', prompt_tokens=2, completion_tokens=1)
        self.fake.reply('{"wrong": true}', prompt_tokens=2, completion_tokens=1)
        self.fake.reply('{"ok": true}', prompt_tokens=2, completion_tokens=1)
        from dataclasses import replace
        config = replace(self.config, output_schema={'type': 'object', 'required': ['ok']})
        result = self.run_live([case(0)], config=config)
        self.assertEqual(result.status, 'failed')
        self.assertAlmostEqual(result.cost, 0.9)
        self.assertEqual(result.human_review, ['0'])
        self.assertNotIn('secret', result.model_dump_json())
        self.assertEqual(len(self.fake.calls), 3)

    def test_missing_usage_stops_after_first_call(self) -> None:
        from t6.llm.transport import Completion
        from t6.llm.result import Tokens
        self.fake._script.append(Completion('{"ok": true}', 'fake-model', Tokens(None, 1)))
        with self.assertRaisesRegex(ValueError, 'token counts'):
            self.run_live([case(0), case(1)])
        self.assertEqual(len(self.fake.calls), 1)
