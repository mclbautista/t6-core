"""Shared, JSON-serializable shapes for LLM eval cases and run records.

Apps own their prompts and synthetic cases. Real cases and runs belong off-repo;
bringing a real case into a repo requires privacy masking, a names denylist
check and human review. These models record provenance, not those checks.
Grader names identify app-owned checks; expected properties describe behavior,
not an exact output.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
import math
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints

from t6.llm.transport import OpenAIChat, Provider

NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Grader = NonEmpty
"""Name of the grader to use for a case."""


class SyntheticProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["synthetic"]


class RealProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["real"]
    reviewer: NonEmpty
    date: date


Provenance = Annotated[SyntheticProvenance | RealProvenance, Field(discriminator="kind")]


class Case(BaseModel):
    """A masked task input and properties its output should satisfy."""

    model_config = ConfigDict(extra="forbid")

    id: NonEmpty
    task: NonEmpty
    masked_input: JsonValue
    expected_properties: Annotated[list[NonEmpty], Field(min_length=1)]
    grader: Grader
    provenance: Provenance


class CaseResult(BaseModel):
    """The outcome and short explanation for one case."""

    model_config = ConfigDict(extra="forbid")

    case_id: NonEmpty
    passed: Annotated[bool, Field(strict=True)]
    reason: NonEmpty


class RunRecord(BaseModel):
    """Evidence for one eval run, stored as one JSON file."""

    model_config = ConfigDict(extra="forbid")

    model_id: NonEmpty
    provider: NonEmpty
    prompt_hash: NonEmpty
    schema_hash: NonEmpty
    t6_version: NonEmpty
    date: date
    results: list[CaseResult]

    # Optional fields preserve compatibility with records written before the runner.
    status: Literal["passed", "failed", "incomplete", "offline"] | None = None
    cost: float = 0.0
    cost_cap_reached: bool = False
    regressions: list[str] = Field(default_factory=list)
    changed_failures: list[str] = Field(default_factory=list)
    accepted_regressions: dict[str, NonEmpty] = Field(default_factory=dict)
    human_review: list[str] = Field(default_factory=list)

    def write(self, path: str | Path) -> None:
        """Write the run record to a JSON file."""
        Path(path).write_text(self.model_dump_json(indent=2, exclude_unset=True) + "\n", encoding="utf-8")

    @classmethod
    def read(cls, path: str | Path) -> RunRecord:
        """Read and validate a run-record JSON file."""
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))


@dataclass(frozen=True)
class Attempt:
    """One code-graded attempt and its actual cost in the configured currency.

    A judge may request review, but cannot influence ``passed``.
    """

    passed: bool
    reason: str
    cost: float = 0.0
    review: bool = False


def run_evals(
    cases: Sequence[Case],
    *,
    record: RunRecord,
    evaluate: Callable[[Case, Provider], Attempt],
    provider: Provider,
    cost_cap: float,
    must_pass: set[str] | frozenset[str] = frozenset(),
    history: Sequence[RunRecord] = (),
    accept: Mapping[str, str] | None = None,
    offline: bool = False,
    offline_evaluate: Callable[[Case], Attempt] | None = None,
    path: str | Path | None = None,
) -> RunRecord:
    """Run app-owned code graders and optionally write one record.

    ``evaluate`` executes the task and its code grader once; callers configure
    provider pricing and return actual cost per attempt. The cap is checked
    after each attempt, so one call may exceed it. Offline evaluation uses a
    separate callback and never invokes ``evaluate``. Chat providers receive
    temperature zero; providers without that setting keep their own defaults. History is chronological;
    only the immediately preceding recorded live run is compared. Acceptances
    map case IDs to the name of the person accepting the change.
    """
    if not math.isfinite(cost_cap) or cost_cap < 0:
        raise ValueError("cost_cap must be finite and nonnegative")
    ids = [case.id for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("case IDs must be unique")
    if not must_pass <= set(ids):
        raise ValueError("must_pass contains unknown case IDs")
    accepted = dict(accept or {})
    if any(not name.strip() for name in accepted.values()):
        raise ValueError("acceptance requires a person's name")
    previous = next((r for r in reversed(history) if r.status not in ("offline", "incomplete")), None)
    previous_results = {r.case_id: r for r in previous.results} if previous else {}
    results: list[CaseResult] = []
    review: list[str] = []
    total = 0.0
    capped = False
    for case in cases:
        attempts: list[Attempt] = []
        if offline and offline_evaluate is None:
            break
        for _ in range(3):
            if not offline and total >= cost_cap:
                capped = True
                break
            if offline:
                assert offline_evaluate is not None
                attempt = offline_evaluate(case)
            else:
                eval_provider = provider.with_temperature(0) if isinstance(provider, OpenAIChat) else provider
                attempt = evaluate(case, eval_provider)
            if not math.isfinite(attempt.cost) or attempt.cost < 0:
                raise ValueError("attempt cost must be finite and nonnegative")
            total += attempt.cost
            attempts.append(attempt)
            if attempt.review and case.id not in review:
                review.append(case.id)
            if not offline and total >= cost_cap:
                capped = True
                break
        if len(attempts) != 3:
            break
        passed = sum(a.passed for a in attempts) >= 2
        reasons = "; ".join(a.reason for a in attempts)
        results.append(CaseResult(case_id=case.id, passed=passed, reason=reasons))
        if capped:
            break
    changes = [r.case_id for r in results if not r.passed and r.case_id in previous_results and previous_results[r.case_id].passed]
    regressions = [r.case_id for r in results if not r.passed and r.case_id in previous_results and not previous_results[r.case_id].passed]
    applicable = set(changes + regressions)
    if previous:
        for case_id, person in previous.accepted_regressions.items():
            if case_id in regressions:
                accepted.setdefault(case_id, person)
    if set(accepted) - applicable:
        raise ValueError("acceptance must refer to a current change or regression")
    rest = [r for r in results if r.case_id not in must_pass]
    bars = all(r.passed for r in results if r.case_id in must_pass) and (not rest or sum(r.passed for r in rest) * 10 >= len(rest) * 9)
    blocked = any(case_id not in accepted for case_id in applicable)
    status: Literal["passed", "failed", "incomplete", "offline"]
    if offline:
        status = "offline"
    elif capped or len(results) != len(cases):
        status = "incomplete"
    else:
        status = "passed" if bars and not blocked else "failed"
    output = record.model_copy(update={"results": results, "status": status, "cost": total,
        "cost_cap_reached": capped, "changed_failures": changes, "regressions": regressions,
        "accepted_regressions": accepted, "human_review": review})
    # model_copy does not mark updated fields on older pydantic versions.
    if path is not None:
        output.write(path)
    return output


__all__ = ["Case", "CaseResult", "Grader", "Provenance", "RealProvenance", "RunRecord", "SyntheticProvenance", "Attempt", "run_evals"]
