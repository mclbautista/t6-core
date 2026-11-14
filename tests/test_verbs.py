"""Public catalogue, CLI and outcome contracts for studio verbs."""
import contextlib
import io
import json
import unittest
from typing import Any

from t6.cli import main
from t6.verbs import Ambiguous, Done, NeedsPerson, Preview, Refused, Outcome, outcome_json


class VerbTests(unittest.TestCase):
    def test_catalogue_matches_registered_cli_help(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["commands", "--json"]), 0)
        catalogue = json.loads(output.getvalue())
        self.assertEqual([entry["noun"] for entry in catalogue], ["project", "departments"])
        entry = catalogue[1]
        self.assertIn("data_sources", entry["input_schema"]["properties"])
        self.assertIn("departments", entry["output_schema"]["properties"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as exit:
            main(["--help"])
        self.assertEqual(exit.exception.code, 0)
        # Registered commands have an explicit marker in help, separate from legacy commands.
        help_verbs = [line.strip().split()[0] for line in output.getvalue().splitlines() if "[verb]" in line]
        self.assertEqual(help_verbs, [entry["noun"] for entry in catalogue if not entry["verb"]])
        for noun in {entry["noun"] for entry in catalogue if entry["verb"]}:
            output = io.StringIO()
            with contextlib.redirect_stdout(output), self.assertRaises(SystemExit):
                main([noun, "--help"])
            help_verbs = [line.strip().split()[0] for line in output.getvalue().splitlines() if "[verb]" in line]
            self.assertEqual(help_verbs, [entry["verb"] for entry in catalogue if entry["noun"] == noun])

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
