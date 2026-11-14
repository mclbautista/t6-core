"""Public catalogue, CLI and outcome contracts for studio verbs."""
import argparse
import contextlib
import io
import json
import re
import unittest
from typing import Any
from unittest.mock import patch
from pydantic import BaseModel

from t6.cli import main
from t6.verbs import Verb, add_parsers, Ambiguous, Done, NeedsPerson, Preview, Refused, Outcome, outcome_json


class VerbTests(unittest.TestCase):
    def test_catalogue_matches_registered_cli_help(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["commands", "--json"]), 0)
        catalogue = json.loads(output.getvalue())
        pairs = {(entry["noun"], entry["verb"]) for entry in catalogue}
        def help_text(args: list[str]) -> str:
            text = io.StringIO()
            with contextlib.redirect_stdout(text), self.assertRaises(SystemExit):
                main([*args, "--help"])
            return text.getvalue()

        root = help_text([])
        aliases = {alias.split()[-1] for entry in catalogue for alias in entry["aliases"]}
        from_help: set[tuple[str, str]] = set()
        for noun in {noun for noun, _ in pairs}:
            self.assertRegex(root, rf"\b{re.escape(noun)}\b")
            noun_pairs = {(n, verb) for n, verb in pairs if n == noun}
            if (noun, "") in noun_pairs:
                from_help.add((noun, ""))
                continue
            match = re.search(r"\{([^{}]+)\}", help_text([noun]))
            self.assertIsNotNone(match)
            assert match is not None
            from_help.update((noun, verb) for verb in match.group(1).split(",") if verb not in aliases or (noun, verb) in pairs)
        self.assertEqual(from_help, pairs)

        for noun, verb in pairs:
            output = io.StringIO()
            with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as exit:
                main([noun, verb, "--help"] if verb else [noun, "--help"])
            self.assertEqual(exit.exception.code, 0)

    def test_activity_finds_all_rotations_and_reports_ambiguity(self) -> None:
        import os
        import tempfile
        from pathlib import Path
        from t6.registry import Rotation, SqliteProjectRegistry
        from t6.verbs import ActivityInput, project_activity

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registry"
            registry = SqliteProjectRegistry(path)
            registry.register(actor="test", idempotency_key="out", code="ABC", rotation=Rotation.OUT)
            with patch.dict(os.environ, {"T6_REGISTRY_DB": str(path)}):
                self.assertIsInstance(project_activity(ActivityInput(project="ABC"), lambda: None), Done)
                registry.register(actor="test", idempotency_key="unclassified", code="ABC", rotation=Rotation.UNCLASSIFIED)
                self.assertIsInstance(project_activity(ActivityInput(project="ABC"), lambda: None), Ambiguous)

    def test_optional_boolean_and_list_arguments(self) -> None:
        class Input(BaseModel):
            name: str | None = None
            enabled: bool = False
            tags: list[str] = []

        entry = Verb("example", "run", "example", (), Input, Input, "reader", "", lambda *_: Done(result=None))
        parser = argparse.ArgumentParser()
        with patch("t6.verbs.VERBS", (entry,)):
            add_parsers(parser.add_subparsers(dest="command", required=True))
        args = parser.parse_args(["example", "run", "--name", "hi", "--enabled", "false", "--tags", "a", "b"])
        self.assertEqual((args.name, args.enabled, args.tags), ("hi", False, ["a", "b"]))
        self.assertIsNone(parser.parse_args(["example", "run"]).name)

    def test_outcomes_have_documented_codes_and_json_shapes(self) -> None:
        examples: list[tuple[Outcome, int, dict[str, Any]]] = [
            (Done(result={"id": "1"}), 0, {"outcome": "done", "result": {"id": "1"}}),
            (Preview(plan={"step": "check"}, token="abc"), 0, {"outcome": "preview", "plan": {"step": "check"}, "token": "abc"}),
            (NeedsPerson(reason="choose"), 3, {"outcome": "needs_person", "reason": "choose"}),
            (Refused(rule="permission", reason="denied"), 2, {"outcome": "refused", "rule": "permission", "reason": "denied"}),
            (Ambiguous(candidates=["a", "b"]), 4, {"outcome": "ambiguous", "candidates": ["a", "b"]}),
        ]
        for outcome, code, shape in examples:
            with self.subTest(outcome=outcome.outcome):
                self.assertEqual(outcome_json(outcome), (code, shape))
                json.dumps(shape)


if __name__ == "__main__":
    unittest.main()
