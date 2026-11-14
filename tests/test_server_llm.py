import json
import unittest
import tempfile
from typing import Any

from t6.llm.testing import FakeProvider
from t6.person import People
from t6.preview import PreviewTokens
from t6.privacy import Person, Registry
from t6.registry import SqliteProjectRegistry
from t6.server.app import Request
from t6.server.llm import LLMService
from t6.server.verbs import VerbAPI


class LLMAPITests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.registry = SqliteProjectRegistry(tmp.name + '/registry')
        self.fake = FakeProvider().reply('{"summary":"[person 1] confirmed"}', prompt_tokens=5, completion_tokens=3)
        self.service = LLMService({'fake': self.fake}, 'fake', Registry(people=[Person('Jane Doe')]))
        people = People(self.registry, PreviewTokens('test'), {'reports': frozenset({'llm'})})
        self.api = VerbAPI(people, None, {'secret': 'reports'}, lambda *args: None, llm_service=self.service)
        self.body = {'task_name': 'summary', 'prompt_template': 'Summarize: {input}',
                     'output_schema': {'type': 'object', 'properties': {'summary': {'type': 'string'}}, 'required': ['summary']},
                     'input_text': 'Jane Doe confirmed', 'masking_preset': 'names'}

    def call(self) -> Any:
        return self.api(Request('POST', '/v1/llm/run', {'Authorization': 'Bearer secret'}, json.dumps(self.body).encode())).body

    def test_run_and_audit(self) -> None:
        result = self.call()
        self.assertEqual(result['result']['output'], {'summary': 'Jane Doe confirmed'})
        self.assertEqual(result['result']['model'], 'fake-model')
        self.assertEqual(result['result']['provider'], 'fake')
        self.assertEqual(len(result['result']['prompt_hash']), 64)
        self.assertEqual(len(result['result']['schema_hash']), 64)
        self.assertTrue(self.fake.calls[0].zdr)
        self.assertNotIn('Jane Doe', str(self.fake.calls[0].messages))
        with self.registry._transaction() as db:
            row = db.execute("SELECT * FROM audit WHERE action = 'llm_run'").fetchone()
        self.assertNotIn('Jane Doe', str(row))
        self.assertNotIn('confirmed', str(row))

    def test_permission_and_invalid_output(self) -> None:
        self.api.people.service_permissions = {}
        self.assertEqual(self.call()['rule'], 'permission')
        self.assertEqual(self.fake.calls, [])
        self.api.people.service_permissions = {'reports': frozenset({'llm'})}
        self.fake = FakeProvider().reply('{"summary": 123}')
        self.api.llm_service = LLMService({'fake': self.fake}, 'fake', Registry())
        self.assertFalse(self.call()['result']['output_valid'])

    def test_non_zdr_refused(self) -> None:
        fake = FakeProvider(retention='unknown')
        self.api.llm_service = LLMService({'fake': fake}, 'fake', Registry())
        self.assertEqual(self.call()['rule'], 'policy')
        self.assertEqual(fake.calls, [])

    def test_unresolved_name_refused(self) -> None:
        # Strict intentionally skips short project names; the leak check catches them.
        from t6.privacy import Project
        self.api.llm_service = LLMService({'fake': self.fake}, 'fake', Registry(projects=[Project('Home')]))
        self.body.update(input_text='Home', masking_preset='strict')
        self.assertEqual(self.call()['rule'], 'masking')
        self.assertEqual(self.fake.calls, [])
