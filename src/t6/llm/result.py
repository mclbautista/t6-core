"""What every LLM call returns: ``Ok`` or ``Failed``, each with a ``Trace``."""

from __future__ import annotations

import dataclasses
from typing import Generic, Literal, TypeVar

T = TypeVar("T")

Reason = Literal[
    "timeout",
    "api_error",
    "empty",
    "invalid_json",
    "schema_violation",
    "unresolved_placeholder",
    "leak",
]


@dataclasses.dataclass(frozen=True)
class Tokens:
    """Token counts the provider reported. Zero when the call got no reply."""

    prompt: int = 0
    completion: int = 0

    @property
    def total(self) -> int:
        return self.prompt + self.completion


@dataclasses.dataclass(frozen=True)
class Trace:
    """One call, for the app to store. Holds no prompt or reply text."""

    prompt_tag: str
    model: str
    tokens: Tokens
    latency_ms: int


@dataclasses.dataclass(frozen=True)
class Ok(Generic[T]):
    """The validated output model, with real names restored."""

    value: T
    trace: Trace
    ok: Literal[True] = dataclasses.field(default=True, init=False)


@dataclasses.dataclass(frozen=True)
class Failed:
    """Why the call gave no usable output. ``detail`` holds no raw names."""

    reason: Reason
    trace: Trace
    detail: str = ""
    ok: Literal[False] = dataclasses.field(default=False, init=False)


Result = Ok[T] | Failed
