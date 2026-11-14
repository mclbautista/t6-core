"""Shared, JSON-serializable shapes for LLM eval cases and run records.

Apps own their prompts and synthetic cases. Real cases and runs belong off-repo;
bringing a real case into a repo requires privacy masking, a names denylist
check and human review. These models record provenance, not those checks.
Grader names identify app-owned checks; expected properties describe behavior,
not an exact output.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
import math
from pathlib import Path
from typing import Annotated, Literal

from jsonschema.validators import validator_for
from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints
from referencing import Registry

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


def grade_schema(case: Case, output: JsonValue, schema: dict[str, JsonValue]) -> CaseResult:
    """Validate JSON output against an app-owned JSON Schema, without remote retrieval.

    Invalid schemas and unresolved references are configuration errors and raise.
    """
    validator_class = validator_for(schema)
    validator_class.check_schema(schema)
    validator = validator_class(schema, registry=Registry())
    error = next(validator.iter_errors(output), None)
    return CaseResult(case_id=case.id, passed=error is None,
                      reason="schema valid" if error is None else "schema validation failed")


def grade_kinds(
    case: Case, kinds: Iterable[str], *, required: Iterable[str] = (), forbidden: Iterable[str] = (),
) -> CaseResult:
    """Check app-extracted output kinds against the case's required and forbidden kinds."""
    found = set(kinds)
    missing = set(required) - found
    present = set(forbidden) & found
    if missing or present:
        return CaseResult(case_id=case.id, passed=False, reason="required kind missing or forbidden kind present")
    return CaseResult(case_id=case.id, passed=True, reason="kind requirements met")


def _strings(value: JsonValue) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)


def grade_quotes(case: Case, quotes: Iterable[str]) -> CaseResult:
    """Check app-extracted quotes verbatim against individual masked-input strings."""
    sources = tuple(_strings(case.masked_input))
    if any(not quote or not any(quote in source for source in sources) for quote in quotes):
        return CaseResult(case_id=case.id, passed=False, reason="quote not found in masked input")
    return CaseResult(case_id=case.id, passed=True, reason="quotes grounded in masked input")


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
    review_outputs: dict[str, list[JsonValue]] = Field(default_factory=dict)
    reviewer: NonEmpty | None = None
    review_date: date | None = None

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
    only completed case results in the immediately preceding live run are compared;
    cases without a result have no prior outcome. Acceptances
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
    previous = next((r for r in reversed(history) if r.status != "offline"), None)
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
    elif len(results) != len(cases):
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


__all__ = ["Case", "CaseResult", "Grader", "Provenance", "RealProvenance", "RunRecord", "SyntheticProvenance", "Attempt", "run_evals",
           "grade_kinds", "grade_quotes", "grade_schema", "LiveEvalConfig", "run_live_evals"]

@dataclass(frozen=True)
class LiveEvalConfig:
    """App-owned live run configuration; prices are per token in one currency."""

    task_name: str
    prompt_template: str
    output_schema: dict
    masking_preset: Literal['names', 'strict']
    cost_cap: float
    prompt_token_price: float
    completion_token_price: float
    real_cases_path: str | Path | None = None
    model_hint: str | None = None


def run_live_evals(
    cases: Sequence[Case], *, config: LiveEvalConfig, client: object,
    grade: Callable[[Case, JsonValue], CaseResult], t6_version: str, run_date: date,
    history: Sequence[RunRecord] = (), must_pass: set[str] | frozenset[str] = frozenset(),
    accept: Mapping[str, str] | None = None, path: str | Path | None = None,
    rng: object | None = None,
) -> RunRecord:
    """Run an app's task through the API, adding off-repo cases and a review sample.

    The API must attest that ZDR was enforced. Case inputs must be strings;
    apps with structured inputs should serialize them before creating cases.
    Costs are calculated from API token counts and configured per-token prices.
    """
    import json
    import random
    from t6.verbs import Done, LLMRunInput, LLMRunOutput

    for price in (config.prompt_token_price, config.completion_token_price):
        if not math.isfinite(price) or price < 0:
            raise ValueError('token prices must be finite and nonnegative')
    if not math.isfinite(config.cost_cap) or config.cost_cap <= 0:
        raise ValueError('live cost cap must be finite and positive')
    all_cases = list(cases)
    if config.real_cases_path is not None:
        data = json.loads(Path(config.real_cases_path).read_text(encoding='utf-8'))
        real_cases = [Case.model_validate(item) for item in data]
        if any(case.provenance.kind != 'real' for case in real_cases):
            raise ValueError('real-case file must contain only real cases')
        all_cases.extend(real_cases)
    if not all_cases or any(case.task != config.task_name or not isinstance(case.masked_input, str) for case in all_cases):
        raise ValueError('live cases must have the configured task and string inputs')
    metadata: tuple[str, str, str, str] | None = None
    outputs: dict[str, list[JsonValue]] = {}

    def evaluate(case: Case, _provider: Provider) -> Attempt:
        nonlocal metadata
        result = client.llm_run(LLMRunInput(task_name=config.task_name, prompt_template=config.prompt_template,
            output_schema=config.output_schema, input_text=case.masked_input, masking_preset=config.masking_preset,
            model_hint=config.model_hint, require_zdr=True, temperature_zero=True))
        if not isinstance(result, Done):
            raise RuntimeError(f'llm run refused or unavailable: {result}')
        output = LLMRunOutput.model_validate(result.result)
        if not output.zdr:
            raise RuntimeError('llm run did not attest to zero retention')
        current = (output.model, output.provider, output.prompt_hash, output.schema_hash)
        if metadata is not None and metadata != current:
            raise RuntimeError('llm run metadata changed during the run')
        metadata = current
        tokens = output.tokens
        if any(type(tokens.get(k)) is not int or tokens[k] < 0 for k in ('prompt', 'completion')):
            raise ValueError('llm run returned invalid token counts')
        outputs.setdefault(case.id, []).append(output.output)
        graded = grade(case, output.output)
        if graded.case_id != case.id:
            raise ValueError('grader returned a different case ID')
        return Attempt(graded.passed, graded.reason,
            tokens['prompt'] * config.prompt_token_price + tokens['completion'] * config.completion_token_price)

    # Metadata is replaced with the API's values after the first call.
    record = RunRecord(model_id='pending', provider='pending', prompt_hash='pending', schema_hash='pending',
                       t6_version=t6_version, date=run_date, results=[])
    result = run_evals(all_cases, record=record, evaluate=evaluate, provider=None, cost_cap=config.cost_cap,
                       must_pass=must_pass, history=history, accept=accept)  # type: ignore[arg-type]
    if metadata is None:
        raise RuntimeError('live run produced no API metadata')
    previous = next((r for r in reversed(history) if r.status != 'offline'), None)
    prior = {r.case_id: r.passed for r in previous.results} if previous else {}
    required = {r.case_id for r in result.results if not r.passed or
                (r.case_id in prior and r.passed != prior[r.case_id])}
    required.update(result.human_review)
    remaining = [r.case_id for r in result.results if r.case_id not in required]
    sampler = rng if rng is not None else random.SystemRandom()
    selected = set(sampler.sample(remaining, min(5, len(remaining))))
    review = [r.case_id for r in result.results if r.case_id in required or r.case_id in selected]
    result = result.model_copy(update=dict(zip(('model_id', 'provider', 'prompt_hash', 'schema_hash'), metadata)) |
                               {'human_review': review, 'review_outputs': {case_id: outputs[case_id] for case_id in review}})
    if path is not None:
        result.write(path)
    return result
