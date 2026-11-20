"""Unit tests for t6.llm.skill: SKILL.md loader, stdlib YAML fallback, path confinement, and run_script."""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from t6.llm import (
    OnError,
    ScriptHook,
    ScriptOutcome,
    Skill,
    SkillError,
    ToolScript,
    ToolSpec,
    run_script,
)
from t6.llm.skill import _parse_simple_yaml


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _build_sample_skill(root: Path, *, script_body: str = "import json, sys\nprint(json.dumps({'echo': json.load(sys.stdin)}))\n") -> Path:
    _write(root / "references" / "rules.md", "Rule 1: keep names masked.")
    _write(root / "references" / "examples.md", "Example: [person 1] -> ok.")
    _write(
        root / "schemas" / "out.json",
        json.dumps(
            {
                "type": "object",
                "properties": {"answer": {"type": "string"}},
                "required": ["answer"],
            }
        ),
    )
    _write(root / "scripts" / "pre.py", script_body)
    _write(root / "scripts" / "lookup.py", script_body)
    _write(root / "scripts" / "post.py", script_body)
    _write(
        root / "SKILL.md",
        """---
name: sample-skill
description: "Sample skill #1 with quotes"
version: 1.0.0
hermes:
  tags: [t6, skill]
  related_skills: [other]
metadata:
  {
    "hermes": {
      "category": "ops",
      "enabled": true
    }
  }
t6:
  references:
    - references/rules.md
    - references/examples.md
  output_schema: schemas/out.json
  max_turns: 4 # cap turns
  env:
    - T6_SKILL_FLAG
  pre:
    - name: normalize
      script: scripts/pre.py
      timeout: 5
      on_error: fail
  tools:
    - name: lookup_item
      description: 'Look up ''item'' details'
      script: scripts/lookup.py
      parameters:
        type: object
        properties:
          query: {type: string, minLength: 1}
          limit: {type: integer, default: 5}
        required: [query]
      timeout: 7.5
      on_error: return_to_model
  post:
    - name: verify
      script: scripts/post.py
---

# Sample Skill

Use `lookup_item` when details are missing.
""",
    )
    return root


class SkillLoaderTests(unittest.TestCase):
    def test_loads_t6_frontmatter_alongside_hermes_and_matches_stdlib_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = _build_sample_skill(Path(tmp))
            with_yaml = Skill.load(skill_dir)
            with mock.patch.dict(sys.modules, {"yaml": None}):
                without_yaml = Skill.load(skill_dir)

        self.assertEqual(with_yaml, without_yaml)
        self.assertEqual(with_yaml.skill_hash, without_yaml.skill_hash)
        self.assertEqual(len(with_yaml.skill_hash), 64)
        self.assertEqual(with_yaml.name, "sample-skill")
        self.assertEqual(with_yaml.description, "Sample skill #1 with quotes")
        self.assertEqual(with_yaml.max_turns, 4)
        self.assertEqual(with_yaml.env, ("T6_SKILL_FLAG",))
        self.assertEqual(
            with_yaml.references,
            (
                ("references/rules.md", "Rule 1: keep names masked."),
                ("references/examples.md", "Example: [person 1] -> ok."),
            ),
        )
        self.assertEqual(
            with_yaml.output_schema,
            {
                "type": "object",
                "properties": {"answer": {"type": "string"}},
                "required": ["answer"],
            },
        )
        self.assertEqual(
            with_yaml.pre,
            (ScriptHook(name="normalize", script=Path("scripts/pre.py"), timeout=5.0, on_error="fail"),),
        )
        self.assertEqual(
            with_yaml.tools,
            (
                ToolScript(
                    name="lookup_item",
                    description="Look up 'item' details",
                    script=Path("scripts/lookup.py"),
                    parameters={
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "minLength": 1},
                            "limit": {"type": "integer", "default": 5},
                        },
                        "required": ["query"],
                    },
                    timeout=7.5,
                    on_error="return_to_model",
                ),
            ),
        )
        self.assertEqual(
            with_yaml.post,
            (ScriptHook(name="verify", script=Path("scripts/post.py"), timeout=15.0, on_error="fail"),),
        )
        prompt = with_yaml.system_prompt()
        self.assertIn("# Sample Skill", prompt)
        self.assertIn("Rule 1: keep names masked.", prompt)
        self.assertIn("Example: [person 1] -> ok.", prompt)

        spec = with_yaml.tools[0].to_spec()
        self.assertEqual(
            spec,
            ToolSpec(
                name="lookup_item",
                description="Look up 'item' details",
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "minLength": 1},
                        "limit": {"type": "integer", "default": 5},
                    },
                    "required": ["query"],
                },
            ),
        )

    def test_exports_and_minimal_skill_without_t6_section(self) -> None:
        import t6.llm
        from typing import get_args

        expected = {"OnError", "ScriptHook", "ScriptOutcome", "Skill", "SkillError", "ToolScript", "run_script"}
        self.assertTrue(expected.issubset(set(t6.llm.__all__)))
        self.assertEqual(set(get_args(OnError)), {"fail", "return_to_model"})

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "plain-skill"
            _write(root / "SKILL.md", "---\nhermes:\n  tags: [ops]\n---\nOnly instructions.\n")
            skill = Skill.load(root)
            self.assertEqual(skill.name, "plain-skill")
            self.assertEqual(skill.instructions, "Only instructions.")
            self.assertEqual(skill.system_prompt(), "Only instructions.")
            self.assertIsNone(skill.output_schema)
            self.assertEqual(skill.tools, ())

    def test_rejects_invalid_output_schema_json_or_jsonschema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "schemas" / "bad.json", "{not json")
            _write(root / "SKILL.md", "---\nt6:\n  output_schema: schemas/bad.json\n---\nbody\n")
            with self.assertRaises(SkillError):
                Skill.load(root)

            _write(root / "schemas" / "bad_schema.json", json.dumps({"type": "not-a-json-type"}))
            _write(root / "SKILL.md", "---\nt6:\n  output_schema: schemas/bad_schema.json\n---\nbody\n")
            with self.assertRaises(SkillError):
                Skill.load(root)

    def test_skill_hash_changes_when_script_or_reference_or_schema_changes(self) -> None:
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            dir_a = _build_sample_skill(Path(a))
            dir_b = _build_sample_skill(Path(b))
            base_hash = Skill.load(dir_a).skill_hash
            self.assertEqual(base_hash, Skill.load(dir_b).skill_hash)

            # Edit script content only (SKILL.md unchanged) -> skill_hash must change.
            _write(dir_b / "scripts" / "lookup.py", "print('changed')\n")
            self.assertNotEqual(base_hash, Skill.load(dir_b).skill_hash)

            # Edit reference content only -> skill_hash must change.
            dir_b = _build_sample_skill(Path(b))
            _write(dir_b / "references" / "rules.md", "Rule 1: updated.")
            self.assertNotEqual(base_hash, Skill.load(dir_b).skill_hash)

    def test_stdlib_yaml_matches_pyyaml_on_complex_constructs(self) -> None:
        import yaml

        yaml_text = """
name: complex # inline comment
url: https://example.com#anchor
flag_yes: yes
flag_false: false
nothing: ~
count: -7
rate: 1.25
block_lit: |
  first
  second
block_fold: >
  hello
  world

  next
items:
- plain
- name: inline_map
  nested:
    arr: [1, true, null, "x: y"]
  flow_multiline: {
    "a": [10, 20],
    "b": false
  }
"""
        self.assertEqual(_parse_simple_yaml(yaml_text), yaml.safe_load(yaml_text))

    def test_rejects_malformed_frontmatter_and_unknown_t6_keys(self) -> None:
        cases = {
            "missing_opening": "# No frontmatter\n",
            "unclosed": "---\nname: x\n",
            "non_mapping_root": "---\n- one\n- two\n---\nbody\n",
            "non_mapping_t6": "---\nt6: invalid\n---\nbody\n",
            "unknown_t6_key": "---\nt6:\n  bogus: 1\n---\nbody\n",
            "unknown_hook_key": "---\nt6:\n  pre:\n    - name: a\n      script: scripts/a.py\n      extra: 1\n---\n",
            "unknown_tool_key": "---\nt6:\n  tools:\n    - name: a\n      script: scripts/a.py\n      bogus: 1\n---\n",
            "bad_max_turns_zero": "---\nt6:\n  max_turns: 0\n---\n",
            "bad_max_turns_bool": "---\nt6:\n  max_turns: true\n---\n",
            "bad_on_error": "---\nt6:\n  pre:\n    - name: a\n      script: scripts/a.py\n      on_error: ignore\n---\n",
            "bad_timeout": "---\nt6:\n  pre:\n    - name: a\n      script: scripts/a.py\n      timeout: 0\n---\n",
            "bad_env_equals": "---\nt6:\n  env: [FOO=bar]\n---\n",
            "duplicate_tools": "---\nt6:\n  tools:\n    - name: dup\n      script: scripts/a.py\n    - name: dup\n      script: scripts/a.py\n---\n",
        }
        for label, content in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                _write(root / "scripts" / "a.py", "print('ok')\n")
                _write(root / "SKILL.md", content)
                with self.assertRaises(SkillError):
                    Skill.load(root)

    def test_rejects_missing_files_and_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outside = Path(tmp) / "outside.txt"
            _write(outside, "secret")
            outside_script = Path(tmp) / "outside.py"
            _write(outside_script, "print('pwn')")
            skill_dir = Path(tmp) / "skill"
            skill_dir.mkdir()

            traversal_cases = [
                ("ref_dotdot", "references: [../outside.txt]"),
                ("ref_abs", f"references: [{outside}]"),
                ("ref_missing", "references: [references/missing.md]"),
                ("schema_dotdot", "output_schema: ../outside.txt"),
                ("schema_missing", "output_schema: schemas/missing.json"),
                ("pre_dotdot", "pre:\n    - name: p\n      script: ../outside.py"),
                ("tool_missing", "tools:\n    - name: t\n      script: scripts/missing.py"),
                ("post_dotdot", "post:\n    - name: p\n      script: ../outside.py"),
            ]
            for label, block in traversal_cases:
                with self.subTest(label=label):
                    _write(skill_dir / "SKILL.md", f"---\nt6:\n  {block}\n---\nbody\n")
                    with self.assertRaises(SkillError):
                        Skill.load(skill_dir)

            # Symlink pointing outside the skill root must also be rejected.
            refs_dir = skill_dir / "references"
            refs_dir.mkdir(exist_ok=True)
            link_path = refs_dir / "escape.md"
            link_path.symlink_to(outside)
            _write(skill_dir / "SKILL.md", "---\nt6:\n  references: [references/escape.md]\n---\nbody\n")
            with self.assertRaises(SkillError):
                Skill.load(skill_dir)


class RunScriptTests(unittest.TestCase):
    def test_run_script_passes_utf8_json_stdin_captures_stdout_and_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = _build_sample_skill(
                Path(tmp),
                script_body=(
                    "import json, os, sys\n"
                    "data = json.load(sys.stdin)\n"
                    "print(json.dumps({'cwd': os.getcwd(), 'received': data}, ensure_ascii=False))\n"
                ),
            )
            skill = Skill.load(skill_dir)
            outcome = run_script(
                skill.tools[0],
                {"name": "José", "count": 2},
                skill_dir=skill.root,
                env_names=skill.env,
            )
            self.assertIsInstance(outcome, ScriptOutcome)
            self.assertTrue(outcome.ok)
            self.assertEqual(outcome.exit_code, 0)
            self.assertEqual(outcome.error, "")
            self.assertGreaterEqual(outcome.duration_ms, 0)
            parsed = json.loads(outcome.stdout)
            self.assertEqual(parsed["cwd"], str(skill_dir.resolve()))
            self.assertEqual(parsed["received"], {"name": "José", "count": 2})

    def test_sanitizes_env_strips_forbidden_and_blocked_keys_and_allows_only_listed_t6_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = Path(tmp)
            _write(
                skill_dir / "scripts" / "env_dump.py",
                "import json, os\nprint(json.dumps(dict(os.environ)))\n",
            )
            _write(
                skill_dir / "SKILL.md",
                """---
name: env-skill
t6:
  env:
    - ALLOWED_VAR
    - OPENROUTER_API_KEY
    - CUSTOM_PROVIDER_KEY
  tools:
    - name: dump_env
      script: scripts/env_dump.py
---
body
""",
            )
            skill = Skill.load(skill_dir)
            fake_environ = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "ALLOWED_VAR": "from-os",
                "UNLISTED_VAR": "secret-unlisted",
                "OPENROUTER_API_KEY": "sk-or-secret",
                "DEEPSEEK_API_KEY": "sk-ds-secret",
                "T6_NOTION_TOKEN": "ntn-secret",
                "CUSTOM_PROVIDER_KEY": "sk-custom",
            }
            with mock.patch.dict(os.environ, fake_environ, clear=True):
                outcome = run_script(
                    skill.tools[0],
                    {},
                    skill_dir=skill.root,
                    env_names=skill.env,
                    blocked_env=("CUSTOM_PROVIDER_KEY",),
                )
            self.assertTrue(outcome.ok, outcome.error)
            child_env = json.loads(outcome.stdout)
            self.assertEqual(child_env.get("SKILL_DIR"), str(skill_dir.resolve()))
            self.assertEqual(child_env.get("ALLOWED_VAR"), "from-os")
            for forbidden in (
                "UNLISTED_VAR",
                "OPENROUTER_API_KEY",
                "DEEPSEEK_API_KEY",
                "T6_NOTION_TOKEN",
                "CUSTOM_PROVIDER_KEY",
            ):
                self.assertNotIn(forbidden, child_env)

            # Explicit allowed_env mapping overrides os.environ lookup for allowlisted keys only.
            outcome_explicit = run_script(
                skill.tools[0],
                {},
                skill_dir=skill.root,
                env_names=skill.env,
                allowed_env={"ALLOWED_VAR": "from-explicit", "UNLISTED_VAR": "nope", "OPENROUTER_API_KEY": "sk-no"},
                blocked_env=("CUSTOM_PROVIDER_KEY",),
            )
            self.assertTrue(outcome_explicit.ok)
            child_explicit = json.loads(outcome_explicit.stdout)
            self.assertEqual(child_explicit.get("ALLOWED_VAR"), "from-explicit")
            self.assertNotIn("UNLISTED_VAR", child_explicit)
            self.assertNotIn("OPENROUTER_API_KEY", child_explicit)

    def test_timeout_and_nonzero_exit_return_failed_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = Path(tmp)
            _write(skill_dir / "scripts" / "slow.py", "import time\ntime.sleep(2.0)\n")
            _write(skill_dir / "scripts" / "fail.py", "import sys\nprint('partial')\nsys.stderr.write('boom\\n')\nsys.exit(3)\n")

            timed_out = run_script(Path("scripts/slow.py"), {}, skill_dir=skill_dir, timeout=0.05)
            self.assertFalse(timed_out.ok)
            self.assertEqual(timed_out.exit_code, -1)
            self.assertIn("timed out", timed_out.error)

            failed = run_script(Path("scripts/fail.py"), {}, skill_dir=skill_dir)
            self.assertFalse(failed.ok)
            self.assertEqual(failed.exit_code, 3)
            self.assertEqual(failed.stdout.strip(), "partial")
            self.assertIn("boom", failed.error)

    def test_run_script_rejects_traversal_and_missing_script_and_handles_executable_shell(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outside = Path(tmp) / "outside.py"
            _write(outside, "print('nope')\n")
            skill_dir = Path(tmp) / "skill"
            sh_script = skill_dir / "scripts" / "hello.sh"
            _write(sh_script, "#!/bin/sh\ncat\n")
            sh_script.chmod(sh_script.stat().st_mode | stat.S_IXUSR)

            with self.assertRaises(SkillError):
                run_script(Path("../outside.py"), {}, skill_dir=skill_dir)
            with self.assertRaises(SkillError):
                run_script(Path("scripts/missing.py"), {}, skill_dir=skill_dir)

            outcome = run_script(Path("scripts/hello.sh"), {"ok": True}, skill_dir=skill_dir)
            self.assertTrue(outcome.ok, outcome.error)
            self.assertEqual(json.loads(outcome.stdout), {"ok": True})


if __name__ == "__main__":
    unittest.main()
