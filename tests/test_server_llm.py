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
        self.body: dict[str, Any] = {'task_name': 'summary', 'prompt_template': 'Summarize: {input}',
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

    def test_strict_lets_plain_words_through(self) -> None:
        # Strict leaves a plain-word Project name and a one-character alias on purpose; the leak check follows the preset.
        from t6.privacy import Project
        registry = Registry(people=[Person('Jane Doe', aliases=['J'])], projects=[Project('Home')])
        self.api.llm_service = LLMService({'fake': self.fake}, 'fake', registry)
        self.body.update(input_text='Home with J', masking_preset='strict')
        self.assertEqual(self.call()['outcome'], 'done')
        self.assertIn('Home with J', str(self.fake.calls[0].messages))

    def test_names_preset_still_refuses_a_name_in_the_template(self) -> None:
        self.body.update(prompt_template='Ask Jane Doe: {input}')
        self.assertEqual(self.call()['rule'], 'masking')
        self.assertEqual(self.fake.calls, [])

    def audit(self) -> dict[str, Any]:
        with self.registry._transaction() as db:
            row = db.execute("SELECT after FROM audit WHERE action = 'llm_run' ORDER BY rowid DESC").fetchone()
        return json.loads(row[0])  # type: ignore[no-any-return]

    def serve(self, fake: FakeProvider, **options: Any) -> None:
        self.fake = fake
        self.api.llm_service = LLMService({'fake': fake}, 'fake', Registry(people=[Person('Jane Doe')]), **options)

    def test_examples_become_turns_in_order_and_are_masked(self) -> None:
        self.body['examples'] = [{'input_text': 'Jane Doe asked', 'output': {'summary': 'Jane Doe asked'}}]
        self.assertEqual(self.call()['result']['output'], {'summary': 'Jane Doe confirmed'})
        messages = self.fake.calls[0].messages
        self.assertEqual([m['role'] for m in messages], ['system', 'user', 'assistant', 'user'])
        self.assertEqual(messages[1]['content'], 'Summarize: [person 1] asked')
        self.assertEqual(json.loads(messages[2]['content']), {'summary': '[person 1] asked'})
        self.assertEqual(messages[3]['content'], 'Summarize: [person 1] confirmed')
        self.assertNotIn('Jane', str(messages))

    def test_example_output_must_match_schema(self) -> None:
        self.body['examples'] = [{'input_text': 'x', 'output': {'summary': 5}}]
        self.assertEqual(self.call()['rule'], 'schema')
        self.assertEqual(self.fake.calls, [])

    def test_examples_count_toward_max_bytes(self) -> None:
        self.serve(self.fake, max_bytes=400)
        self.assertEqual(self.call()['result']['output_valid'], True)
        self.fake.reply('{"summary":"a"}')
        self.body['examples'] = [{'input_text': 'y' * 400, 'output': {'summary': 'a'}}]
        self.assertEqual(self.call()['rule'], 'size')

    def test_failure_classes(self) -> None:
        cases = [(FakeProvider().timeout(), 'timeout', None), (FakeProvider().rate_limited(retry_after=2), 'api_error', 2),
                 (FakeProvider().rate_limited(), 'api_error', None), (FakeProvider().api_error(), 'api_error', None)]
        for fake, failure, retry in cases:
            with self.subTest(failure=failure, retry=retry):
                self.serve(fake)
                body = self.call()
                self.assertEqual((body['rule'], body['failure']), ('provider', failure))
                self.assertEqual(body.get('retry_after'), retry)
                self.assertEqual('retry_after' in body, retry is not None)

    def test_output_failure_classes(self) -> None:
        cases = [(None, 'empty'), ('  ', 'empty'), ('nope', 'invalid_json'), ('{"summary": 1}', 'schema_violation'),
                 ('{"summary": "[person 9] said"}', 'unresolved_placeholder')]
        for content, failure in cases:
            with self.subTest(failure=failure, content=content):
                self.serve(FakeProvider().reply(content))
                result = self.call()['result']
                self.assertFalse(result['output_valid'])
                self.assertEqual(result['failure'], failure)
        self.serve(FakeProvider().reply('{"summary": "ok"}'))
        self.assertIsNone(self.call()['result']['failure'])

    def test_policy_and_leak_failures(self) -> None:
        self.serve(FakeProvider(retention='unknown'))
        self.assertEqual(self.call()['failure'], 'policy')
        self.serve(FakeProvider().reply('{"summary": "Jane Doe"}'))
        body = self.call()
        self.assertEqual((body['rule'], body['failure']), ('masking', 'leak'))

    def test_retention_exception_is_per_provider(self) -> None:
        loose = FakeProvider('loose-model', retention='unknown').reply('{"summary": "ok"}')
        strict = FakeProvider('strict-model', retention='unknown').reply('{"summary": "ok"}')
        self.api.llm_service = LLMService({'loose': loose, 'strict': strict}, 'loose', Registry(),
                                          min_retention_by_provider={'loose': 'unknown'})
        result = self.call()['result']
        self.assertEqual((result['provider'], result['zdr']), ('loose', False))
        self.assertFalse(loose.calls[0].zdr)
        self.body['model_hint'] = 'strict-model'
        self.assertEqual(self.call()['rule'], 'policy')
        self.assertEqual(strict.calls, [])

    def test_caller_can_tighten_the_exception(self) -> None:
        loose = FakeProvider(retention='unknown')
        self.api.llm_service = LLMService({'fake': loose}, 'fake', Registry(), min_retention_by_provider={'fake': 'unknown'})
        self.body['require_zdr'] = True
        self.assertEqual(self.call()['rule'], 'policy')
        self.assertEqual(loose.calls, [])

    def test_bad_retention_map_is_refused_at_construction(self) -> None:
        for bad in ({'other': 'unknown'}, {'fake': 'zdr'}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                LLMService({'fake': self.fake}, 'fake', Registry(), min_retention_by_provider=bad)  # type: ignore[arg-type]

    def test_audit_row_has_provider_and_zdr_and_no_text(self) -> None:
        self.call()
        audit = self.audit()
        self.assertEqual(audit, {'outcome': 'done', 'provider': 'fake', 'zdr': True, 'allow_projects': 0})

    def test_other_refusals_have_no_failure_keys(self) -> None:
        self.api.people.service_permissions = {}
        body = self.call()
        self.assertEqual(body['rule'], 'permission')
        self.assertNotIn('failure', body)
        self.assertNotIn('retry_after', body)

    def test_client_keeps_the_new_refusal_fields(self) -> None:
        from t6.client import _outcome
        refused = _outcome.validate_python({'outcome': 'refused', 'rule': 'provider', 'reason': 'x', 'failure': 'api_error', 'retry_after': 3.5})
        self.assertEqual((refused.failure, refused.retry_after), ('api_error', 3.5))  # type: ignore[union-attr]

    def reports(self, reply: str) -> None:
        from t6.privacy import Project
        self.fake = FakeProvider().reply(reply)
        self.api.llm_service = LLMService({'fake': self.fake}, 'fake', Registry(
            people=[Person('Jane Doe')], projects=[Project('Own Show', code='OWN'), Project('Other Show')]))

    def test_allow_projects_lets_one_project_through_and_masks_the_others(self) -> None:
        self.reports('{"summary": "Own Show and [project 1]"}')
        self.body.update(input_text='Own Show, Other Show and Jane Doe', masking_preset='strict', allow_projects=['Own Show'])
        result = self.call()['result']
        self.assertEqual(self.fake.calls[0].messages[-1]['content'], 'Summarize: Own Show, [project 1] and [person 1]')
        self.assertEqual(result['output'], {'summary': 'Own Show and Other Show'})
        row = str(self.audit())
        self.assertEqual(self.audit()['allow_projects'], 1)
        self.assertNotIn('Own Show', row)

    def test_allow_projects_accepts_a_code_and_lets_the_reply_name_it(self) -> None:
        self.reports('{"summary": "Own Show"}')
        self.body.update(input_text='x', allow_projects=['own'])
        self.assertEqual(self.call()['result']['output'], {'summary': 'Own Show'})

    def test_allow_does_not_let_other_names_leak_in_the_reply(self) -> None:
        self.reports('{"summary": "Other Show"}')
        self.body.update(allow_projects=['Own Show'])
        self.assertEqual(self.call()['failure'], 'leak')

    def test_allow_projects_refuses_a_person_or_unknown_name(self) -> None:
        for entry in ('Jane Doe', 'Nobody'):
            with self.subTest(entry=entry):
                self.reports('{"summary": "x"}')
                self.body.update(allow_projects=[entry])
                self.assertEqual(self.call()['rule'], 'allow')
                self.assertEqual(self.fake.calls, [])

    def test_allow_projects_refuses_a_project_whose_code_is_also_a_person(self) -> None:
        from t6.privacy import Project
        self.fake = FakeProvider().reply('{"summary": "x"}')
        self.api.llm_service = LLMService({'fake': self.fake}, 'fake', Registry(people=[Person('Sam')], projects=[Project('Atlas', code='Sam')]))
        self.body.update(input_text='Sam met Atlas', allow_projects=['Atlas'])
        self.assertEqual(self.call()['rule'], 'allow')
        self.assertEqual(self.fake.calls, [])

    def test_quotes_are_repaired_only_when_asked(self) -> None:
        reply = '{"summary": "He said "hi" twice"}'
        self.serve(FakeProvider().reply(reply))
        result = self.call()['result']
        self.assertEqual((result['output_valid'], result['failure'], result['repaired']), (False, 'invalid_json', False))
        self.serve(FakeProvider().reply(reply))
        self.body['repair_quotes'] = True
        result = self.call()['result']
        self.assertEqual((result['output_valid'], result['repaired']), (True, True))
        self.assertEqual(result['output'], {'summary': 'He said "hi" twice'})
        self.serve(FakeProvider().reply('{"summary": "fine"}'))
        self.assertFalse(self.call()['result']['repaired'])

    def per_field_body(self) -> None:
        self.body.update(output_schema={'type': 'object', 'required': ['changes'], 'properties': {'changes': {
            'type': 'array', 'items': {'type': 'object', 'properties': {'text': {'type': 'string'}}}}}},
            input_text='Jane Doe asked')
        self.serve(FakeProvider().reply('{"changes": [{"text": "[person 1] ok"}, {"text": "[person 7] ok"}]}'))

    def test_per_field_restore_keeps_good_fields_and_lists_the_bad_one(self) -> None:
        self.per_field_body()
        self.body['restore'] = 'per_field'
        result = self.call()['result']
        self.assertTrue(result['output_valid'])
        self.assertIsNone(result['failure'])
        self.assertEqual(result['output'], {'changes': [{'text': 'Jane Doe ok'}, {'text': '[person 7] ok'}]})
        self.assertEqual(result['unresolved'], ['changes[1].text'])
        self.assertEqual(result['masked_output'], {'changes': [{'text': '[person 1] ok'}, {'text': '[person 7] ok'}]})
        self.assertNotIn('Jane', str(result['masked_output']))

    def test_whole_restore_is_all_or_nothing(self) -> None:
        self.per_field_body()
        result = self.call()['result']
        self.assertFalse(result['output_valid'])
        self.assertEqual(result['failure'], 'unresolved_placeholder')
        self.assertEqual((result['unresolved'], result['masked_output']), ([], None))
