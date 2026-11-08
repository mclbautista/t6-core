"""Typed tasks: the one way an app calls an LLM.

A :class:`Task` names a prompt (its tag), the input model the app fills and the
output model the reply must fit. Prompts stay in the app: ``render`` turns an
input into chat messages, or into a :class:`~t6.llm.transport.DecisionsRequest`
for a Decisions task (Jev). :func:`run` checks the input is masked, sends it,
parses and validates the reply, then runs ``Session.leaks`` and
``Session.restore``. It returns ``Ok`` or ``Failed`` and never raises for a bad
reply. Only programmer errors raise: unmasked input, or masked input without
its :class:`~t6.privacy.Session`.
"""

from __future__ import annotations

import dataclasses
import json
import time
from collections.abc import Callable
from enum import Enum
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ValidationError

from t6.llm.decisions import MalformedReply
from t6.llm.result import Failed, Ok, Reason, Result, Tokens, Trace
from t6.llm.transport import Completion, Provider, ProviderError, ProviderTimeout, Request
from t6.privacy import Masked, Session, Unresolved

In = TypeVar("In")
Out = TypeVar("Out", bound=BaseModel)

DEFAULT_TIMEOUT = 60.0


class UnmaskedInput(TypeError):
    """The task input holds plain text that did not go through ``t6.privacy``."""


class MissingSession(TypeError):
    """The task input holds pseudonymized text but no Session was given to restore it."""


@dataclasses.dataclass(frozen=True)
class Task(Generic[In, Out]):
    """One LLM job. ``render`` builds the request from the input; the app owns the prompt.

    A chat task renders a list of messages; a Decisions task renders a ``DecisionsRequest``.
    """

    prompt_tag: str
    input_model: type[In]
    output_model: type[Out]
    render: Callable[[In], Request]


def run(
    task: Task[In, Out],
    payload: In,
    *,
    provider: Provider,
    session: Session | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Result[Out]:
    """Run ``task`` on ``payload`` through ``provider``. Returns ``Ok`` or ``Failed``, never raises for the reply.

    Raises :class:`UnmaskedInput` if ``payload`` holds any plain ``str``, and
    :class:`MissingSession` if it holds ``Masked`` text and ``session`` is ``None``.
    """
    if not isinstance(payload, task.input_model):
        raise TypeError(f"{task.prompt_tag}: input must be {task.input_model.__name__}, got {type(payload).__name__}")
    plain = _plain_text_paths(payload)
    if plain:
        raise UnmaskedInput(f"{task.prompt_tag}: unmasked text at {', '.join(plain)}; pass it through t6.privacy")
    if session is None and _holds_masked(payload):
        raise MissingSession(f"{task.prompt_tag}: pseudonymized input needs the Session that masked it")
    request = task.render(payload)

    started = time.monotonic()
    model, tokens = provider.model, Tokens()

    def trace() -> Trace:
        return Trace(task.prompt_tag, model, tokens, round((time.monotonic() - started) * 1000))

    def failed(reason: Reason, detail: str) -> Failed:
        return Failed(reason, trace(), detail)

    try:
        completion: Completion = provider.complete(request, timeout=timeout)
    except MalformedReply as error:
        # The Decisions transport read the reply but it is off the alpha shape.
        model, tokens = error.model, error.tokens
        return failed("schema_violation", str(error))
    except ProviderTimeout as error:
        return failed("timeout", str(error))
    except ProviderError as error:
        return failed("api_error", str(error))
    model, tokens = completion.model, completion.tokens

    if completion.content is None or not completion.content.strip():
        return failed("empty", "the reply had no content")
    try:
        data = json.loads(completion.content)
    except ValueError as error:
        return failed("invalid_json", str(error))
    try:
        value = task.output_model.model_validate(data)
    except ValidationError as error:
        problems = "; ".join(f"{'.'.join(map(str, e['loc'])) or '<root>'}: {e['type']}" for e in error.errors())
        return failed("schema_violation", problems)

    if session is not None:
        leaks = session.leaks(value)
        if leaks:
            # Field paths only: the names themselves must not reach logs.
            return failed("leak", "raw registry names at " + ", ".join(sorted({leak.path or "<root>" for leak in leaks})))
        try:
            value = session.restore(value)
        except Unresolved as error:
            return failed("unresolved_placeholder", str(error))
    return Ok(value, trace())


def _children(value: object) -> list[tuple[str, object]]:
    if isinstance(value, dict):
        return [(str(k), v) for k, v in value.items()]
    if isinstance(value, (list, tuple, set, frozenset)):
        return [(f"[{i}]", v) for i, v in enumerate(value)]
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return [(f.name, getattr(value, f.name)) for f in dataclasses.fields(value)]
    model_fields = getattr(type(value), "model_fields", None)
    if isinstance(model_fields, dict):
        return [(name, getattr(value, name)) for name in model_fields]
    return []


def _walk(value: object, path: str = "") -> list[tuple[str, object]]:
    """Every leaf under ``value`` as ``(path, leaf)``. ``Masked`` and ``str`` are leaves."""
    if isinstance(value, (Masked, str)):
        return [(path, value)]
    found: list[tuple[str, object]] = []
    for name, child in _children(value):
        joined = f"{path}{name}" if name.startswith("[") else (f"{path}.{name}" if path else name)
        found.extend(_walk(child, joined))
    return found


def _plain_text_paths(payload: object) -> list[str]:
    # A str Enum member is a closed value set, not free text.
    return [path or "<root>" for path, leaf in _walk(payload) if isinstance(leaf, str) and not isinstance(leaf, Enum)]


def _holds_masked(payload: Any) -> bool:
    return any(isinstance(leaf, Masked) for _, leaf in _walk(payload))
