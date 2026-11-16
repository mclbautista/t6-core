import io
import json
import os
import tempfile
import unittest
import urllib.error
from typing import Any

from t6.llm.decisions import DECISIONS_URL, JEV_MODEL, OpenRouterDecisions
from t6.llm.config import ConfigError
from t6.llm.testing import FakeProvider
from t6.person import People
from t6.preview import PreviewTokens
from t6.privacy import Person, Registry
from t6.registry import SqliteProjectRegistry
from t6.server.app import Request
from t6.server.llm import LLMService, service_from_env
from t6.server.verbs import VerbAPI


def answer(match: float, maybe: float, no: float, choice: str) -> dict[str, Any]:
    return {'type': 'choice', 'choice': choice, 'probabilities': {'match': match, 'maybe': maybe, 'no': no}}


class Send:
    """The fake `send` of OpenRouterDecisions: records requests, replies with a body or raises."""

    def __init__(self, body: Any = None, error: Exception | None = None) -> None:
        self.body, self.error = body, error
        self.requests: list[tuple[str, dict[str, Any], float]] = []

    def __call__(self, url: str, headers: dict[str, str], payload: bytes, timeout: float) -> bytes:
        self.requests.append((url, json.loads(payload), timeout))
        if self.error:
            raise self.error
        return json.dumps(self.body).encode()


def two_answers() -> dict[str, Any]:
    return {'model': 'jev-test', 'usage': {'input_tokens': 11, 'output_tokens': 4},
            'answers': {'c0': answer(0.9, 0.06, 0.04, 'match'), 'c1': answer(0.1, 0.2, 0.7, 'no')}}


class LLMDecideTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.registry = SqliteProjectRegistry(tmp.name + '/registry')
        self.send = Send(two_answers())
        self.names = Registry(people=[Person('Jane Doe')])
        self.body: dict[str, Any] = {
            'task_name': 'deliverable-match', 'state': {'project': 'Harbour Lights', 'job_order': 'Jane Doe asks for a teaser'},
            'question': 'Is the Job Order about {candidate}?', 'criteria': {'match': 'Same item', 'maybe': 'Unsure', 'no': 'Another item'},
            'candidates': ['Teaser 30s', 'Jane Doe cut']}
        self.permissions = {'reports': frozenset({'llm'})}
        self.make(self.transport())

    def transport(self, retention: Any = 'unknown', send: Send | None = None) -> OpenRouterDecisions:
        return OpenRouterDecisions(api_key='k', retention=retention, send=send or self.send)

    def make(self, decisions: OpenRouterDecisions | None, **options: Any) -> None:
        options.setdefault('min_retention_by_provider', {'decisions': 'unknown'})
        self.service = LLMService({'fake': FakeProvider()}, 'fake', self.names, decisions=decisions, **options)
        people = People(self.registry, PreviewTokens('test'), self.permissions)
        self.api = VerbAPI(people, None, {'secret': 'reports'}, lambda *args: None, llm_service=self.service)

    def call(self) -> Any:
        return self.api(Request('POST', '/v1/llm/decide', {'Authorization': 'Bearer secret'}, json.dumps(self.body).encode())).body

    def test_request_is_masked_and_keyed_and_answers_follow_candidate_order(self) -> None:
        result = self.call()
        self.assertEqual(result['outcome'], 'done', result)
        ((url, sent, timeout),) = self.send.requests
        self.assertEqual((url, timeout, sent['model']), (DECISIONS_URL, 60, JEV_MODEL))
        self.assertEqual(list(sent['questions']), ['c0', 'c1'])
        self.assertNotIn('Jane Doe', json.dumps(sent))
        self.assertIn('Harbour Lights', sent['state']['project'])
        self.assertEqual(sent['questions']['c0']['instructions'], 'Is the Job Order about Teaser 30s?')
        self.assertEqual(sent['questions']['c0']['criteria'], self.body['criteria'])
        self.assertNotIn('{candidate}', json.dumps(sent))
        out = result['result']
        self.assertEqual([a['index'] for a in out['answers']], [0, 1])
        self.assertEqual(out['answers'][0]['choice'], 'match')
        self.assertEqual(out['answers'][1]['probabilities'], {'match': 0.1, 'maybe': 0.2, 'no': 0.7})
        self.assertEqual((out['model'], out['provider'], out['zdr']), ('jev-test', 'decisions', False))
        self.assertEqual(out['tokens'], {'prompt': 11, 'completion': 4, 'total': 15})

    def test_credentials_are_withheld(self) -> None:
        self.body['state']['job_order'] = 'password: hunter2-secret'
        self.assertEqual(self.call()['outcome'], 'done')
        self.assertNotIn('hunter2-secret', json.dumps(self.send.requests[0][1]))

    def test_registered_name_in_question_or_criteria_is_refused(self) -> None:
        self.body['question'] = 'Is Jane Doe in {candidate}?'
        result = self.call()
        self.assertEqual((result['outcome'], result['rule']), ('refused', 'masking'))
        self.body['question'] = 'Is it {candidate}?'
        self.body['criteria']['match'] = 'Jane Doe wrote it'
        self.assertEqual(self.call()['rule'], 'masking')
        self.assertEqual(self.send.requests, [])

    def test_question_needs_one_marker_and_criteria_three_keys(self) -> None:
        self.body['question'] = 'No marker'
        self.assertEqual(self.call()['rule'], 'template')
        self.body['question'] = '{candidate} or {candidate}'
        self.assertEqual(self.call()['rule'], 'template')
        self.body['question'] = '{candidate}?'
        self.body['criteria'] = {'match': 'a', 'no': 'b'}
        self.assertEqual(self.call()['rule'], 'criteria')
        self.assertEqual(self.send.requests, [])

    def test_size_limit_and_empty_candidates(self) -> None:
        self.make(self.transport(), max_bytes=50)
        self.assertEqual(self.call()['rule'], 'size')
        self.make(self.transport())
        self.body['candidates'] = []
        self.assertEqual(self.call()['rule'], 'candidates')
        self.assertEqual(self.send.requests, [])

    def test_malformed_reply_is_schema_violation_with_model_and_tokens(self) -> None:
        self.send.body = {'model': 'jev-test', 'usage': {'input_tokens': 7, 'output_tokens': 2}, 'answers': {'c0': answer(0.9, 0.06, 0.04, 'match')}}
        result = self.call()
        self.assertEqual((result['outcome'], result['rule'], result['failure']), ('refused', 'provider', 'schema_violation'))
        self.assertIn('jev-test', result['reason'])
        self.assertIn('7', result['reason'])
        self.assertIn('2', result['reason'])

    def test_rate_limit_and_timeout_and_error(self) -> None:
        self.send.error = urllib.error.HTTPError('u', 429, 'Too Many', {'Retry-After': '7'}, io.BytesIO(b''))  # type: ignore[arg-type]
        result = self.call()
        self.assertEqual((result['rule'], result['failure'], result['retry_after']), ('provider', 'api_error', 7.0))
        self.send.error = TimeoutError('slow')
        result = self.call()
        self.assertEqual((result['rule'], result['failure']), ('provider', 'timeout'))
        self.assertNotIn('retry_after', result)
        self.send.error = urllib.error.HTTPError('u', 500, 'Boom', {}, io.BytesIO(b''))  # type: ignore[arg-type]
        self.assertEqual(self.call()['failure'], 'api_error')

    def test_no_decisions_provider_is_config(self) -> None:
        self.make(None, min_retention_by_provider={})
        result = self.call()
        self.assertEqual((result['outcome'], result['rule']), ('refused', 'config'))

    def test_retention_floor_comes_from_the_provider_map(self) -> None:
        self.make(self.transport(), min_retention_by_provider={})
        result = self.call()
        self.assertEqual((result['rule'], result['failure']), ('policy', 'policy'))
        self.assertEqual(self.send.requests, [])
        self.make(self.transport('zdr_enforced'), min_retention_by_provider={})
        result = self.call()
        self.assertEqual((result['outcome'], result['result']['zdr']), ('done', True))
        self.make(self.transport(), min_retention_by_provider={'decisions': 'unknown'})
        self.body['require_zdr'] = True
        self.assertEqual(self.call()['rule'], 'policy')

    def test_floor_for_decisions_needs_a_decisions_provider(self) -> None:
        with self.assertRaises(ValueError):
            LLMService({'fake': FakeProvider()}, 'fake', self.names, min_retention_by_provider={'decisions': 'unknown'})

    def test_run_cannot_pick_the_decisions_provider(self) -> None:
        self.assertNotIn('decisions', self.service.providers)

    def test_permission(self) -> None:
        self.api.people.service_permissions = {}
        self.assertEqual(self.call()['rule'], 'permission')
        self.assertEqual(self.send.requests, [])

    def test_audit_row_holds_outcome_provider_and_zdr_only(self) -> None:
        self.call()
        with self.registry._transaction() as db:
            row = db.execute("SELECT after FROM audit WHERE action = 'llm_decide'").fetchone()
        self.assertEqual(json.loads(row[0]), {'outcome': 'done', 'provider': 'decisions', 'zdr': False})
        self.send.error = TimeoutError('slow')
        self.call()
        with self.registry._transaction() as db:
            rows = db.execute("SELECT after FROM audit WHERE action = 'llm_decide' ORDER BY rowid").fetchall()
        self.assertEqual(json.loads(rows[1][0]), {'outcome': 'refused', 'provider': 'decisions', 'zdr': False})


class DecisionsConfigTests(unittest.TestCase):
    def build(self, extra: dict[str, Any], env: dict[str, str] | None = None) -> LLMService | None:
        config = {'llm': {'default': 'chat', 'providers': {'chat': {'base_url': 'https://llm.example', 'model': 'm',
                  'retention': 'zdr_enforced', 'api_key_env': 'CHAT_KEY'}}}, 'privacy_registry': {}, **extra}
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'llm.json')
            with open(path, 'w', encoding='utf-8') as file:
                json.dump(config, file)
            return service_from_env({'T6_LLM_CONFIG': path, 'CHAT_KEY': 'c', **(env or {})})

    def test_defaults_and_overrides(self) -> None:
        service = self.build({'decisions': {'api_key_env': 'DEC_KEY'}, 'provider_min_retention': {'decisions': 'unknown'}}, {'DEC_KEY': 'd'})
        assert service is not None and service.decisions is not None
        self.assertEqual((service.decisions.model, service.decisions.retention), (JEV_MODEL, 'unknown'))
        service = self.build({'decisions': {'api_key_env': 'DEC_KEY', 'model': 'other', 'url': 'https://d.example/x', 'retention': 'zdr_enforced'}}, {'DEC_KEY': 'd'})
        assert service is not None and service.decisions is not None
        self.assertEqual((service.decisions.model, service.decisions.retention), ('other', 'zdr_enforced'))

    def test_absent_key_means_no_decisions(self) -> None:
        service = self.build({})
        assert service is not None
        self.assertIsNone(service.decisions)

    def test_bad_config_stops_the_server(self) -> None:
        with self.assertRaises(ConfigError):
            self.build({'decisions': {'api_key_env': 'DEC_KEY'}})
        with self.assertRaises(ConfigError):
            self.build({'decisions': {'api_key_env': 'DEC_KEY', 'api_key': 'x'}}, {'DEC_KEY': 'd'})
        with self.assertRaises(ConfigError):
            self.build({'decisions': {'api_key_env': 'DEC_KEY', 'retention': 'maybe'}}, {'DEC_KEY': 'd'})
        with self.assertRaises(ConfigError):
            self.build({'provider_min_retention': {'decisions': 'unknown'}})


if __name__ == '__main__':
    unittest.main()
