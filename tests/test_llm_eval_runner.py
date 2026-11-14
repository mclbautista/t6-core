import tempfile
import unittest
from pathlib import Path

from t6.llm.evals import Attempt, Case, RunRecord, run_evals
from t6.llm.testing import FakeProvider
from t6.llm.transport import OpenAIChat


def case(number):
    return Case(id=str(number), task="test", masked_input={}, expected_properties=["valid"],
                grader="code", provenance={"kind": "synthetic"})


def record():
    return RunRecord(model_id="fake", provider="fake", prompt_hash="p", schema_hash="s",
                     t6_version="1", date="2026-11-14", results=[])


def evaluate(c, provider):
    completion = provider.complete([{"role": "user", "content": c.id}], timeout=1)
    return Attempt(completion.content == "yes", completion.content or "empty", completion.tokens.total / 100, completion.content == "review")


def script(*answers):
    fake = FakeProvider()
    for answer in answers:
        fake.reply(answer, prompt_tokens=1)
    return fake


class RunnerTests(unittest.TestCase):
    def run_cases(self, cases, fake, **kwargs):
        return run_evals(cases, record=record(), provider=fake, evaluate=evaluate, cost_cap=kwargs.pop("cost_cap", 10), **kwargs)

    def test_majority_and_judge(self):
        result = self.run_cases([case(1), case(2)], script("yes", "yes", "review", "yes", "review", "no"))
        self.assertEqual([r.passed for r in result.results], [True, False])
        self.assertEqual(result.human_review, ["1", "2"])
        self.assertEqual(result.status, "failed")

    def test_changes_regression_and_acceptance_with_pass_bar(self):
        cases = [case(i) for i in range(10)]
        good = self.run_cases(cases, script(*(["yes"] * 30)))
        bad_script = ["no"] * 3 + ["yes"] * 27
        change = self.run_cases(cases, script(*bad_script), history=[good])
        self.assertEqual(change.changed_failures, ["0"])
        self.assertEqual(change.status, "failed")
        regression = self.run_cases(cases, script(*bad_script), history=[good, change])
        self.assertEqual(regression.regressions, ["0"])
        self.assertEqual(regression.status, "failed")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.json"
            accepted = self.run_cases(cases, script(*bad_script), history=[regression], accept={"0": "Cyril"}, path=path)
            self.assertEqual(accepted.status, "passed")
            self.assertEqual(RunRecord.read(path).accepted_regressions, {"0": "Cyril"})
            again = self.run_cases(cases, script(*bad_script), history=[accepted])
            self.assertEqual(again.status, "passed")
        must = self.run_cases(cases, script(*bad_script), history=[regression], accept={"0": "Cyril"}, must_pass={"0"})
        self.assertEqual(must.status, "failed")

    def test_cost_cap_and_offline(self):
        fake = script("yes", "yes", "yes")
        result = self.run_cases([case(1)], fake, cost_cap=0.02)
        self.assertEqual(len(fake.calls), 2)
        self.assertTrue(result.cost_cap_reached)
        self.assertEqual(result.status, "incomplete")
        fake = script("yes")
        result = self.run_cases([case(1)], fake, offline=True, offline_evaluate=lambda c: Attempt(True, "fixture"))
        self.assertEqual(fake.calls, [])
        self.assertEqual(result.status, "offline")

    def test_temperature_for_chat(self):
        import json
        bodies = []
        def send(url, headers, body, timeout):
            bodies.append(json.loads(body))
            return b'{"choices":[{"message":{"content":"yes"}}]}'
        provider = OpenAIChat(base_url="https://example.invalid", api_key="x", model="m", send=send)
        self.run_cases([case(1)], provider)
        self.assertEqual([b["temperature"] for b in bodies], [0, 0, 0])
