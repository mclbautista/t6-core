"""A scripted fake provider for tests. No network, no recorded live replies.

Queue what the provider should do, in order, then run tasks against it::

    fake = FakeProvider()
    fake.reply('{"summary": "[person 1] confirmed"}', prompt_tokens=40, completion_tokens=8)
    fake.reply("not json")
    fake.timeout()
    fake.api_error("HTTP 500")
    fake.rate_limited(retry_after=2)

For a Decisions task (Jev), queue the raw Decisions API body instead. The fake
runs it through the transport's alpha-shape validation, so a malformed body
gives the same ``failed(schema_violation)`` as the real transport::

    fake.decisions({"model": "typesafe/jev-1.1", "answers": {"c0": {...}}})

A fake declares ``zdr_enforced`` retention unless told otherwise:
``FakeProvider(retention="unknown")``.

Each call to ``complete`` takes the next scripted step. ``calls`` records the
request sent, so a test can check that only masked text left the app.
"""

from __future__ import annotations

import dataclasses
from collections import deque

from t6.llm.decisions import read_reply
from t6.llm.policy import Retention
from t6.llm.result import Tokens
from t6.llm.transport import (
    Completion,
    DecisionsRequest,
    Message,
    ProviderError,
    ProviderTimeout,
    RateLimited,
    Request,
)


@dataclasses.dataclass(frozen=True)
class Call:
    request: Request
    timeout: float
    zdr: bool = False
    """True when the task's policy is ``zdr_enforced``."""

    @property
    def messages(self) -> tuple[Message, ...]:
        """The chat messages sent. Raises ``TypeError`` for a Decisions request."""
        if isinstance(self.request, DecisionsRequest):
            raise TypeError("this call sent a DecisionsRequest, not chat messages")
        return tuple(self.request)


@dataclasses.dataclass(frozen=True)
class _DecisionsBody:
    body: object
    requested_model: str


class FakeProvider:
    """Replays scripted replies and failures in order."""

    def __init__(self, model: str = "fake-model", *, retention: Retention = "zdr_enforced") -> None:
        self._model = model
        self._retention: Retention = retention
        self._script: deque[Completion | _DecisionsBody | Exception] = deque()
        self.calls: list[Call] = []

    @property
    def model(self) -> str:
        return self._model

    @property
    def retention(self) -> Retention:
        return self._retention

    def reply(
        self,
        content: str | None,
        *,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        model: str | None = None,
    ) -> FakeProvider:
        """Queue a reply. ``content`` may be malformed JSON, empty, ``None`` or placeholder-mangled."""
        self._script.append(Completion(content, model or self._model, Tokens(prompt_tokens, completion_tokens)))
        return self

    def decisions(self, body: object) -> FakeProvider:
        """Queue a raw Decisions API reply body. It may be off the alpha shape."""
        self._script.append(_DecisionsBody(body, self._model))
        return self

    def timeout(self) -> FakeProvider:
        """Queue a timeout."""
        self._script.append(ProviderTimeout("scripted timeout"))
        return self

    def api_error(self, detail: str = "scripted API error") -> FakeProvider:
        """Queue a provider error."""
        self._script.append(ProviderError(detail))
        return self

    def rate_limited(self, retry_after: float | None = None) -> FakeProvider:
        """Queue a 429. ``retry_after`` is the seconds the provider asks for, or ``None`` for no header."""
        self._script.append(RateLimited(retry_after))
        return self

    @property
    def pending(self) -> int:
        """Scripted steps not used yet."""
        return len(self._script)

    def complete(self, request: Request, *, timeout: float, zdr: bool = False) -> Completion:
        if not isinstance(request, DecisionsRequest):
            request = tuple(request)
        self.calls.append(Call(request, timeout, zdr))
        if not self._script:
            raise AssertionError("FakeProvider has no scripted reply left")
        step = self._script.popleft()
        if isinstance(step, Exception):
            raise step
        if isinstance(step, _DecisionsBody):
            if not isinstance(request, DecisionsRequest):
                raise TypeError("a Decisions reply was scripted but the task sent chat messages")
            return read_reply(step.body, request, requested_model=step.requested_model)
        if isinstance(request, DecisionsRequest):
            raise TypeError("a chat reply was scripted but the task sent a DecisionsRequest")
        return step
