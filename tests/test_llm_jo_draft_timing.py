"""The `jo draft` timing command: FakeProvider only, no live calls (spec #791, ticket #792)."""

import json
import unittest
from typing import Any

from t6.llm.jo_draft_timing import MAX_CANDIDATES, measure, summarize
from t6.llm.testing import FakeProvider
from t6.llm.transport import DecisionsRequest
from t6.privacy import Person, Project, Registry

REGISTRY = Registry(people=[Person("Maya Reyes", aliases=["Maya"])], projects=[Project("Harbor Lights", code="HBR")])
LINES: list[dict[str, Any]] = [
    {"line": "HBR ftr export seq 5 to transfer link, for Maya",
     "candidates": [{"task": "Export Harbor Lights FTR for Maya", "purpose": "client review",
                     "mode": "PIX, password: hunter2"}] * 14},
    {"line": "HBR tlr color pass, for Maya", "candidates": []},
]


def answer(choice: str = "match") -> dict[str, Any]:
    return {"type": "choice", "choice": choice, "probabilities": {"match": 0.8, "maybe": 0.1, "no": 0.1}}


def jev(keys: int) -> dict[str, Any]:
    return {"model": "typesafe/jev-1.1", "answers": {f"c{i}": answer() for i in range(keys)}, "usage": {"input_tokens": 50, "output_tokens": 4}}


HAIKU_REPLY = '{"task": "Export", "purpose": "(?)", "notes": "(?)"}'


class TimingTests(unittest.TestCase):
    def run_all(self, *, lookups: FakeProvider | None = None) -> tuple[list[Any], FakeProvider, FakeProvider, FakeProvider]:
        match = FakeProvider("~typesafe/jev-latest").decisions(jev(MAX_CANDIDATES)).decisions(jev(1))
        look = lookups or FakeProvider("~typesafe/jev-latest").decisions(jev(2)).decisions(jev(2))
        haiku = FakeProvider("haiku").reply(HAIKU_REPLY, prompt_tokens=30, completion_tokens=9).reply(HAIKU_REPLY)
        return measure(LINES, REGISTRY, jev_match=match, jev_lookups=look, haiku=haiku), match, look, haiku

    def test_three_calls_per_line_with_summary_numbers(self) -> None:
        rows, *_ = self.run_all()
        self.assertEqual([[t.step for t in row] for row in rows], [["jev_match", "jev_lookups", "haiku"]] * 2)
        summary = summarize(rows)
        self.assertEqual(summary["lines"], 2)
        for step in ("jev_match", "jev_lookups", "haiku", "all_three"):
            self.assertLessEqual(summary[step]["median_ms"], summary[step]["worst_ms"])
        self.assertEqual(summary["haiku"]["failed"], 0)
        self.assertEqual(summary["jev_match"]["cost_usd"], "unknown")

    def test_only_masked_text_without_credentials_is_sent_and_twelve_candidates_at_most(self) -> None:
        _, match, look, haiku = self.run_all()
        first = match.calls[0].request
        assert isinstance(first, DecisionsRequest)
        self.assertEqual(len(first.questions), MAX_CANDIDATES)
        sent = repr([[c.request for c in p.calls] for p in (match, look, haiku)])
        for secret in ("Maya", "Reyes", "Harbor", "hunter2"):
            self.assertNotIn(secret, sent)

    def test_output_holds_no_line_text_or_names(self) -> None:
        rows, *_ = self.run_all()
        text = json.dumps(summarize(rows))
        for word in ("Maya", "Harbor", "transfer link", "export"):
            self.assertNotIn(word, text)

    def test_a_failed_call_is_a_row_with_its_reason(self) -> None:
        look = FakeProvider("~typesafe/jev-latest").timeout().timeout().timeout().timeout()
        rows, *_ = self.run_all(lookups=look)
        self.assertEqual(rows[0][1].failure, "timeout")
        self.assertEqual(summarize(rows)["jev_lookups"]["failed"], 2)


if __name__ == "__main__":
    unittest.main()
