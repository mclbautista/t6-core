"""Time the three model calls of `jo draft` before they are built into it (spec #791, ticket #792).

For each real one-line request the command runs, on masked text only:

1. Jev call 1: which of up to 12 past Job Orders match (`jo-draft-match-v1`);
2. Jev call 2: which extra lookups are needed (`jo-draft-lookups-v1`);
3. one Haiku turn on a draft-shaped prompt (`jo-draft-haiku-v1`).

It prints, for each call, the time, model, tokens and cost (OpenRouter
`usage.cost`, or `unknown`), then the median and worst case of each call and of
the three together. The output holds no prompt or reply text. A live run uses
the operator's keys; tests use ``FakeProvider`` only.

    python scripts/jo_draft_timing.py LINES.json --config llm.json --privacy-registry privacy_registry.json

``LINES.json`` is a list of ``{"line": "...", "candidates": [{"task": "...", "purpose": "...", "mode": "..."}]}``
(real text; it is masked here, and never printed). The config is the API's
``T6_LLM_CONFIG`` shape: ``llm`` maps ``jo-draft-haiku-v1`` to its own provider,
and ``decisions_by_task`` maps each Jev task tag to its own Decisions entry
(``decisions`` is the fallback).
"""

from __future__ import annotations

import dataclasses
import statistics
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict

from t6.llm.decisions import CandidateDecisions, candidate_key
from t6.llm.policy import DataPolicy
from t6.llm.result import Failed, Trace
from t6.llm.task import Task, run
from t6.llm.transport import Choice, DecisionsRequest, Message, Provider
from t6.privacy import Masked, Registry, Session, pseudonymize, withhold

__all__ = ["MATCH", "LOOKUPS", "HAIKU", "measure", "summarize"]

MATCH_TAG = "jo-draft-match-v1"
LOOKUPS_TAG = "jo-draft-lookups-v1"
HAIKU_TAG = "jo-draft-haiku-v1"
MAX_CANDIDATES = 12
POLICY = DataPolicy(min_retention="unknown", masking="pseudonymize")
"""Masked text only. Jev's retention is unknown unless the account enforces ZDR."""

_MATCH_CRITERIA = {
    "match": "The request is the same kind of work as this past Job Order.",
    "maybe": "The request could be the same kind of work, but it is not clear.",
    "no": "The request is a different kind of work.",
}
_YES_NO = {
    "match": "Yes.",
    "maybe": "Not sure.",
    "no": "No.",
}


@dataclasses.dataclass(frozen=True)
class MatchInput:
    line: Masked
    candidates: tuple[Masked, ...]


@dataclasses.dataclass(frozen=True)
class LookupInput:
    line: Masked
    best: Masked


@dataclasses.dataclass(frozen=True)
class DraftInput:
    line: Masked
    best: Masked


class DraftFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: str
    purpose: str
    notes: str


def render_match(data: MatchInput) -> DecisionsRequest:
    return DecisionsRequest(
        state={"request": data.line.text},
        questions={
            candidate_key(i): Choice(f"Is the request the same kind of work as this past Job Order? {past.text}", _MATCH_CRITERIA)
            for i, past in enumerate(data.candidates)
        },
    )


def render_lookups(data: LookupInput) -> DecisionsRequest:
    return DecisionsRequest(
        state={"request": data.line.text, "closest_past_job_order": data.best.text},
        questions={
            "c0": Choice("Does the request name a person other than the assignee?", _YES_NO),
            "c1": Choice("Is one more past Job Order needed in full to draft this request?", _YES_NO),
        },
    )


def render_haiku(data: DraftInput) -> list[Message]:
    return [
        {"role": "system", "content": 'Draft a Job Order. Return JSON {"task": "...", "purpose": "...", "notes": "..."}. Mark unknowns "(?)".'},
        {"role": "user", "content": f"Request: {data.line.text}\nClosest past Job Order: {data.best.text}"},
    ]


MATCH: Task[MatchInput, CandidateDecisions] = Task(MATCH_TAG, MatchInput, CandidateDecisions, render_match, policy=POLICY)
LOOKUPS: Task[LookupInput, CandidateDecisions] = Task(LOOKUPS_TAG, LookupInput, CandidateDecisions, render_lookups, policy=POLICY)
HAIKU: Task[DraftInput, DraftFields] = Task(HAIKU_TAG, DraftInput, DraftFields, render_haiku, policy=POLICY)

STEPS = ("jev_match", "jev_lookups", "haiku")


@dataclasses.dataclass(frozen=True)
class Timing:
    """One call: ids and numbers only."""

    step: str
    ms: int
    model: str
    prompt_tokens: int | None
    completion_tokens: int | None
    cost: float | None
    failure: str | None = None


def _timing(step: str, trace: Trace, failure: str | None = None) -> Timing:
    return Timing(step, trace.latency_ms, trace.model, trace.tokens.prompt, trace.tokens.completion, trace.cost, failure)


def _past(session: Session, item: Mapping[str, Any]) -> Masked:
    parts = [f"{label}: {withhold(str(item.get(key) or '')).text}" for label, key in (("Task", "task"), ("Purpose", "purpose"), ("Mode of Transfer", "mode"))]
    return session.mask("\n".join(parts))


def _step(task: Task[Any, Any], payload: Any, provider: Provider, session: Session, step: str) -> Timing:
    result = run(task, payload, provider=provider, session=session)
    return _timing(step, result.trace, result.reason if isinstance(result, Failed) else None)


def measure(
    lines: Sequence[Mapping[str, Any]], registry: Registry, *, jev_match: Provider, jev_lookups: Provider, haiku: Provider
) -> list[list[Timing]]:
    """Three timed calls for each line, masked with ``registry``. A failed call is a row with its reason."""
    rows: list[list[Timing]] = []
    for item in lines:
        line, session = pseudonymize(withhold(str(item["line"])).text, registry)
        candidates = tuple(_past(session, c) for c in list(item.get("candidates") or [])[:MAX_CANDIDATES])
        best = candidates[0] if candidates else session.mask("(none)")
        rows.append([
            _step(MATCH, MatchInput(line, candidates), jev_match, session, "jev_match"),
            _step(LOOKUPS, LookupInput(line, best), jev_lookups, session, "jev_lookups"),
            _step(HAIKU, DraftInput(line, best), haiku, session, "haiku"),
        ])
    return rows


def summarize(rows: Sequence[Sequence[Timing]]) -> dict[str, Any]:
    """Per-call numbers and the median and worst case of each step and of the three together."""
    def cost_of(timings: Sequence[Timing]) -> float | str:
        return "unknown" if any(t.cost is None for t in timings) else round(sum(t.cost or 0.0 for t in timings), 6)

    def stats(values: list[int]) -> dict[str, float]:
        return {"median_ms": float(statistics.median(values)), "worst_ms": float(max(values))} if values else {}

    summary: dict[str, Any] = {"lines": len(rows), "calls": [[dataclasses.asdict(t) for t in row] for row in rows]}
    for step in STEPS:
        timings = [t for row in rows for t in row if t.step == step]
        summary[step] = {**stats([t.ms for t in timings]), "failed": sum(1 for t in timings if t.failure), "cost_usd": cost_of(timings)}
    totals = [sum(t.ms for t in row) for row in rows]
    summary["all_three"] = {**stats(totals), "cost_usd": cost_of([t for row in rows for t in row])}
    return summary
