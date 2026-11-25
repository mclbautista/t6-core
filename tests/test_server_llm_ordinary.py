import json
import tempfile
import unittest
from typing import Any

from t6.llm.decisions import OpenRouterDecisions
from t6.llm.testing import FakeProvider
from t6.person import People
from t6.preview import PreviewTokens
from t6.privacy import Person, Project, Registry
from t6.registry import SqliteProjectRegistry
from t6.server.app import Request
from t6.server.llm import LLMService
from t6.server.verbs import VerbAPI

# `timeline` is a COMMON_WORDS word used as a Project name, `SO` (written in capitals) a PLAIN_WORDS word used as a code, `zqx` is no word.
REGISTRY = Registry(people=[Person('Jane Doe')],
                    projects=[Project('timeline'), Project('Harbour', code='SO'), Project('Beacon', code='zqx')])
SCHEMA = {'type': 'object', 'properties': {'summary': {'type': 'string'}}, 'required': ['summary']}


class OrdinaryNameTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.registry = SqliteProjectRegistry(tmp.name + '/registry')
        self.fake = FakeProvider()
        self.reply = '{"summary":"ok"}'
        service = LLMService({'fake': self.fake}, 'fake', REGISTRY)
        people = People(self.registry, PreviewTokens('test'), {'reports': frozenset({'llm'})})
        self.api = VerbAPI(people, None, {'secret': 'reports'}, lambda *args: None, llm_service=service)
        self.body: dict[str, Any] = {'task_name': 'summary', 'prompt_template': 'Summarize: {input}',
                                     'output_schema': SCHEMA, 'input_text': 'plain text', 'masking_preset': 'names'}

    def call(self) -> Any:
        self.fake.reply(self.reply)
        return self.api(Request('POST', '/v1/llm/run', {'Authorization': 'Bearer secret'}, json.dumps(self.body).encode())).body

    def schema_with(self, **properties: Any) -> dict[str, Any]:
        return {'type': 'object', 'properties': {'summary': {'type': 'string'}, **properties}, 'required': ['summary']}

    def audit(self) -> dict[str, Any]:
        with self.registry._transaction() as db:
            row = db.execute("SELECT after FROM audit WHERE action = 'llm_run' ORDER BY rowid DESC").fetchone()
        return json.loads(row[0])  # type: ignore[no-any-return]

    def test_ordinary_words_pass_in_the_template_in_any_case(self) -> None:
        for word in ('timeline', 'Timeline', 'TIMELINE', 'so', 'So', 'SO'):
            with self.subTest(word=word):
                self.body['prompt_template'] = f'List the {word} items: {{input}}'
                self.assertEqual(self.call()['outcome'], 'done')

    def test_ordinary_words_pass_in_the_schema_as_title_and_property_name(self) -> None:
        for word in ('timeline', 'Timeline', 'so', 'SO'):
            with self.subTest(word=word):
                self.body['output_schema'] = self.schema_with(**{word: {'type': 'string', 'title': word.capitalize(), 'description': f'the {word}'}})
                self.assertEqual(self.call()['outcome'], 'done')

    def test_a_name_that_is_no_word_is_refused_and_the_reason_names_the_part_not_the_word(self) -> None:
        self.body['prompt_template'] = 'Look at zqx: {input}'
        body = self.call()
        self.assertEqual((body['rule'], body['reason']), ('masking', 'Unresolved registered names in the prompt template'))
        self.body.update(prompt_template='Summarize: {input}', output_schema=self.schema_with(note={'type': 'string', 'description': 'about ZQX'}))
        body = self.call()
        self.assertEqual((body['rule'], body['reason']), ('masking', 'Unresolved registered names in the output schema'))
        self.assertNotIn('zqx', json.dumps(body).lower())
        self.assertEqual(self.fake.calls, [])

    def test_property_names_and_required_entries_are_checked(self) -> None:
        for word in ('zqx', 'Beacon'):
            for schema in (self.schema_with(**{word: {'type': 'string'}}),
                           {'type': 'object', 'properties': {'summary': {'type': 'string'}}, 'required': ['summary', word]}):
                with self.subTest(word=word, schema=schema):
                    self.body['output_schema'] = schema
                    body = self.call()
                    self.assertEqual((body['rule'], body['reason']), ('masking', 'Unresolved registered names in the output schema'))
        for word in ('timeline', 'SO'):
            for schema in (self.schema_with(**{word: {'type': 'string', 'title': word.capitalize()}}),
                           {'type': 'object', 'properties': {'summary': {'type': 'string'}}, 'required': ['summary', word]}):
                with self.subTest(word=word, schema=schema):
                    self.body['output_schema'] = schema
                    self.assertEqual(self.call()['outcome'], 'done')

    def test_a_pydantic_schema_with_a_field_named_after_an_ordinary_project_passes(self) -> None:
        from pydantic import BaseModel

        class Model(BaseModel):
            summary: str
            timeline: str

        self.body['output_schema'] = Model.model_json_schema()
        self.fake.reply('{"summary":"a","timeline":"b"}')
        self.assertEqual(self.call()['outcome'], 'done')

    def test_a_non_ordinary_name_is_refused_even_when_a_trivial_term_sits_beside_it(self) -> None:
        self.body['prompt_template'] = 'so Harbour: {input}'
        self.assertEqual(self.call()['reason'], 'Unresolved registered names in the prompt template')

    def test_names_preset_leaves_an_ordinary_project_name_and_a_lowercase_code_readable_in_the_input(self) -> None:
        self.body['input_text'] = 'the timeline is so late; Jane Doe, Harbour and zqx, So and SO'
        self.assertEqual(self.call()['outcome'], 'done')
        sent = self.fake.calls[0].messages[-1]['content']
        self.assertIn('the timeline is so late', sent)
        for hidden in ('Jane', 'Harbour', 'zqx', 'So ', 'SO'):
            self.assertNotIn(hidden, sent)

    def test_the_reply_check_follows_the_input_masking(self) -> None:
        self.reply = '{"summary":"the timeline is so late"}'
        self.assertEqual(self.call()['result']['output'], {'summary': 'the timeline is so late'})
        self.reply = '{"summary":"Harbour"}'
        self.assertEqual(self.call()['failure'], 'leak')

    def test_dry_run_checks_everything_and_does_not_call_the_provider(self) -> None:
        self.body['dry_run'] = True
        result = self.call()['result']
        self.assertEqual((result['output'], result['dry_run'], result['output_valid'], result['failure']), (None, True, True, None))
        self.assertEqual((result['model'], result['provider'], result['tokens']), ('fake-model', 'fake', {'prompt': 0, 'completion': 0, 'total': 0}))
        self.assertTrue(result['zdr'])
        self.assertEqual((len(result['prompt_hash']), len(result['schema_hash'])), (64, 64))
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.audit(), {'outcome': 'done', 'provider': 'fake', 'zdr': True, 'allow_projects': 0, 'dry_run': True})
        self.body['dry_run'] = False
        self.assertFalse(self.call()['result']['dry_run'])
        self.assertNotIn('dry_run', self.audit())

    def test_dry_run_returns_the_refusals_of_a_real_run(self) -> None:
        cases: list[tuple[dict[str, Any], str]] = [({'prompt_template': 'zqx {input}'}, 'masking'), ({'prompt_template': 'no marker'}, 'template'),
                 ({'output_schema': {'type': 'nope'}}, 'schema'), ({'model_hint': 'other'}, 'policy'),
                 ({'allow_projects': ['Jane Doe']}, 'allow')]
        base = dict(self.body)
        for change, rule in cases:
            with self.subTest(rule=rule):
                self.body = {**base, **change, 'dry_run': True}
                dry = self.call()
                self.body = {**base, **change}
                real = self.call()
                self.assertEqual(dry, real)
                self.assertEqual(dry['rule'], rule)
        self.assertEqual(self.fake.calls, [])


class OrdinaryNameDecideTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.registry = SqliteProjectRegistry(tmp.name + '/registry')
        self.requests: list[dict[str, Any]] = []

        def send(url: str, headers: dict[str, str], payload: bytes, timeout: float) -> bytes:
            self.requests.append(json.loads(payload))
            probabilities = {'match': 0.9, 'maybe': 0.06, 'no': 0.04}
            return json.dumps({'model': 'jev-test', 'usage': {'input_tokens': 1, 'output_tokens': 1},
                               'answers': {'c0': {'type': 'choice', 'choice': 'match', 'probabilities': probabilities}}}).encode()

        service = LLMService({'fake': FakeProvider()}, 'fake', REGISTRY, min_retention_by_provider={'decisions': 'unknown'},
                             decisions=OpenRouterDecisions(api_key='k', retention='unknown', send=send))
        people = People(self.registry, PreviewTokens('test'), {'reports': frozenset({'llm'})})
        self.api = VerbAPI(people, None, {'secret': 'reports'}, lambda *args: None, llm_service=service)
        self.body: dict[str, Any] = {
            'task_name': 'match', 'state': {'job': 'the timeline is so late, Harbour'},
            'question': 'Is the Timeline about {candidate} so?', 'criteria': {'match': 'Same', 'maybe': 'Unsure', 'no': 'TIMELINE other'},
            'candidates': ['teaser']}

    def call(self) -> Any:
        return self.api(Request('POST', '/v1/llm/decide', {'Authorization': 'Bearer secret'}, json.dumps(self.body).encode())).body

    def test_fixed_texts_may_hold_an_ordinary_word_and_the_input_keeps_it_readable(self) -> None:
        self.assertEqual(self.call()['outcome'], 'done')
        self.assertIn('the timeline is so late, [project 1]', json.dumps(self.requests[0]))

    def test_refusal_reasons_name_the_part(self) -> None:
        cases: list[tuple[dict[str, Any], str]] = [({'question': 'Is zqx {candidate}?'}, 'the question'),
                 ({'criteria': {'match': 'a', 'maybe': 'b', 'no': 'Harbour'}}, 'the criteria'),
                 ({'state': {'zqx': 'x'}}, 'a state key')]
        base = dict(self.body)
        for change, part in cases:
            with self.subTest(part=part):
                self.body = {**base, **change}
                body = self.call()
                self.assertEqual((body['rule'], body['reason']), ('masking', f'Unresolved registered names in {part}'))
                self.assertNotIn('zqx', json.dumps(body).lower())
        self.assertEqual(self.requests, [])


class OrdinaryPersonNameTests(unittest.TestCase):
    """A Contacts person whose name or alias is an ordinary word does not refuse a fixed schema (#520 deploy)."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        registry = SqliteProjectRegistry(tmp.name + '/registry')
        self.fake = FakeProvider()
        # `Deadline` is a person named with an ordinary word, `date` an ordinary alias, `Zqxa Vor` no word at all.
        names = Registry(people=[Person('Deadline'), Person('Jane Doe', aliases=('date',)), Person('Zqxa Vor')], projects=[])
        service = LLMService({'fake': self.fake}, 'fake', names)
        people = People(registry, PreviewTokens('test'), {'reports': frozenset({'llm'})})
        self.api = VerbAPI(people, None, {'secret': 'reports'}, lambda *args: None, llm_service=service)

    def call(self, schema: dict[str, Any]) -> Any:
        self.fake.reply('{"summary":"ok"}')
        body = {'task_name': 'summary', 'prompt_template': 'Summarize: {input}', 'output_schema': schema,
                'input_text': 'plain text', 'masking_preset': 'names'}
        return self.api(Request('POST', '/v1/llm/run', {'Authorization': 'Bearer secret'}, json.dumps(body).encode())).body

    def test_ordinary_person_names_and_aliases_pass_in_the_schema(self) -> None:
        schema = {'type': 'object', 'properties': {'summary': {'type': 'string'}, 'deadline': {'type': 'string', 'title': 'Deadline'},
                                                   'date': {'type': 'string', 'title': 'Date'}}, 'required': ['summary']}
        self.assertEqual(self.call(schema)['outcome'], 'done')

    def test_a_person_name_that_is_no_word_is_still_refused(self) -> None:
        schema = {'type': 'object', 'properties': {'summary': {'type': 'string', 'description': 'not for Zqxa Vor'}}, 'required': ['summary']}
        self.assertEqual(self.call(schema)['reason'], 'Unresolved registered names in the output schema')


class OrdinaryTitlePersonTests(unittest.TestCase):
    """Person-filed titles and lowercase ordinary names/aliases never mask or refuse ordinary words (#700)."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        registry = SqliteProjectRegistry(tmp.name + '/registry')
        self.fake = FakeProvider()
        names = Registry(
            people=[
                Person('Song for You and Me from Day session'),
                Person('for'),
                Person('Jane Doe', aliases=('and', 'session')),
                Person('Viva'),
            ],
            projects=[],
        )
        service = LLMService({'fake': self.fake}, 'fake', names)
        people = People(registry, PreviewTokens('test'), {'reports': frozenset({'llm'})})
        self.api = VerbAPI(people, None, {'secret': 'reports'}, lambda *args: None, llm_service=service)

    def run_llm(self, *, preset: str = 'strict', input_text: str = 'plain', reply: str = '{"summary":"ok"}',
                template: str = 'Summarize for Day and session from you: {input}') -> Any:
        self.fake.reply(reply)
        body = {'task_name': 'summary', 'prompt_template': template, 'output_schema': SCHEMA,
                'input_text': input_text, 'masking_preset': preset}
        return self.api(Request('POST', '/v1/llm/run', {'Authorization': 'Bearer secret'}, json.dumps(body).encode())).body

    def test_ordinary_words_from_person_title_and_aliases_stay_readable_and_do_not_leak(self) -> None:
        for preset in ('names', 'strict'):
            with self.subTest(preset=preset):
                res = self.run_llm(
                    preset=preset,
                    input_text='and And for For from From Day session Session',
                    reply='{"summary":"and for from Day session"}',
                )
                self.assertEqual(res['outcome'], 'done', res)
                self.assertEqual(res['result']['output'], {'summary': 'and for from Day session'})
                self.assertIn('and And for For from From Day session Session', self.fake.calls[-1].messages[-1]['content'])

    def test_real_one_word_person_name_is_still_masked_and_refused_on_leak(self) -> None:
        res_ok = self.run_llm(input_text='Viva joined', reply='{"summary":"[person 1] joined"}')
        self.assertEqual(res_ok['outcome'], 'done')
        self.assertEqual(res_ok['result']['output'], {'summary': 'Viva joined'})
        self.assertNotIn('Viva', self.fake.calls[-1].messages[-1]['content'])

        res_leak = self.run_llm(input_text='plain', reply='{"summary":"Viva"}')
        self.assertEqual((res_leak['outcome'], res_leak['rule'], res_leak['failure']), ('refused', 'masking', 'leak'))
