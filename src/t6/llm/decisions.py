"""Jev: OpenRouter's Decisions API as a transport behind the typed task.

A Decisions task renders a :class:`DecisionsRequest` instead of chat messages:
``state`` holds the masked context and each candidate becomes a typed
:class:`Choice` question keyed ``c0``, ``c1``, ... The keys map back to Notion
page IDs inside the app through :class:`CandidateKeys`; a page ID never reaches
the transport. The output model is :class:`CandidateDecisions`, one choice and
match/maybe/no probabilities per key.

The endpoint is alpha, so the transport validates every reply against the
documented shape. Anything else raises :class:`MalformedReply`, which ``run``
returns as ``failed(schema_violation)``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from t6.llm.result import Tokens
from t6.llm.transport import (
    Choice,
    Completion,
    DecisionsRequest,
    ProviderError,
    Request,
    Send,
    _post,
    _urlopen,
)

DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"
JEV_MODEL = "~typesafe/jev-latest"

# match + maybe + no must be 1 within this, plus float noise.
PROBABILITY_TOLERANCE = 0.01

Verdict = Literal["match", "maybe", "no"]


def candidate_key(index: int) -> str:
    """The question key for the candidate at ``index``: ``c0``, ``c1``, ..."""
    return f"c{index}"


class MalformedReply(ProviderError):
    """The reply is off the documented Decisions shape. ``run`` returns ``failed(schema_violation)``.

    The transport still read what it could, so the trace keeps the returned
    model and tokens. The message names field paths only, never reply text.
    """

    def __init__(self, detail: str, *, model: str, tokens: Tokens) -> None:
        super().__init__(detail)
        self.model = model
        self.tokens = tokens


class Probabilities(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    match: float = Field(ge=0, le=1)
    maybe: float = Field(ge=0, le=1)
    no: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _sums_to_one(self) -> Probabilities:
        if abs(self.match + self.maybe + self.no - 1.0) > PROBABILITY_TOLERANCE + 1e-9:
            raise ValueError("probabilities do not sum to 1")
        return self


class CandidateDecision(BaseModel):
    """Jev's answer for one candidate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    choice: Verdict
    probabilities: Probabilities


class CandidateDecisions(BaseModel):
    """The output model for a Decisions task: one :class:`CandidateDecision` per candidate key."""

    answers: dict[str, CandidateDecision]


class CandidateKeys:
    """Maps candidate keys to the app's page IDs. Stays in the app; only the keys go out.

    Build it from the candidates' page IDs in the order the task renders them,
    then map the result back with :meth:`by_page_id`.
    """

    def __init__(self, page_ids: Sequence[str]) -> None:
        if len(set(page_ids)) != len(page_ids):
            raise ValueError("candidate page IDs must be unique")
        self._page_ids = {candidate_key(i): page_id for i, page_id in enumerate(page_ids)}

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(self._page_ids)

    def __len__(self) -> int:
        return len(self._page_ids)

    def __iter__(self) -> Iterator[str]:
        return iter(self._page_ids)

    def page_id(self, key: str) -> str:
        """The page ID behind ``key``. Raises ``KeyError`` for a key this map did not issue."""
        return self._page_ids[key]

    def by_page_id(self, decisions: CandidateDecisions) -> dict[str, CandidateDecision]:
        """``decisions`` keyed by page ID, in candidate order. The keys must match exactly."""
        if set(decisions.answers) != set(self._page_ids):
            raise ValueError("the decisions do not answer exactly the issued candidate keys")
        return {page_id: decisions.answers[key] for key, page_id in self._page_ids.items()}


class OpenRouterDecisions:
    """OpenRouter's alpha Decisions API, the transport Jev runs on.

    The API key comes from the app's config. ``send`` is swappable for tests.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str = JEV_MODEL,
        url: str = DECISIONS_URL,
        send: Send = _urlopen,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._url = url
        self._send = send

    @property
    def model(self) -> str:
        return self._model

    def complete(self, request: Request, *, timeout: float) -> Completion:
        if not isinstance(request, DecisionsRequest):
            raise TypeError("OpenRouterDecisions sends a DecisionsRequest; use OpenAIChat for chat messages")
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        payload = json.dumps(request_body(request, self._model)).encode()
        raw = _post(self._send, self._url, headers, payload, timeout)
        try:
            body = json.loads(raw)
        except ValueError:
            raise ProviderError("unreadable provider response") from None
        if isinstance(body, dict) and "error" in body:
            raise ProviderError("provider returned an error body")
        return read_reply(body, request, requested_model=self._model)


def request_body(request: DecisionsRequest, model: str) -> dict[str, Any]:
    """The exact Decisions API request body."""
    return {
        "model": model,
        "state": dict(request.state),
        "questions": {
            key: {"type": "choice", "instructions": q.instructions, "criteria": dict(q.criteria)}
            for key, q in request.questions.items()
        },
    }


class _AlphaAnswer(BaseModel):
    # No reason or candidate-name echo is asked for, so none is accepted.
    model_config = ConfigDict(extra="forbid")

    type: Literal["choice"]
    choice: Verdict
    confidence: float | None = None  # concentration, not correctness: dropped
    probabilities: Probabilities


class _AlphaUsage(BaseModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class _AlphaReply(BaseModel):
    """The documented Decisions API success body. Other top-level fields (``id``) are ignored."""

    model: str
    answers: dict[str, _AlphaAnswer]
    usage: _AlphaUsage | None = None


def read_reply(body: object, request: DecisionsRequest, *, requested_model: str) -> Completion:
    """Validate a Decisions reply against the alpha shape and the request's keys.

    Returns a :class:`Completion` whose content is the :class:`CandidateDecisions`
    JSON. Raises :class:`MalformedReply` for anything off the shape. The fake
    provider calls this too, so task tests exercise the same validation.
    """
    model, tokens = _salvage(body, requested_model)
    try:
        reply = _AlphaReply.model_validate(body)
    except ValidationError as error:
        problems = "; ".join(f"{'.'.join(map(str, e['loc'])) or '<root>'}: {e['type']}" for e in error.errors())
        raise MalformedReply(problems, model=model, tokens=tokens) from None
    asked, answered = set(request.questions), set(reply.answers)
    if asked != answered:
        raise MalformedReply(
            f"answers: {len(asked - answered)} missing and {len(answered - asked)} unknown candidate key(s)",
            model=model,
            tokens=tokens,
        )
    content = {
        "answers": {
            key: {"choice": reply.answers[key].choice, "probabilities": reply.answers[key].probabilities.model_dump()}
            for key in request.questions
        }
    }
    return Completion(json.dumps(content), model, tokens)


def _salvage(body: object, requested_model: str) -> tuple[str, Tokens]:
    """The model and token counts a possibly malformed body holds, for the trace. Never raises."""
    if not isinstance(body, dict):
        return requested_model, Tokens()
    model = body.get("model") if isinstance(body.get("model"), str) else requested_model
    usage = body.get("usage")
    counts = [usage.get(name) if isinstance(usage, dict) else None for name in ("input_tokens", "output_tokens")]
    prompt, completion = (n if isinstance(n, int) and not isinstance(n, bool) and n >= 0 else 0 for n in counts)
    return str(model), Tokens(prompt, completion)
