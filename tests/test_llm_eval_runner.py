import tempfile
import unittest
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from pathlib import Path

from t6.llm.evals import Attempt, Case, RunRecord, SyntheticProvenance, run_evals
from t6.llm.testing import FakeProvider
from t6.llm.transport import OpenAIChat, Provider


def case(number: int) -> Case:
    return Case(id=str(number), task="test", masked_input={}, expected_properties=["valid"],
                grader="code", provenance=SyntheticProvenance(kind="synthetic"))


def record() -> RunRecord:
    return RunRecord(model_id="fake", provider="fake", prompt_hash="p", schema_hash="s",
                     t6_version="1", date=date(2026, 11, 14), results=[])


def evaluate(c: Case, provider: Provider) -> Attempt:
    completion = provider.complete([{"role": "user", "content": c.id}], timeout=1)
    return Attempt(completion.content == "yes", completion.content or "empty", completion.tokens.total / 100, completion.content == "review")


def script(*answers: str) -> FakeProvider:
    fake = FakeProvider()
    for answer in answers:
        fake.reply(answer, prompt_tokens=1)
    return fake


class RunnerTests(unittest.TestCase):
    def run_cases(
        self, cases: Sequence[Case], fake: Provider, *, cost_cap: float = 10,
        history: Sequence[RunRecord] = (), accept: Mapping[str, str] | None = None,
        path: str | Path | None = None, must_pass: set[str] | frozenset[str] = frozenset(),
        offline: bool = False, offline_evaluate: Callable[[Case], Attempt] | None = None,
    ) -> RunRecord:
        return run_evals(cases, record=record(), provider=fake, evaluate=evaluate, cost_cap=cost_cap,
                         history=history, accept=accept, path=path, must_pass=must_pass,
                         offline=offline, offline_evaluate=offline_evaluate)

    def test_majority_and_judge(self) -> None:
        result = self.run_cases([case(1), case(2)], script("yes", "yes", "review", "yes", "review", "no"))
        self.assertEqual([r.passed for r in result.results], [True, False])
        self.assertEqual(result.human_review, ["1", "2"])
        self.assertEqual(result.status, "failed")

    def test_changes_regression_and_acceptance_with_pass_bar(self) -> None:
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

    def test_cost_cap_and_offline(self) -> None:
        fake = script("yes", "yes", "yes")
        result = self.run_cases([case(1)], fake, cost_cap=0.02)
        self.assertEqual(len(fake.calls), 2)
        self.assertTrue(result.cost_cap_reached)
        self.assertEqual(result.status, "incomplete")
        fake = script("yes")
        result = self.run_cases([case(1)], fake, offline=True, offline_evaluate=lambda c: Attempt(True, "fixture"))
        self.assertEqual(fake.calls, [])
        self.assertEqual(result.status, "offline")

    def test_temperature_for_chat(self) -> None:
        import json
        bodies: list[dict[str, object]] = []
        def send(url: str, headers: dict[str, str], body: bytes, timeout: float) -> bytes:
            bodies.append(json.loads(body))
            return b'{"choices":[{"message":{"content":"yes"}}]}'
        provider = OpenAIChat(base_url="https://example.invalid", api_key="x", model="m", send=send)
        self.run_cases([case(1)], provider)
        self.assertEqual([b["temperature"] for b in bodies], [0, 0, 0])
