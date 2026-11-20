"""End-to-end tests for multi-turn skill execution and privacy airlock in ``t6.llm.task``."""

from __future__ import annotations

import dataclasses
import json
import os
import tempfile
import unittest
from enum import Enum
from pathlib import Path
from typing import Any
from unittest import mock

from pydantic import BaseModel, ConfigDict

from t6.llm import (
    DataPolicy,
    DecisionsRequest,
    Choice,
    Failed,
    Message,
    Ok,
    ScriptRun,
    Skill,
    Task,
    Tokens,
    ToolCall,
    run,
)
from t6.llm.task import assistant_tool_message, execute_tool_call, mask_for_model
from t6.llm.testing import FakeProvider
from t6.privacy import Masked, Person, Project, Registry, Session, Withheld, pseudonymize, withhold


class Tone(str, Enum):
    brief = "brief"
    full = "full"


@dataclasses.dataclass(frozen=True)
class Notes:
    text: Masked
    tone: Tone = Tone.brief


@dataclasses.dataclass(frozen=True)
class Ticket:
    text: Withheld


class Summary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    headline: str
    role: str = ""


def render_notes(notes: Notes) -> list[Message]:
    return [
        {"role": "system", "content": f"Return JSON summary. Tone={notes.tone.value}."},
        {"role": "user", "content": notes.text.text},
    ]


def render_ticket(ticket: Ticket) -> list[Message]:
    return [{"role": "user", "content": ticket.text.text}]


def make_registry() -> Registry:
    return Registry(
        people=[Person("Marco Rossi", aliases=["Marco"])],
        projects=[Project("Other Show", code="OS1")],
    )


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class SkillTaskAirlockTests(unittest.TestCase):
    def test_pre_tool_loop_post_privacy_airlock_and_trace_aggregation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "skill"
            log_file = Path(tmp) / "observed.json"

            _write(root / "references" / "guide.md", "Always include the person's verified role.")
            _write(
                root / "scripts" / "pre.py",
                f"""import json, pathlib, sys
payload = json.load(sys.stdin)
obs_path = pathlib.Path({str(log_file)!r})
obs = json.loads(obs_path.read_text()) if obs_path.exists() else {{}}
obs["pre"] = payload
obs_path.write_text(json.dumps(obs))
data = payload["input"]
data["text"] = data["text"] + "\\nOther Show\\npin: 9999"
data["tone"] = "full"
print(json.dumps({{"input": data}}))
""",
            )
            _write(
                root / "scripts" / "lookup.py",
                f"""import json, os, pathlib, sys
args = json.load(sys.stdin)
obs_path = pathlib.Path({str(log_file)!r})
obs = json.loads(obs_path.read_text())
obs["tool"] = args
obs["tool_env"] = {{
    "ALLOWED_CFG": os.environ.get("ALLOWED_CFG"),
    "OPENROUTER_API_KEY": os.environ.get("OPENROUTER_API_KEY"),
    "BLOCKED_KEY": os.environ.get("BLOCKED_KEY"),
}}
obs_path.write_text(json.dumps(obs))
print(json.dumps({{"who": args["who"], "role": "editor", "secret": "password: letmein"}}))
""",
            )
            _write(
                root / "scripts" / "post.py",
                f"""import json, pathlib, sys
payload = json.load(sys.stdin)
obs_path = pathlib.Path({str(log_file)!r})
obs = json.loads(obs_path.read_text())
obs["post"] = payload
obs_path.write_text(json.dumps(obs))
out = payload["output"]
out["headline"] = "FINAL: " + out["headline"]
print(json.dumps({{"output": out}}))
""",
            )
            _write(
                root / "SKILL.md",
                """---
name: airlock-skill
description: Test full pre/tool/post airlock.
t6:
  references:
    - references/guide.md
  max_turns: 4
  env:
    - ALLOWED_CFG
    - OPENROUTER_API_KEY
    - BLOCKED_KEY
  pre:
    - name: normalize
      script: scripts/pre.py
  tools:
    - name: lookup
      description: Look up person role.
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
Follow the reference guide closely.
""",
            )

            skill = Skill.load(root)
            task: Task[Notes, Summary] = Task("airlock-v1", Notes, Summary, render_notes, skill=skill)
            masked, session = pseudonymize("Marco Rossi moved grade\npassword: hunter2", make_registry())

            fake = (
                FakeProvider("router/model")
                .tool_call("lookup", {"who": "[person 1]"}, prompt_tokens=15, completion_tokens=5)
                .reply('{"headline": "[person 1] on [project 1]", "role": "editor"}', prompt_tokens=25, completion_tokens=8)
            )
            with mock.patch.dict(
                os.environ,
                {"OPENROUTER_API_KEY": "sk-or-secret", "BLOCKED_KEY": "secret-blocked"},
                clear=False,
            ):
                result = run(
                    task,
                    Notes(masked),
                    provider=fake,
                    session=session,
                    allowed_env={"ALLOWED_CFG": "ok-cfg", "OPENROUTER_API_KEY": "sk-override"},
                    blocked_env=("BLOCKED_KEY",),
                )

            assert isinstance(result, Ok), result
            self.assertEqual(result.value, Summary(headline="FINAL: Marco Rossi on Other Show", role="editor"))

            # Verify scripts received real unmasked names and sanitized env.
            observed = json.loads(log_file.read_text(encoding="utf-8"))
            self.assertEqual(
                observed["pre"]["input"],
                {"text": "Marco Rossi moved grade\npassword: hunter2", "tone": "brief"},
            )
            self.assertEqual(observed["tool"], {"who": "Marco Rossi"})
            self.assertEqual(observed["tool_env"], {"ALLOWED_CFG": "ok-cfg", "OPENROUTER_API_KEY": None, "BLOCKED_KEY": None})
            self.assertEqual(observed["post"]["output"], {"headline": "Marco Rossi on Other Show", "role": "editor"})

            # Verify provider never saw real names or passwords on any turn.
            self.assertEqual(len(fake.calls), 2)
            first_call, second_call = fake.calls
            self.assertEqual(first_call.tools, (skill.tools[0].to_spec(),))
            self.assertEqual(first_call.messages[0]["role"], "system")
            self.assertIn("Follow the reference guide closely.", first_call.messages[0]["content"])
            self.assertIn("Always include the person's verified role.", first_call.messages[0]["content"])
            self.assertIn("Tone=full.", first_call.messages[1]["content"])

            all_sent = json.dumps([list(c.messages) for c in fake.calls])
            for secret in ("Marco", "Rossi", "Other Show", "hunter2", "9999", "letmein"):
                self.assertNotIn(secret, all_sent)
            self.assertIn("[person 1]", all_sent)
            self.assertIn("[project 1]", all_sent)

            tool_msg = second_call.messages[-1]
            self.assertEqual(tool_msg["role"], "tool")
            self.assertEqual(tool_msg["tool_call_id"], "call_1")
            tool_payload = json.loads(tool_msg["content"])
            self.assertEqual(tool_payload["who"], "[person 1]")
            self.assertEqual(tool_payload["role"], "editor")
            self.assertNotIn("letmein", tool_payload["secret"])

            # Verify Trace aggregates tokens, turns, skill metadata, and script_runs.
            trace = result.trace
            self.assertEqual(trace.skill_name, "airlock-skill")
            self.assertEqual(trace.skill_hash, skill.skill_hash)
            self.assertEqual(trace.turns, 2)
            self.assertEqual(trace.tokens, Tokens(40, 13))
            self.assertEqual(trace.tokens.total, 53)
            self.assertEqual([(r.name, r.phase, r.exit_code) for r in trace.script_runs], [
                ("normalize", "pre", 0),
                ("lookup", "tool", 0),
                ("polish", "post", 0),
            ])

    def test_pre_unwrapped_json_and_post_empty_stdout_validator(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "skill"
            _write(
                root / "scripts" / "pre.py",
                "import json, sys\nd = json.load(sys.stdin)['input']\nd['text'] = d['text'] + ' CONFIRMED'\nprint(json.dumps(d))\n",
            )
            _write(
                root / "scripts" / "post_check.py",
                "import json, sys\nd = json.load(sys.stdin)['output']\nassert d['headline']\n",
            )
            _write(
                root / "SKILL.md",
                """---
name: validator-skill
t6:
  pre:
    - name: tag
      script: scripts/pre.py
  post:
    - name: check
      script: scripts/post_check.py
---
Instructions.
""",
            )
            skill = Skill.load(root)
            task: Task[Notes, Summary] = Task("t", Notes, Summary, render_notes)
            masked, session = pseudonymize("Marco Rossi", make_registry())
            fake = FakeProvider().reply('{"headline": "[person 1]"}')

            result = run(task, Notes(masked), provider=fake, session=session, skill=skill)
            assert isinstance(result, Ok)
            self.assertEqual(result.value.headline, "Marco Rossi")
            self.assertEqual(fake.calls[0].messages[-1]["content"], "[person 1] CONFIRMED")
            self.assertEqual(len(result.trace.script_runs), 2)

    def test_withhold_policy_without_session_masks_credentials_across_scripts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "skill"
            _write(
                root / "scripts" / "pre.py",
                "import json, sys\nd = json.load(sys.stdin)['input']\nd['text'] = d['text'] + ' pin: 1111'\nprint(json.dumps(d))\n",
            )
            _write(
                root / "scripts" / "tool.py",
                "import json, sys\n_ = json.load(sys.stdin)\nprint(json.dumps({'info': 'password: secret'}))\n",
            )
            _write(
                root / "SKILL.md",
                """---
name: withhold-skill
t6:
  pre:
    - name: add_pin
      script: scripts/pre.py
  tools:
    - name: info
      script: scripts/tool.py
---
Use info tool.
""",
            )
            skill = Skill.load(root)
            task: Task[Ticket, Summary] = Task(
                "withhold-v1", Ticket, Summary, render_ticket, policy=DataPolicy("unknown", "withhold"), skill=skill
            )
            fake = (
                FakeProvider(retention="unknown")
                .tool_call("info", {})
                .reply('{"headline": "checked"}')
            )
            result = run(task, Ticket(withhold("hello")), provider=fake)
            assert isinstance(result, Ok)
            sent = json.dumps([list(c.messages) for c in fake.calls])
            self.assertNotIn("1111", sent)
            self.assertNotIn("secret", sent)


class SkillErrorHandlingTests(unittest.TestCase):
    def test_tool_on_error_return_to_model_masks_error_and_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "skill"
            _write(
                root / "scripts" / "flaky.py",
                "import sys\nsys.stderr.write('Marco Rossi failed with password: topsecret\\n')\nsys.exit(2)\n",
            )
            _write(
                root / "SKILL.md",
                """---
name: recover-skill
t6:
  tools:
    - name: flaky
      description: Might fail.
      script: scripts/flaky.py
      on_error: return_to_model
---
Call flaky then answer.
""",
            )
            skill = Skill.load(root)
            task: Task[Notes, Summary] = Task("recover-v1", Notes, Summary, render_notes, skill=skill)
            masked, session = pseudonymize("Marco Rossi", make_registry())
            fake = (
                FakeProvider()
                .tool_call("flaky", {"who": "[person 1]"}, prompt_tokens=10, completion_tokens=2)
                .reply('{"headline": "[person 1] recovered"}', prompt_tokens=12, completion_tokens=4)
            )
            result = run(task, Notes(masked), provider=fake, session=session)
            assert isinstance(result, Ok)
            self.assertEqual(result.value.headline, "Marco Rossi recovered")
            self.assertEqual(result.trace.turns, 2)
            self.assertEqual(result.trace.tokens, Tokens(22, 6))
            self.assertEqual(result.trace.script_runs[0].exit_code, 2)

            tool_msg = fake.calls[1].messages[-1]
            self.assertEqual(tool_msg["role"], "tool")
            err_json = json.loads(tool_msg["content"])
            self.assertEqual(err_json["exit_code"], 2)
            self.assertIn("[person 1]", err_json["error"])
            self.assertNotIn("Marco", tool_msg["content"])
            self.assertNotIn("topsecret", tool_msg["content"])

    def test_tool_on_error_fail_aborts_immediately_with_script_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "skill"
            _write(
                root / "scripts" / "boom.py",
                "import sys\nsys.stderr.write('Marco Rossi crashed password: xyz\\n')\nsys.exit(7)\n",
            )
            _write(
                root / "SKILL.md",
                """---
name: fail-skill
t6:
  tools:
    - name: boom
      script: scripts/boom.py
      on_error: fail
---
Run boom.
""",
            )
            skill = Skill.load(root)
            task: Task[Notes, Summary] = Task("fail-v1", Notes, Summary, render_notes, skill=skill)
            masked, session = pseudonymize("Marco Rossi", make_registry())
            fake = (
                FakeProvider()
                .tool_call("boom", {}, prompt_tokens=9, completion_tokens=3)
                .reply('{"headline": "never reached"}')
            )
            result = run(task, Notes(masked), provider=fake, session=session)
            assert isinstance(result, Failed)
            self.assertEqual(result.reason, "script_error")
            self.assertNotIn("Marco", result.detail)
            self.assertNotIn("xyz", result.detail)
            self.assertEqual(len(fake.calls), 1)
            self.assertEqual(fake.pending, 1)
            self.assertEqual(result.trace.turns, 1)
            self.assertEqual(result.trace.tokens, Tokens(9, 3))
            self.assertEqual(len(result.trace.script_runs), 1)
            self.assertEqual(result.trace.script_runs[0], ScriptRun("boom", "tool", 7, result.trace.script_runs[0].duration_ms))

            # Timeout with on_error=fail also aborts with script_error.
            _write(root / "scripts" / "slow.py", "import time\ntime.sleep(1.0)\n")
            _write(
                root / "SKILL.md",
                """---
name: timeout-skill
t6:
  tools:
    - name: slow
      script: scripts/slow.py
      timeout: 0.05
      on_error: fail
---
Run slow.
""",
            )
            slow_skill = Skill.load(root)
            masked_t, session_t = pseudonymize("Marco Rossi", make_registry())
            slow_res = run(
                Task("slow-v1", Notes, Summary, render_notes, skill=slow_skill),
                Notes(masked_t),
                provider=FakeProvider().tool_call("slow", {}),
                session=session_t,
            )
            assert isinstance(slow_res, Failed)
            self.assertEqual(slow_res.reason, "script_error")
            self.assertEqual(slow_res.trace.script_runs[0].exit_code, -1)

    def test_max_turns_exceeded_returns_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "skill"
            _write(root / "scripts" / "ping.py", "import json\nprint(json.dumps({'pong': True}))\n")
            _write(
                root / "SKILL.md",
                """---
name: loop-skill
t6:
  max_turns: 2
  tools:
    - name: ping
      script: scripts/ping.py
---
Ping.
""",
            )
            skill = Skill.load(root)
            task: Task[Notes, Summary] = Task("loop-v1", Notes, Summary, render_notes, skill=skill)
            masked, session = pseudonymize("Marco Rossi", make_registry())
            fake = (
                FakeProvider()
                .tool_call("ping", {}, prompt_tokens=5, completion_tokens=1)
                .tool_call("ping", {}, prompt_tokens=6, completion_tokens=2)
                .reply('{"headline": "too late"}')
            )
            result = run(task, Notes(masked), provider=fake, session=session)
            assert isinstance(result, Failed)
            self.assertEqual(result.reason, "max_turns_exceeded")
            self.assertEqual(len(fake.calls), 2)
            self.assertEqual(fake.pending, 1)
            self.assertEqual(result.trace.turns, 2)
            self.assertEqual(result.trace.tokens, Tokens(11, 3))
            self.assertEqual(len(result.trace.script_runs), 1)

            # max_turns: 1 fails on the first tool call without running the script.
            _write(
                root / "SKILL.md",
                """---
name: one-turn-skill
t6:
  max_turns: 1
  tools:
    - name: ping
      script: scripts/ping.py
---
Ping.
""",
            )
            one_turn_skill = Skill.load(root)
            masked1, session1 = pseudonymize("Marco Rossi", make_registry())
            res1 = run(
                Task("loop1-v1", Notes, Summary, render_notes),
                Notes(masked1),
                provider=FakeProvider().tool_call("ping", {}),
                session=session1,
                skill=one_turn_skill,
            )
            assert isinstance(res1, Failed)
            self.assertEqual(res1.reason, "max_turns_exceeded")
            self.assertEqual(res1.trace.turns, 1)
            self.assertEqual(res1.trace.script_runs, ())

    def test_multi_tool_calls_in_single_turn_and_run_skill_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root1 = Path(tmp) / "s1"
            root2 = Path(tmp) / "s2"
            _write(root1 / "SKILL.md", "---\nname: base-skill\n---\nBase.\n")
            _write(root2 / "scripts" / "echo.py", "import json, sys\nd = json.load(sys.stdin)\nprint(json.dumps({'echo': d['v']}))\n")
            _write(
                root2 / "SKILL.md",
                """---
name: override-skill
t6:
  tools:
    - name: echo
      script: scripts/echo.py
---
Override.
""",
            )
            base_skill = Skill.load(root1)
            override_skill = Skill.load(root2)
            task: Task[Notes, Summary] = Task("override-v1", Notes, Summary, render_notes, skill=base_skill)
            masked, session = pseudonymize("Marco Rossi", make_registry())
            fake = (
                FakeProvider()
                .tool_calls(
                    ToolCall("c1", "echo", {"v": "[person 1]"}),
                    ToolCall("c2", "echo", {"v": "ok"}),
                )
                .reply('{"headline": "[person 1] ok"}')
            )
            result = run(task, Notes(masked), provider=fake, session=session, skill=override_skill)
            assert isinstance(result, Ok)
            self.assertEqual(result.trace.skill_name, "override-skill")
            self.assertEqual(len(result.trace.script_runs), 2)
            second_msgs = fake.calls[1].messages
            self.assertEqual([m["role"] for m in second_msgs[-3:]], ["assistant", "tool", "tool"])
            self.assertEqual(json.loads(second_msgs[-2]["content"]), {"echo": "[person 1]"})
            self.assertEqual(json.loads(second_msgs[-1]["content"]), {"echo": "ok"})

    def test_pre_and_post_failures_and_post_schema_revalidation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "skill"
            _write(root / "scripts" / "bad_pre.py", "import sys\nsys.stderr.write('Marco Rossi bad input\\n')\nsys.exit(1)\n")
            _write(
                root / "SKILL.md",
                """---
name: bad-pre
t6:
  pre:
    - name: fail_pre
      script: scripts/bad_pre.py
---
Body.
""",
            )
            skill = Skill.load(root)
            task: Task[Notes, Summary] = Task("pre-v1", Notes, Summary, render_notes, skill=skill)
            masked, session = pseudonymize("Marco Rossi", make_registry())
            fake = FakeProvider().reply('{"headline": "unused"}')
            pre_res = run(task, Notes(masked), provider=fake, session=session)
            assert isinstance(pre_res, Failed)
            self.assertEqual(pre_res.reason, "script_error")
            self.assertNotIn("Marco", pre_res.detail)
            self.assertEqual(fake.calls, [])
            self.assertEqual(pre_res.trace.turns, 0)
            self.assertEqual([(r.name, r.phase, r.exit_code) for r in pre_res.trace.script_runs], [("fail_pre", "pre", 1)])

            # Post returning invalid schema triggers schema_violation.
            _write(
                root / "scripts" / "bad_post.py",
                "import json\nprint(json.dumps({'output': {'headline': 123}}))\n",
            )
            _write(
                root / "SKILL.md",
                """---
name: bad-post
t6:
  post:
    - name: mangle
      script: scripts/bad_post.py
---
Body.
""",
            )
            bad_post_skill = Skill.load(root)
            masked2, session2 = pseudonymize("Marco Rossi", make_registry())
            post_res = run(
                Task("post-v1", Notes, Summary, render_notes, skill=bad_post_skill),
                Notes(masked2),
                provider=FakeProvider().reply('{"headline": "[person 1]"}'),
                session=session2,
            )
            assert isinstance(post_res, Failed)
            self.assertEqual(post_res.reason, "schema_violation")
            self.assertEqual(len(post_res.trace.script_runs), 1)


class SkillLeakAndGuardTests(unittest.TestCase):
    def test_decisions_task_rejects_skill_with_type_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "skill"
            _write(root / "SKILL.md", "---\nname: s\n---\nPrompt.\n")
            skill = Skill.load(root)
            task: Task[Notes, Summary] = Task(
                "decide-v1",
                Notes,
                Summary,
                lambda n: DecisionsRequest(state={"s": n.text.text}, questions={"c0": Choice("Q?", {"match": "a", "maybe": "b", "no": "c"})}),
                skill=skill,
            )
            masked, session = pseudonymize("Marco Rossi", make_registry())
            with self.assertRaises(TypeError):
                run(task, Notes(masked), provider=FakeProvider(), session=session)

    def test_skill_system_prompt_and_tool_schema_leaks_are_rejected(self) -> None:
        cases = [
            ("body", "---\nname: leak-body\n---\nAsk Marco Rossi directly.\n", None),
            ("reference", "---\nname: leak-ref\nt6:\n  references: [references/ref.md]\n---\nClean body.\n", "Notes about Marco Rossi."),
            ("tool_desc", "---\nname: leak-desc\nt6:\n  tools:\n    - name: t\n      description: Calls Marco Rossi\n      script: scripts/t.py\n---\nok\n", None),
            ("tool_param_key", "---\nname: leak-key\nt6:\n  tools:\n    - name: t\n      script: scripts/t.py\n      parameters:\n        type: object\n        properties:\n          Marco:\n            type: string\n---\nok\n", None),
            ("tool_name", "---\nname: leak-name\nt6:\n  tools:\n    - name: Marco\n      script: scripts/t.py\n---\nok\n", None),
        ]
        for label, skill_md, ref_text in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / "skill"
                _write(root / "scripts" / "t.py", "print('{}')\n")
                if ref_text is not None:
                    _write(root / "references" / "ref.md", ref_text)
                _write(root / "SKILL.md", skill_md)
                skill = Skill.load(root)
                masked, session = pseudonymize("hello", make_registry())
                fake = FakeProvider().reply('{"headline": "x"}')
                result = run(Task("leak-v1", Notes, Summary, render_notes), Notes(masked), provider=fake, session=session, skill=skill)
                assert isinstance(result, Failed)
                self.assertEqual(result.reason, "leak")
                self.assertNotIn("Marco", result.detail)
                self.assertEqual(fake.calls, [])

    def test_tool_call_argument_leak_and_unresolved_placeholder_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "skill"
            marker = Path(tmp) / "ran.txt"
            _write(root / "scripts" / "t.py", f"import pathlib\npathlib.Path({str(marker)!r}).write_text('ran')\nprint('{{}}')\n")
            _write(root / "SKILL.md", "---\nname: s\nt6:\n  tools:\n    - name: t\n      script: scripts/t.py\n---\nok\n")
            skill = Skill.load(root)
            task: Task[Notes, Summary] = Task("arg-v1", Notes, Summary, render_notes, skill=skill)

            masked1, session1 = pseudonymize("Marco Rossi", make_registry())
            res_leak = run(
                task,
                Notes(masked1),
                provider=FakeProvider().tool_call("t", {"who": "Marco Rossi"}),
                session=session1,
            )
            assert isinstance(res_leak, Failed)
            self.assertEqual(res_leak.reason, "leak")
            self.assertNotIn("Marco", res_leak.detail)
            self.assertFalse(marker.exists())

            masked2, session2 = pseudonymize("Marco Rossi", make_registry())
            res_unresolved = run(
                task,
                Notes(masked2),
                provider=FakeProvider().tool_call("t", {"who": "[person 9]"}),
                session=session2,
            )
            assert isinstance(res_unresolved, Failed)
            self.assertEqual(res_unresolved.reason, "unresolved_placeholder")
            self.assertFalse(marker.exists())


class ReusableHelperTests(unittest.TestCase):
    def test_helpers_mask_format_and_handle_unknown_tool(self) -> None:
        _, session = pseudonymize("Marco Rossi", make_registry())
        masked_text = mask_for_model("Marco Rossi password: secret", session)
        self.assertIn("[person 1]", masked_text)
        self.assertNotIn("Marco", masked_text)
        self.assertNotIn("secret", masked_text)

        from t6.llm.transport import Completion

        comp = Completion(
            content=None,
            model="m",
            tool_calls=(ToolCall("c1", "lookup", {"who": "[person 1]"}),),
        )
        msg = assistant_tool_message(comp)
        self.assertEqual(msg["role"], "assistant")
        self.assertEqual(msg["content"], "")
        self.assertEqual(msg["tool_calls"][0]["function"]["name"], "lookup")
        self.assertEqual(json.loads(msg["tool_calls"][0]["function"]["arguments"]), {"who": "[person 1]"})

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "skill"
            _write(root / "SKILL.md", "---\nname: empty\n---\nok\n")
            skill = Skill.load(root)
            outcome = execute_tool_call(ToolCall("c1", "missing", {}), skill, session=session)
            self.assertIsNone(outcome.failure_reason)
            self.assertIsNone(outcome.script_run)
            assert outcome.message is not None
            self.assertEqual(json.loads(outcome.message["content"]), {"error": "unknown tool: missing"})


if __name__ == "__main__":
    unittest.main()
