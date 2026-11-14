import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from t6.llm.evals import Case, RunRecord


class EvalTypesTests(unittest.TestCase):
    def test_case_requires_grader_and_provenance(self) -> None:
        case = {
            "id": "blocker-1", "task": "brief", "masked_input": {"text": "[PERSON_1]"},
            "expected_properties": ["raises a blocker Concern"],
            "grader": "blocker-check", "provenance": {"kind": "synthetic"},
        }
        self.assertEqual(Case.model_validate(case).grader, "blocker-check")
        for field in case:
            with self.subTest(field=field), self.assertRaises(ValidationError):
                Case.model_validate({k: v for k, v in case.items() if k != field})
        for field in ("id", "task", "grader"):
            with self.subTest(empty=field), self.assertRaises(ValidationError):
                Case.model_validate({**case, field: "  "})
        with self.assertRaises(ValidationError):
            Case.model_validate({**case, "expected_properties": []})
        for provenance in ({"kind": "real"}, {"kind": "real", "reviewer": "Reviewer"}):
            with self.subTest(provenance=provenance), self.assertRaises(ValidationError):
                Case.model_validate({**case, "provenance": provenance})
        real = Case.model_validate({**case, "provenance": {
            "kind": "real", "reviewer": "Reviewer", "date": "2026-11-14",
        }})
        self.assertEqual(real.provenance.kind, "real")

    def test_run_record_file_round_trips_with_required_fields(self) -> None:
        data: dict[str, Any] = {
            "model_id": "model-1", "provider": "provider-1", "prompt_hash": "abc",
            "schema_hash": "def", "t6_version": "0.3.4", "date": "2026-11-14",
            "results": [{"case_id": "blocker-1", "passed": False, "reason": "no blocker"}],
        }
        record = RunRecord.model_validate(data)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.json"
            record.write(path)
            self.assertEqual(json.loads(path.read_text()), data)
            self.assertEqual(RunRecord.read(path), record)
        for field in data:
            with self.subTest(field=field), self.assertRaises(ValidationError):
                RunRecord.model_validate({k: v for k, v in data.items() if k != field})
        for field in ("case_id", "passed", "reason"):
            with self.subTest(result_field=field), self.assertRaises(ValidationError):
                RunRecord.model_validate({**data, "results": [
                    {k: v for k, v in data["results"][0].items() if k != field}
                ]})


if __name__ == "__main__":
    unittest.main()
