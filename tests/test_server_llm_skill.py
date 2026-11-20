"""Tests for server-side skill execution, privacy airlock, config, and audit metadata."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from t6.llm.api import LLMRunInput, LLMRunOutput
from t6.llm.config import ConfigError
from t6.llm.testing import FakeProvider
from t6.person import People
from t6.preview import PreviewTokens
from t6.privacy import Person, Project, Registry
from t6.privacy.masking import Leak, Session
from t6.registry import SqliteProjectRegistry
from t6.server.app import Request
from t6.server.llm import LLMService, service_from_env
from t6.server.verbs import VerbAPI


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')


SUMMARY_SCHEMA: dict[str, Any] = {
    'type': 'object',
    'properties': {'summary': {'type': 'string'}, 'role': {'type': 'string'}},
    'required': ['summary'],
    'additionalProperties': False,
}


class ServerLLMSkillTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.skills_dir = self.tmp / 'skills'
        self.skills_dir.mkdir()
        self.registry = SqliteProjectRegistry(str(self.tmp / 'registry'))
        self.privacy = Registry(
            people=[Person('Jane Doe', aliases=['Jane'])],
            projects=[Project('Beacon', code='BCN'), Project('timeline')],
        )
        self.fake = FakeProvider()
        self.service = LLMService(
            {'fake': self.fake},
            'fake',
            self.privacy,
            skills_dir=self.skills_dir,
            allowed_script_env={'ALLOWED_VAR': 'ok-val'},
            blocked_script_env=('CUSTOM_SECRET',),
        )
        people = People(self.registry, PreviewTokens('test'), {'reports': frozenset({'llm'})})
        self.api = VerbAPI(people, None, {'secret': 'reports'}, lambda *args: None, llm_service=self.service)
        self.body: dict[str, Any] = {
            'task_name': 'skill-summary',
            'prompt_template': 'Summarize: {input}',
            'input_text': 'Jane Doe reviewed the timeline',
            'masking_preset': 'names',
            'skill': 'briefing',
        }

    def call(self, body: dict[str, Any] | None = None) -> Any:
        payload = self.body if body is None else body
        return self.api(Request('POST', '/v1/llm/run', {'Authorization': 'Bearer secret'}, json.dumps(payload).encode())).body

    def audit(self) -> dict[str, Any]:
        with self.registry._transaction() as db:
            row = db.execute("SELECT after FROM audit WHERE action = 'llm_run' ORDER BY rowid DESC").fetchone()
        return json.loads(row[0])  # type: ignore[no-any-return]

    def test_end_to_end_pre_tool_post_airlock_and_audit(self) -> None:
        skill_dir = self.skills_dir / 'briefing'
        obs_file = self.tmp / 'observed.json'
        _write(skill_dir / 'references' / 'style.md', 'Always verify the timeline role.')
        _write(skill_dir / 'schemas' / 'out.json', json.dumps(SUMMARY_SCHEMA))
        _write(
            skill_dir / 'scripts' / 'pre.py',
            f"""import json, pathlib, sys
payload = json.load(sys.stdin)
obs_path = pathlib.Path({str(obs_file)!r})
obs = json.loads(obs_path.read_text()) if obs_path.exists() else {{}}
obs['pre'] = payload
obs_path.write_text(json.dumps(obs))
print(json.dumps({{'input': payload['input'] + ' for Beacon\\npassword: pre-secret'}}))
""",
        )
        _write(
            skill_dir / 'scripts' / 'lookup.py',
            f"""import json, os, pathlib, sys
args = json.load(sys.stdin)
obs_path = pathlib.Path({str(obs_file)!r})
obs = json.loads(obs_path.read_text())
obs['tool'] = args
obs['tool_env'] = {{
    'ALLOWED_VAR': os.environ.get('ALLOWED_VAR'),
    'OPENROUTER_API_KEY': os.environ.get('OPENROUTER_API_KEY'),
    'CUSTOM_SECRET': os.environ.get('CUSTOM_SECRET'),
}}
obs_path.write_text(json.dumps(obs))
print(json.dumps({{'who': args['who'], 'role': 'lead', 'note': 'password: tool-secret'}}))
""",
        )
        _write(
            skill_dir / 'scripts' / 'post.py',
            f"""import json, pathlib, sys
payload = json.load(sys.stdin)
obs_path = pathlib.Path({str(obs_file)!r})
obs = json.loads(obs_path.read_text())
obs['post'] = payload
obs_path.write_text(json.dumps(obs))
out = payload['output']
out['summary'] = 'FINAL: ' + out['summary']
print(json.dumps({{'output': out}}))
""",
        )
        _write(
            skill_dir / 'SKILL.md',
            """---
name: briefing-skill
description: Summarize with tool lookup.
t6:
  references:
    - references/style.md
  output_schema: schemas/out.json
  max_turns: 4
  env:
    - ALLOWED_VAR
    - OPENROUTER_API_KEY
    - CUSTOM_SECRET
  pre:
    - name: enrich
      script: scripts/pre.py
  tools:
    - name: lookup
      description: Look up a person's timeline role.
      script: scripts/lookup.py
      parameters:
        type: object
        properties:
          who:
            type: string
        required: [who]
  post:
    - name: polish
      script: scripts/post.py
---
Follow the style reference carefully.
""",
        )

        self.fake.tool_call('lookup', {'who': '[person 1]'}, prompt_tokens=12, completion_tokens=4).reply(
            '{"summary": "[person 1] on [project 1]", "role": "lead"}',
            prompt_tokens=20,
            completion_tokens=6,
        )

        with mock.patch.dict(os.environ, {'OPENROUTER_API_KEY': 'sk-leak', 'CUSTOM_SECRET': 'sec-leak'}):
            body = self.call()

        self.assertEqual(body['outcome'], 'done', body)
        result = body['result']
        self.assertTrue(result['output_valid'])
        self.assertEqual(result['output'], {'summary': 'FINAL: Jane Doe on Beacon', 'role': 'lead'})
        self.assertEqual(result['skill'], 'briefing-skill')
        self.assertEqual(len(result['skill_hash']), 64)
        self.assertEqual(result['turns'], 2)
        self.assertEqual(result['tokens'], {'prompt': 32, 'completion': 10, 'total': 42})
        self.assertEqual(
            [(r['name'], r['phase'], r['exit_code']) for r in result['script_runs']],
            [('enrich', 'pre', 0), ('lookup', 'tool', 0), ('polish', 'post', 0)],
        )
        self.assertTrue(all(set(r) == {'name', 'phase', 'exit_code', 'duration_ms'} for r in result['script_runs']))

        # Scripts received unmasked real names and sanitized env.
        observed = json.loads(obs_file.read_text(encoding='utf-8'))
        self.assertEqual(observed['pre'], {'input': 'Jane Doe reviewed the timeline'})
        self.assertEqual(observed['tool'], {'who': 'Jane Doe'})
        self.assertEqual(observed['tool_env'], {'ALLOWED_VAR': 'ok-val', 'OPENROUTER_API_KEY': None, 'CUSTOM_SECRET': None})
        self.assertEqual(observed['post'], {'output': {'summary': 'Jane Doe on Beacon', 'role': 'lead'}})

        # Provider never saw real names or credentials.
        self.assertEqual(len(self.fake.calls), 2)
        sent = json.dumps([list(c.messages) for c in self.fake.calls])
        for secret in ('Jane', 'Doe', 'Beacon', 'pre-secret', 'tool-secret', 'sk-leak', 'sec-leak'):
            self.assertNotIn(secret, sent)
        self.assertIn('Follow the style reference carefully.', self.fake.calls[0].messages[0]['content'])
        self.assertIn('Always verify the timeline role.', self.fake.calls[0].messages[0]['content'])
        self.assertEqual(len(self.fake.calls[0].tools), 1)
        tool_reply = json.loads(self.fake.calls[1].messages[-1]['content'])
        self.assertEqual(tool_reply['who'], '[person 1]')
        self.assertNotIn('tool-secret', tool_reply['note'])

        # Audit row records skill metadata without any caller text, names, or script stdout/stdin.
        audit = self.audit()
        self.assertEqual(audit['outcome'], 'done')
        self.assertEqual(audit['skill'], 'briefing-skill')
        self.assertEqual(audit['skill_hash'], result['skill_hash'])
        self.assertEqual(audit['turns'], 2)
        self.assertEqual(audit['script_runs'], result['script_runs'])
        with self.registry._transaction() as db:
            raw_row = str(db.execute("SELECT * FROM audit WHERE action = 'llm_run' ORDER BY rowid DESC").fetchone())
        for text in ('Jane', 'Beacon', 'FINAL', 'pre-secret', 'tool-secret', 'lead', 'reviewed'):
            self.assertNotIn(text, raw_row)

    def test_refuses_unconfigured_skills_dir_unknown_traversal_and_bad_skill(self) -> None:
        no_skills_api = VerbAPI(
            self.api.people,
            None,
            {'secret': 'reports'},
            lambda *args: None,
            llm_service=LLMService({'fake': self.fake}, 'fake', self.privacy),
        )
        res = no_skills_api(Request('POST', '/v1/llm/run', {'Authorization': 'Bearer secret'}, json.dumps(self.body).encode())).body
        self.assertEqual((res['outcome'], res['rule']), ('refused', 'skill'))

        outside = self.tmp / 'outside'
        _write(outside / 'SKILL.md', '---\nname: outside\n---\nHello\n')
        (self.skills_dir / 'sym').symlink_to(outside)
        _write(self.skills_dir / 'broken' / 'SKILL.md', 'not frontmatter')

        for bad_name in ('', '  ', '.', '..', '../outside', '/etc/passwd', 'a\\b', 'sym', 'missing', 'broken'):
            with self.subTest(bad_name=bad_name):
                body = {**self.body, 'skill': bad_name, 'output_schema': SUMMARY_SCHEMA}
                res = self.call(body)
                self.assertEqual((res['outcome'], res['rule']), ('refused', 'skill'))
                self.assertNotIn('outside', json.dumps(res))
        self.assertEqual(self.fake.calls, [])

    def test_requires_output_schema_unless_skill_provides_it(self) -> None:
        _write(self.skills_dir / 'no-schema' / 'SKILL.md', '---\nname: no-schema\n---\nBody.\n')
        res = self.call({**self.body, 'skill': 'no-schema'})
        self.assertEqual((res['outcome'], res['rule']), ('refused', 'schema'))

        res_no_skill = self.call({'task_name': 'x', 'prompt_template': 'Summarize: {input}', 'input_text': 'hi', 'masking_preset': 'names'})
        self.assertEqual((res_no_skill['outcome'], res_no_skill['rule']), ('refused', 'schema'))

        # Caller output_schema overrides or supplies schema when skill has none.
        self.fake.reply('{"summary": "ok"}')
        res_ok = self.call({**self.body, 'skill': 'no-schema', 'output_schema': SUMMARY_SCHEMA})
        self.assertEqual(res_ok['outcome'], 'done')
        self.assertEqual(res_ok['result']['output'], {'summary': 'ok'})

    def test_refuses_registered_names_in_skill_prompt_and_tools_and_enforces_max_bytes(self) -> None:
        cases = [
            ('body', '---\nname: leak-body\n---\nCall Jane Doe.\n', None),
            ('reference', '---\nname: leak-ref\nt6:\n  references: [references/r.md]\n---\nClean body.\n', 'See Beacon.'),
            ('tool_name', '---\nname: leak-tname\nt6:\n  tools:\n    - name: Beacon\n      script: scripts/t.py\n---\nClean.\n', None),
            ('tool_desc', '---\nname: leak-tdesc\nt6:\n  tools:\n    - name: t\n      description: Ask Jane Doe\n      script: scripts/t.py\n---\nClean.\n', None),
            ('tool_param_key', '---\nname: leak-tkey\nt6:\n  tools:\n    - name: t\n      script: scripts/t.py\n      parameters:\n        type: object\n        properties:\n          Beacon:\n            type: string\n---\nClean.\n', None),
            ('tool_param_val', '---\nname: leak-tval\nt6:\n  tools:\n    - name: t\n      script: scripts/t.py\n      parameters:\n        type: object\n        properties:\n          x:\n            description: Jane Doe ID\n---\nClean.\n', None),
        ]
        for label, skill_md, ref in cases:
            with self.subTest(label=label):
                sdir = self.skills_dir / f'leak_{label}'
                _write(sdir / 'scripts' / 't.py', "print('{}')\n")
                if ref is not None:
                    _write(sdir / 'references' / 'r.md', ref)
                _write(sdir / 'SKILL.md', skill_md)
                res = self.call({**self.body, 'skill': f'leak_{label}', 'output_schema': SUMMARY_SCHEMA})
                self.assertEqual((res['outcome'], res['rule']), ('refused', 'masking'))
                self.assertNotIn('Jane', json.dumps(res))
                self.assertNotIn('Beacon', json.dumps(res))

        # Ordinary word ("timeline") in prompt and tools passes FIXED_TEXT, and max_bytes includes skill prompt/tools.
        sdir = self.skills_dir / 'big'
        _write(sdir / 'SKILL.md', '---\nname: big\n---\n' + ('timeline ' * 50) + '\n')
        small_service = LLMService({'fake': self.fake}, 'fake', self.privacy, max_bytes=200, skills_dir=self.skills_dir)
        small_api = VerbAPI(self.api.people, None, {'secret': 'reports'}, lambda *args: None, llm_service=small_service)
        res_size = small_api(Request(
            'POST', '/v1/llm/run', {'Authorization': 'Bearer secret'},
            json.dumps({**self.body, 'skill': 'big', 'output_schema': SUMMARY_SCHEMA}).encode(),
        )).body
        self.assertEqual((res_size['outcome'], res_size['rule']), ('refused', 'size'))
        self.assertEqual(self.fake.calls, [])

    def test_dry_run_with_skill_does_not_execute_scripts_or_provider(self) -> None:
        sdir = self.skills_dir / 'dry'
        marker = self.tmp / 'ran.txt'
        _write(sdir / 'scripts' / 'pre.py', f"import pathlib\npathlib.Path({str(marker)!r}).write_text('ran')\nprint('{{}}')\n")
        _write(
            sdir / 'SKILL.md',
            """---
name: dry-skill
t6:
  pre:
    - name: p
      script: scripts/pre.py
---
Clean prompt.
""",
        )
        res = self.call({**self.body, 'skill': 'dry', 'output_schema': SUMMARY_SCHEMA, 'dry_run': True})
        self.assertEqual(res['outcome'], 'done')
        self.assertFalse(marker.exists())
        self.assertEqual(self.fake.calls, [])
        out = res['result']
        self.assertTrue(out['dry_run'])
        self.assertEqual((out['skill'], out['turns'], out['script_runs']), ('dry-skill', 0, []))
        audit = self.audit()
        self.assertEqual((audit['dry_run'], audit['skill'], audit['turns'], audit['script_runs']), (True, 'dry-skill', 0, []))

    def test_error_handling_pre_tool_max_turns_post_and_leaks(self) -> None:
        # 1. pre fail -> script_error, turns=0
        s1 = self.skills_dir / 'pre_fail'
        _write(s1 / 'scripts' / 'pre.py', "import sys\nsys.stderr.write('Jane Doe failed')\nsys.exit(3)\n")
        _write(s1 / 'SKILL.md', "---\nname: pre-fail\nt6:\n  pre:\n    - name: check\n      script: scripts/pre.py\n      on_error: fail\n---\nPrompt.\n")
        res1 = self.call({**self.body, 'skill': 'pre_fail', 'output_schema': SUMMARY_SCHEMA})
        self.assertEqual(res1['outcome'], 'done')
        self.assertFalse(res1['result']['output_valid'])
        self.assertEqual(res1['result']['failure'], 'script_error')
        self.assertEqual(res1['result']['turns'], 0)
        self.assertEqual(res1['result']['script_runs'][0]['exit_code'], 3)
        self.assertEqual(self.fake.calls, [])
        self.assertNotIn('Jane', str(self.audit()))

        # 2. pre return_to_model -> masked note delivered to model
        s2 = self.skills_dir / 'pre_recover'
        _write(s2 / 'scripts' / 'pre.py', "import sys\nsys.stderr.write('Jane Doe password: pw123')\nsys.exit(2)\n")
        _write(s2 / 'SKILL.md', "---\nname: pre-recover\nt6:\n  pre:\n    - name: check\n      script: scripts/pre.py\n      on_error: return_to_model\n---\nPrompt.\n")
        self.fake.reply('{"summary": "[person 1] ok"}')
        res2 = self.call({**self.body, 'skill': 'pre_recover', 'output_schema': SUMMARY_SCHEMA})
        self.assertEqual(res2['outcome'], 'done')
        self.assertTrue(res2['result']['output_valid'])
        last_msg = self.fake.calls[-1].messages[-1]['content']
        self.assertIn('[person 1]', last_msg)
        self.assertNotIn('Jane', last_msg)
        self.assertNotIn('pw123', last_msg)

        # 3. tool on_error: return_to_model recovers, on_error: fail aborts
        s3 = self.skills_dir / 'tool_errs'
        _write(s3 / 'scripts' / 'flaky.py', "import sys\nsys.stderr.write('Beacon password: sec')\nsys.exit(4)\n")
        _write(s3 / 'scripts' / 'boom.py', "import sys\nsys.exit(5)\n")
        _write(
            s3 / 'SKILL.md',
            """---
name: tool-errs
t6:
  tools:
    - name: flaky
      script: scripts/flaky.py
      on_error: return_to_model
    - name: boom
      script: scripts/boom.py
      on_error: fail
---
Use tools.
""",
        )
        self.fake.calls.clear()
        self.fake.tool_call('flaky', {}, prompt_tokens=5, completion_tokens=1).reply('{"summary": "recovered"}', prompt_tokens=6, completion_tokens=2)
        res3 = self.call({**self.body, 'skill': 'tool_errs', 'output_schema': SUMMARY_SCHEMA})
        self.assertTrue(res3['result']['output_valid'])
        self.assertEqual(res3['result']['turns'], 2)
        self.assertEqual(res3['result']['script_runs'][0]['exit_code'], 4)
        self.assertNotIn('Beacon', self.fake.calls[1].messages[-1]['content'])
        self.assertNotIn('sec', self.fake.calls[1].messages[-1]['content'])

        self.fake.tool_call('boom', {}, prompt_tokens=3, completion_tokens=1)
        res4 = self.call({**self.body, 'skill': 'tool_errs', 'output_schema': SUMMARY_SCHEMA})
        self.assertFalse(res4['result']['output_valid'])
        self.assertEqual(res4['result']['failure'], 'script_error')
        self.assertEqual(res4['result']['script_runs'][0]['exit_code'], 5)

        # 4. max_turns_exceeded
        s4 = self.skills_dir / 'loop'
        _write(s4 / 'scripts' / 'ping.py', "print('{}')\n")
        _write(s4 / 'SKILL.md', "---\nname: loop\nt6:\n  max_turns: 2\n  tools:\n    - name: ping\n      script: scripts/ping.py\n---\nPing.\n")
        self.fake.tool_call('ping', {}, prompt_tokens=4, completion_tokens=1).tool_call('ping', {}, prompt_tokens=5, completion_tokens=2)
        res5 = self.call({**self.body, 'skill': 'loop', 'output_schema': SUMMARY_SCHEMA})
        self.assertFalse(res5['result']['output_valid'])
        self.assertEqual(res5['result']['failure'], 'max_turns_exceeded')
        self.assertEqual(res5['result']['turns'], 2)
        self.assertEqual(res5['result']['tokens'], {'prompt': 9, 'completion': 3, 'total': 12})
        self.assertEqual(len(res5['result']['script_runs']), 1)

        # 5. tool argument leak -> Refused(rule='masking', failure='leak')
        self.fake.tool_call('ping', {'who': 'Jane Doe'})
        res6 = self.call({**self.body, 'skill': 'loop', 'output_schema': SUMMARY_SCHEMA})
        self.assertEqual((res6['outcome'], res6['rule'], res6['failure']), ('refused', 'masking', 'leak'))

        # 6. post schema violation & post script failure
        s5 = self.skills_dir / 'post_bad'
        _write(s5 / 'scripts' / 'post.py', "import json\nprint(json.dumps({'output': {'summary': 123}}))\n")
        _write(s5 / 'SKILL.md', "---\nname: post-bad\nt6:\n  post:\n    - name: mangle\n      script: scripts/post.py\n---\nGo.\n")
        self.fake.reply('{"summary": "ok"}')
        res7 = self.call({**self.body, 'skill': 'post_bad', 'output_schema': SUMMARY_SCHEMA})
        self.assertFalse(res7['result']['output_valid'])
        self.assertEqual(res7['result']['failure'], 'schema_violation')
        self.assertEqual(res7['result']['script_runs'][0]['phase'], 'post')

        _write(s5 / 'scripts' / 'post.py', "import sys\nsys.exit(9)\n")
        self.fake.reply('{"summary": "ok"}')
        res8 = self.call({**self.body, 'skill': 'post_bad', 'output_schema': SUMMARY_SCHEMA})
        self.assertFalse(res8['result']['output_valid'])
        self.assertEqual(res8['result']['failure'], 'script_error')
        self.assertEqual(res8['result']['script_runs'][0]['exit_code'], 9)

    def test_post_schema_with_output_property_and_per_field_restore(self) -> None:
        sdir = self.skills_dir / 'output_prop'
        schema = {'type': 'object', 'properties': {'output': {'type': 'string'}}, 'required': ['output']}
        _write(sdir / 'scripts' / 'post.py', "import json, sys\nd = json.load(sys.stdin)['output']\nprint(json.dumps({'output': d['output'] + '!' }))\n")
        _write(
            sdir / 'SKILL.md',
            """---
name: output-prop
t6:
  post:
    - name: bang
      script: scripts/post.py
---
Prompt.
""",
        )
        self.fake.reply('{"output": "[person 1] and [person 9]"}')
        res = self.call({**self.body, 'skill': 'output_prop', 'output_schema': schema, 'restore': 'per_field'})
        self.assertEqual(res['outcome'], 'done')
        out = res['result']
        self.assertTrue(out['output_valid'])
        self.assertEqual(out['output'], {'output': 'Jane Doe and [person 9]!'})
        self.assertEqual(out['unresolved'], ['output'])
        self.assertEqual(out['masked_output'], {'output': '[person 1] and [person 9]'})

    def test_non_skill_run_keeps_audit_unchanged(self) -> None:
        self.fake.reply('{"summary": "[person 1] ok"}')
        body = dict(self.body)
        body.pop('skill')
        body['output_schema'] = SUMMARY_SCHEMA
        res = self.call(body)
        self.assertEqual(res['outcome'], 'done')
        self.assertEqual(self.audit(), {'outcome': 'done', 'provider': 'fake', 'zdr': True, 'allow_projects': 0})

    def test_tool_output_leak_mocked_is_refused(self) -> None:
        sdir = self.skills_dir / 't'
        _write(sdir / 'scripts' / 't.py', "import json\nprint(json.dumps({'x': 'y'}))\n")
        _write(sdir / 'SKILL.md', "---\nname: t\nt6:\n  tools:\n    - name: t\n      script: scripts/t.py\n---\nGo.\n")
        self.fake.tool_call('t', {})
        with mock.patch.object(
            Session,
            'leaks',
            autospec=True,
            side_effect=lambda _self, val: [Leak('', 'Jane Doe')] if val == '{"x": "y"}' else [],
        ):
            res = self.call({**self.body, 'skill': 't', 'output_schema': SUMMARY_SCHEMA})
        self.assertEqual((res['outcome'], res['rule'], res['failure']), ('refused', 'masking', 'leak'))
        audit = self.audit()
        self.assertEqual((audit['outcome'], audit['refused_rule'], audit['failure']), ('refused', 'masking', 'leak'))
        self.assertEqual((audit['skill'], audit['turns'], len(audit['script_runs'])), ('t', 1, 1))

    def test_client_llm_run_model_compatibility(self) -> None:
        req = LLMRunInput(task_name='t', prompt_template='Q: {input}', input_text='Jane Doe', masking_preset='names', skill='s')
        self.assertIsNone(req.output_schema)
        self.assertEqual(req.skill, 's')
        out = LLMRunOutput(
            output={'summary': 'ok'},
            model='m',
            provider='fake',
            prompt_hash='a' * 64,
            schema_hash='b' * 64,
            tokens={'prompt': 1, 'completion': 2, 'total': 3},
            skill='s',
            skill_hash='c' * 64,
            turns=2,
            script_runs=[{'name': 't', 'phase': 'tool', 'exit_code': 0, 'duration_ms': 5}],
        )
        validated = LLMRunOutput.model_validate(out.model_dump())
        self.assertEqual((validated.skill, validated.turns, len(validated.script_runs)), ('s', 2, 1))


class ServerSkillConfigTests(unittest.TestCase):
    def build(self, extra: dict[str, Any], env: dict[str, str] | None = None) -> LLMService | None:
        config = {
            'llm': {
                'default': 'chat',
                'providers': {
                    'chat': {
                        'base_url': 'https://llm.example',
                        'model': 'm',
                        'retention': 'zdr_enforced',
                        'api_key_env': 'CHAT_KEY',
                    }
                },
            },
            'privacy_registry': {},
            **extra,
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'llm.json'
            path.write_text(json.dumps(config), encoding='utf-8')
            return service_from_env({'T6_LLM_CONFIG': str(path), 'CHAT_KEY': 'chat-secret', **(env or {})})

    def test_service_from_env_rejects_provider_api_key_env_in_allowed_script_env(self) -> None:
        with self.assertRaises(ConfigError):
            self.build({'allowed_script_env': ['CHAT_KEY']})
        with self.assertRaises(ConfigError):
            self.build({'allowed_script_env': {'CHAT_KEY': 'override'}})
        with self.assertRaises(ConfigError):
            self.build(
                {'decisions': {'api_key_env': 'DEC_KEY'}, 'allowed_script_env': ['DEC_KEY']},
                {'DEC_KEY': 'dec-secret'},
            )

    def test_service_from_env_rejects_invalid_skills_dir_or_allowed_script_env(self) -> None:
        for bad in ('', '   ', 123, ['x']):
            with self.subTest(skills_dir=bad), self.assertRaises(ConfigError):
                self.build({'skills_dir': bad})
        bad_envs: tuple[Any, ...] = ('FOO', 123, ['A=B'], ['bad key'], {'A=B': 'x'}, {'bad key': 'x'}, {'A': 123})
        for bad_env in bad_envs:
            with self.subTest(allowed_script_env=bad_env), self.assertRaises(ConfigError):
                self.build({'allowed_script_env': bad_env})

    def test_service_from_env_isolates_env_and_resolves_relative_skills_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skills = root / 'skills'
            _write(
                skills / 'env-skill' / 'scripts' / 'show.py',
                "import json, os\nprint(json.dumps({'ok': os.environ.get('SKILL_FLAG'), 'key': os.environ.get('CHAT_KEY'), 'unlisted': os.environ.get('HOST_SECRET')}))\n",
            )
            _write(
                skills / 'env-skill' / 'SKILL.md',
                """---
name: env-skill
t6:
  output_schema:
    type: object
    properties:
      flag: {type: string}
      key: {type: [string, 'null']}
      unlisted: {type: [string, 'null']}
    required: [flag]
  env:
    - SKILL_FLAG
    - CHAT_KEY
    - HOST_SECRET
  tools:
    - name: show
      script: scripts/show.py
---
Use show.
""",
            )
            cfg = {
                'llm': {
                    'default': 'chat',
                    'providers': {
                        'chat': {
                            'base_url': 'https://llm.example',
                            'model': 'm',
                            'retention': 'zdr_enforced',
                            'api_key_env': 'CHAT_KEY',
                        }
                    },
                },
                'privacy_registry': {},
                'skills_dir': 'skills',
                'allowed_script_env': ['SKILL_FLAG'],
            }
            cfg_path = root / 'llm.json'
            cfg_path.write_text(json.dumps(cfg), encoding='utf-8')
            service = service_from_env({
                'T6_LLM_CONFIG': str(cfg_path),
                'CHAT_KEY': 'chat-secret',
                'SKILL_FLAG': 'enabled',
                'HOST_SECRET': 'do-not-leak',
            })
            assert service is not None
            self.assertEqual(service.skills_dir, skills.resolve())
            self.assertEqual(service.allowed_script_env, {'SKILL_FLAG': 'enabled'})
            self.assertIn('CHAT_KEY', service.blocked_script_env)

            # Verify runtime execution via the service's env controls.
            fake = FakeProvider().tool_call('show', {}).reply('{"flag": "enabled", "key": null, "unlisted": null}')
            service.providers['chat'] = fake
            service.default = 'chat'
            reg = SqliteProjectRegistry(str(root / 'reg'))
            with mock.patch.dict(os.environ, {'HOST_SECRET': 'os-secret', 'CHAT_KEY': 'os-chat-key'}):
                done = service.run(
                    LLMRunInput(task_name='env', prompt_template='Run: {input}', input_text='hi', masking_preset='names', skill='env-skill'),
                    actor='tester',
                    registry=reg,
                )
            self.assertEqual(done.outcome, 'done')
            tool_payload = json.loads(fake.calls[1].messages[-1]['content'])
            self.assertEqual(tool_payload, {'ok': 'enabled', 'key': None, 'unlisted': None})


if __name__ == '__main__':
    unittest.main()
