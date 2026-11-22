import io
import json
import os
import tempfile
import unittest
import urllib.error
from typing import Any
from unittest import mock

from t6.llm.config import ConfigError
from t6.llm.decisions import DECISIONS_URL, JEV_MODEL, OpenRouterDecisions
from t6.llm.testing import FakeProvider
from t6.person import People
from t6.preview import PreviewTokens
from t6.privacy import Person, Registry
from t6.privacy.masking import Leak, Session
from t6.registry import SqliteProjectRegistry
from t6.server.app import Request
from t6.server.llm import LLMService, service_from_env
from t6.server.verbs import VerbAPI
from test_server_llm_decide import Send, answer


def one_answer(match: float, maybe: float, no: float) -> dict[str, Any]:
    choice = max((match, 'match'), (maybe, 'maybe'), (no, 'no'))[1]
    return {'model': 'jev-test', 'usage': {'input_tokens': 11, 'output_tokens': 4}, 'answers': {'c0': answer(match, maybe, no, choice)}}


class LLMFilterTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.registry = SqliteProjectRegistry(tmp.name + '/registry')
        self.send = Send(one_answer(0.8, 0.1, 0.1))
        self.names = Registry(people=[Person('Jane Doe')])
        self.body: dict[str, Any] = {'rule': 'job_order', 'text': 'Please cut a teaser by Friday, thanks Jane Doe'}
        self.permissions = {'reports': frozenset({'llm'})}
        self.make(self.transport())

    def transport(self, retention: Any = 'unknown') -> OpenRouterDecisions:
        return OpenRouterDecisions(api_key='k', retention=retention, send=self.send)

    def make(self, decisions: OpenRouterDecisions | None, **options: Any) -> None:
        options.setdefault('min_retention_by_provider', {'decisions': 'unknown'})
        self.service = LLMService({'fake': FakeProvider()}, 'fake', self.names, decisions=decisions, **options)
        people = People(self.registry, PreviewTokens('test'), self.permissions)
        self.api = VerbAPI(people, None, {'secret': 'reports'}, lambda *args: None, llm_service=self.service)

    def call(self) -> Any:
        return self.api(Request('POST', '/v1/llm/filter', {'Authorization': 'Bearer secret'}, json.dumps(self.body).encode())).body

    def audit(self) -> list[Any]:
        with self.registry._transaction() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT after FROM audit WHERE action = 'llm_filter' ORDER BY rowid")]

    def verdict(self, match: float, maybe: float, no: float) -> Any:
        self.send.body = one_answer(match, maybe, no)
        return self.call()

    def test_request_is_one_masked_question_with_the_rule_texts(self) -> None:
        self.body['reply_to_text'] = 'Can someone do it? Jane Doe?'
        result = self.call()
        self.assertEqual(result['outcome'], 'done', result)
        ((url, sent, timeout),) = self.send.requests
        self.assertEqual((url, timeout, sent['model']), (DECISIONS_URL, 60, JEV_MODEL))
        self.assertEqual(list(sent['questions']), ['c0'])
        self.assertNotIn('Jane Doe', json.dumps(sent))
        self.assertIn('Please cut a teaser by Friday', json.dumps(sent['state']))
        self.assertIn('Can someone do it?', json.dumps(sent['state']))
        self.assertEqual(set(sent['questions']['c0']['criteria']), {'match', 'maybe', 'no'})
        out = result['result']
        self.assertEqual(out['verdict'], 'pass')
        self.assertEqual(out['probabilities'], {'match': 0.8, 'maybe': 0.1, 'no': 0.1})
        self.assertEqual((out['rule'], out['model'], out['provider'], out['zdr']), ('job_order', 'jev-test', 'decisions', False))
        self.assertEqual(out['tokens'], {'prompt': 11, 'completion': 4, 'total': 15})

    def test_reply_is_left_out_when_absent(self) -> None:
        self.call()
        self.assertEqual(len(self.send.requests[0][1]['state']), 1)

    def test_each_verdict_from_the_thresholds(self) -> None:
        self.assertEqual(self.verdict(0.5, 0.3, 0.2)['result']['verdict'], 'pass')
        self.assertEqual(self.verdict(0.05, 0.05, 0.9)['result']['verdict'], 'drop')
        self.assertEqual(self.verdict(0.2, 0.3, 0.5)['result']['verdict'], 'unsure')
        self.assertEqual(self.verdict(0.49, 0.01, 0.5)['result']['verdict'], 'unsure')

    def test_config_override(self) -> None:
        self.make(self.transport(), filters={'job_order': {'pass': 0.9, 'drop': 0.5}})
        self.assertEqual(self.verdict(0.8, 0.1, 0.1)['result']['verdict'], 'unsure')
        self.assertEqual(self.verdict(0.2, 0.2, 0.6)['result']['verdict'], 'drop')
        self.make(self.transport(), filters={'job_order': {'pass': 0.7}})
        self.assertEqual(self.verdict(0.8, 0.1, 0.1)['result']['verdict'], 'pass')
        self.assertEqual(self.verdict(0.05, 0.05, 0.9)['result']['verdict'], 'drop')

    def test_bad_filters_config(self) -> None:
        for bad in ({'nope': {'pass': 0.5}}, {'job_order': {'pass': 1.5}}, {'job_order': {'drop': -0.1}},
                    {'job_order': {'pass': 'x'}}, {'job_order': {'keep': 0.5}}, {'job_order': {'pass': True}}, {'job_order': 0.5}):
            with self.assertRaises(ConfigError, msg=bad):
                LLMService({'fake': FakeProvider()}, 'fake', self.names, filters=bad)

    def test_unknown_rule_is_input(self) -> None:
        self.body['rule'] = 'nope'
        result = self.call()
        self.assertEqual((result['outcome'], result['rule']), ('refused', 'input'))
        self.assertEqual(self.send.requests, [])
        self.assertEqual(self.audit(), [{'outcome': 'refused'}])

    def test_registered_name_is_masked_in_text_and_reply(self) -> None:
        self.body['reply_to_text'] = 'JANE DOE?'
        self.assertEqual(self.call()['outcome'], 'done')
        self.assertNotIn('ane', json.dumps(self.send.requests[0][1]).replace('Jane Doe', ''))
        self.assertNotIn('Jane', json.dumps(self.send.requests[0][1]))

    def test_credentials_are_withheld(self) -> None:
        self.body['text'] = 'password: hunter2-secret'
        self.assertEqual(self.call()['outcome'], 'done')
        self.assertNotIn('hunter2-secret', json.dumps(self.send.requests[0][1]))

    def test_leak_is_refused(self) -> None:
        with mock.patch.object(Session, 'leaks', return_value=[Leak('message', 'Jane Doe')]):
            result = self.call()
        self.assertEqual((result['outcome'], result['rule']), ('refused', 'masking'))
        self.assertEqual(self.send.requests, [])

    def test_size_limit(self) -> None:
        self.make(self.transport(), max_bytes=50)
        self.assertEqual(self.call()['rule'], 'size')
        self.make(self.transport(), max_bytes=100_000)
        self.body['text'] = 'x' * 100_001
        self.assertEqual(self.call()['rule'], 'size')
        self.assertEqual(self.send.requests, [])

    def test_failure_classes(self) -> None:
        self.send.body = {'model': 'jev-test', 'usage': {'input_tokens': 7, 'output_tokens': 2}, 'answers': {}}
        result = self.call()
        self.assertEqual((result['rule'], result['failure']), ('provider', 'schema_violation'))
        self.assertIn('jev-test', result['reason'])
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
        self.assertEqual(self.call()['rule'], 'config')

    def test_retention_refusal_when_decisions_has_no_entry(self) -> None:
        self.make(self.transport(), min_retention_by_provider={})
        result = self.call()
        self.assertEqual((result['rule'], result['failure']), ('policy', 'policy'))
        self.assertEqual(self.send.requests, [])
        self.make(self.transport('zdr_enforced'), min_retention_by_provider={})
        result = self.call()
        self.assertEqual((result['outcome'], result['result']['zdr']), ('done', True))

    def test_permission(self) -> None:
        self.api.people.service_permissions = {}
        self.assertEqual(self.call()['rule'], 'permission')
        self.assertEqual(self.send.requests, [])

    def test_audit_row_holds_no_text_and_no_probabilities(self) -> None:
        self.body['reply_to_text'] = 'a reply'
        self.call()
        self.send.error = TimeoutError('slow')
        self.call()
        rows = self.audit()
        self.assertEqual(rows[0], {'outcome': 'done', 'provider': 'decisions', 'zdr': False, 'rule': 'job_order', 'verdict': 'pass'})
        self.assertEqual(rows[1], {'outcome': 'refused', 'provider': 'decisions', 'zdr': False, 'rule': 'job_order'})
        with self.registry._transaction() as db:
            raw = ' '.join(str(row) for row in db.execute("SELECT before, after FROM audit WHERE action = 'llm_filter'"))
        for text in ('teaser', 'a reply', '0.8'):
            self.assertNotIn(text, raw)


class FilterConfigTests(unittest.TestCase):
    def build(self, extra: dict[str, Any]) -> LLMService | None:
        config = {'llm': {'default': 'chat', 'providers': {'chat': {'base_url': 'https://llm.example', 'model': 'm',
                  'retention': 'zdr_enforced', 'api_key_env': 'CHAT_KEY'}}}, 'privacy_registry': {}, **extra}
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'llm.json')
            with open(path, 'w', encoding='utf-8') as file:
                json.dump(config, file)
            return service_from_env({'T6_LLM_CONFIG': path, 'CHAT_KEY': 'c'})

    def test_filters_load_and_bad_ones_stop_the_server(self) -> None:
        self.assertIsNotNone(self.build({'filters': {'job_order': {'pass': 0.6, 'drop': 0.8}}}))
        with self.assertRaises(ConfigError):
            self.build({'filters': {'unknown_rule': {'pass': 0.6}}})
        with self.assertRaises(ConfigError):
            self.build({'filters': {'job_order': {'drop': 2}}})
        with self.assertRaises(ConfigError):
            self.build({'filters': ['job_order']})


if __name__ == '__main__':
    unittest.main()
