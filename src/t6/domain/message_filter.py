"""Filter rules: a named question about one message, with its criteria and thresholds.

Pure data. A rule says what to ask the Decisions model; the server asks it and
turns the probabilities into a verdict with `verdict`. A new rule is one more
entry in `FILTER_RULES`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from t6.domain.job_order_rules import JOB_ORDER_FILTER_CRITERIA, JOB_ORDER_FILTER_QUESTION

__all__ = ["FILTER_RULES", "FilterRule", "Verdict", "verdict"]

Verdict = Literal["pass", "drop", "unsure"]


@dataclass(frozen=True, slots=True)
class FilterRule:
    name: str
    question: str
    criteria: Mapping[str, str]
    """Texts for `match`, `maybe` and `no`."""
    pass_at: float = 0.5
    """`pass` when the probability of `match` is at least this."""
    drop_at: float = 0.9
    """`drop` when the probability of `no` is at least this."""


FILTER_RULES: Mapping[str, FilterRule] = {
    rule.name: rule
    for rule in (FilterRule("job_order", JOB_ORDER_FILTER_QUESTION, JOB_ORDER_FILTER_CRITERIA),)
}


def verdict(probabilities: Mapping[str, float], pass_at: float, drop_at: float) -> Verdict:
    """`pass` wins over `drop` when both thresholds are met, so a doubtful message goes on."""
    if probabilities["match"] >= pass_at:
        return "pass"
    if probabilities["no"] >= drop_at:
        return "drop"
    return "unsure"
