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
    "policy",
    "script_error",
    "max_turns_exceeded",
]

ScriptPhase = Literal["pre", "tool", "post"]


@dataclasses.dataclass(frozen=True)
class ScriptRun:
    """Metadata for one script execution. Holds no stdin or stdout text."""

    name: str
    phase: ScriptPhase
    exit_code: int
    duration_ms: int


@dataclasses.dataclass(frozen=True)
class Tokens:
    """Token counts the provider reported. Zero when the call got no reply."""

    prompt: int | None = 0
    completion: int | None = 0

    @property
    def total(self) -> int | None:
        if self.prompt is None or self.completion is None:
            return None
        return self.prompt + self.completion

    def add(self, other: Tokens) -> Tokens:
        """Combine token counts across turns; unknown (``None``) counts propagate."""
        return Tokens(
            None if self.prompt is None or other.prompt is None else self.prompt + other.prompt,
            None if self.completion is None or other.completion is None else self.completion + other.completion,
        )


@dataclasses.dataclass(frozen=True)
class Trace:
    """One call, for the app to store. Holds no prompt or reply text.

    ``repaired`` is true when the parsed reply came from quote repair.
    """

    prompt_tag: str
    model: str
    tokens: Tokens
    latency_ms: int
    repaired: bool = False
    skill_name: str | None = None
    skill_hash: str | None = None
    turns: int = 1
    script_runs: tuple[ScriptRun, ...] = ()


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
