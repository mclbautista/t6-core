"""Typed tasks: the one way an app calls an LLM.

A :class:`Task` names a prompt (its tag), the input model the app fills, the
output model the reply must fit, and its :class:`~t6.llm.DataPolicy`. Prompts
stay in the app: ``render`` turns an input into chat messages, or into a
:class:`~t6.llm.transport.DecisionsRequest` for a Decisions task (Jev). :func:`run`
checks the input is masked the way the policy says, checks the provider's
retention meets it, sends, strips a code fence, parses (repairing quotes if the
task opts in), validates, then runs ``Session.leaks`` and ``Session.restore``.
It returns ``Ok`` or ``Failed`` and never raises for a bad reply or a policy
mismatch. Only programmer errors raise: input masked the wrong way or not at
all, or pseudonymized input without its :class:`~t6.privacy.Session`.
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
from t6.llm.jsontext import repair_quotes, strip_fence
from t6.llm.policy import DataPolicy
from t6.llm.result import Failed, Ok, Reason, Result, Tokens, Trace
from t6.llm.transport import Completion, Provider, ProviderError, ProviderTimeout, RateLimited, Request
from t6.privacy import Masked, Session, Unresolved, Withheld

In = TypeVar("In")
Out = TypeVar("Out", bound=BaseModel)

DEFAULT_TIMEOUT = 60.0
DEFAULT_RATE_LIMIT_WAIT = 1.0
"""Seconds to wait after a 429 that sent no usable ``Retry-After``."""
MAX_RATE_LIMIT_WAIT = 60.0
"""A 429 asking for a longer wait than this fails at once with ``api_error``."""


class UnmaskedInput(TypeError):
    """The task input holds text not masked the way the task's policy requires."""


class MissingSession(TypeError):
    """The task input holds pseudonymized text but no Session was given to restore it."""


@dataclasses.dataclass(frozen=True)
class Task(Generic[In, Out]):
    """One LLM job. ``render`` builds the request from the input; the app owns the prompt.

    A chat task renders a list of messages; a Decisions task renders a ``DecisionsRequest``.

    ``policy`` defaults to the strictest: ``zdr_enforced`` and ``pseudonymize``.
    ``repair_quotes`` turns on quote repair for replies that fail to parse.
    """

    prompt_tag: str
    input_model: type[In]
    output_model: type[Out]
    render: Callable[[In], Request]
    policy: DataPolicy = DataPolicy()
    repair_quotes: bool = False


def run(
    task: Task[In, Out],
    payload: In,
    *,
    provider: Provider,
    session: Session | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    sleep: Callable[[float], None] = time.sleep,
) -> Result[Out]:
    """Run ``task`` on ``payload`` through ``provider``. Returns ``Ok`` or ``Failed``, never raises for the reply.

    Raises :class:`UnmaskedInput` if any text in ``payload`` is not masked the
    way ``task.policy.masking`` requires (a plain ``str`` never is), and
    :class:`MissingSession` if it holds ``Masked`` text and ``session`` is ``None``.
    Returns ``Failed("policy")`` without sending if ``provider.retention`` is
    below ``task.policy.min_retention``. A 429 is waited on once (``sleep``),
    honouring ``Retry-After``; a second 429 returns ``Failed("api_error")``.
    """
    if not isinstance(payload, task.input_model):
        raise TypeError(f"{task.prompt_tag}: input must be {task.input_model.__name__}, got {type(payload).__name__}")
    wrong = _wrongly_masked_paths(payload, task.policy)
    if wrong:
        raise UnmaskedInput(
            f"{task.prompt_tag}: text at {', '.join(wrong)} is not {_MASKING_NOUN[task.policy.masking]}; "
            f"this task's policy requires {task.policy.masking}"
        )
    if session is None and _holds_masked(payload):
        raise MissingSession(f"{task.prompt_tag}: pseudonymized input needs the Session that masked it")

    started = time.monotonic()
    model, tokens, repaired = provider.model, Tokens(), False

    def trace() -> Trace:
        return Trace(task.prompt_tag, model, tokens, round((time.monotonic() - started) * 1000), repaired)

    def failed(reason: Reason, detail: str) -> Failed:
        return Failed(reason, trace(), detail)

    if not task.policy.allows(provider.retention):
        return failed(
            "policy",
            f"provider retention {provider.retention!r} is below the task minimum {task.policy.min_retention!r}",
        )
    request = task.render(payload)

    waited = False
    while True:
        try:
            completion: Completion = provider.complete(request, timeout=timeout)
            break
        except MalformedReply as error:
            # The Decisions transport read the reply but it is off the alpha shape.
            model, tokens = error.model, error.tokens
            return failed("schema_violation", str(error))
        except ProviderTimeout as error:
            return failed("timeout", str(error))
        except RateLimited as error:
            if waited:
                return failed("api_error", f"{error} again after one wait")
            wait = DEFAULT_RATE_LIMIT_WAIT if error.retry_after is None else error.retry_after
            if wait > MAX_RATE_LIMIT_WAIT:
                return failed("api_error", f"{error}, longer than the {MAX_RATE_LIMIT_WAIT:g}s the adapter waits")
            sleep(wait)
            waited = True
        except ProviderError as error:
            return failed("api_error", str(error))
    model, tokens = completion.model, completion.tokens

    if completion.content is None or not completion.content.strip():
        return failed("empty", "the reply had no content")
    text = strip_fence(completion.content)
    try:
        data = json.loads(text)
    except ValueError as error:
        if not task.repair_quotes:
            return failed("invalid_json", str(error))
        try:
            data = json.loads(repair_quotes(text))
        except ValueError:
            return failed("invalid_json", f"{error} (quote repair did not help)")
        repaired = True
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
    """Every leaf under ``value`` as ``(path, leaf)``. ``Masked``, ``Withheld`` and ``str`` are leaves."""
    if isinstance(value, (Masked, Withheld, str)):
        return [(path, value)]
    found: list[tuple[str, object]] = []
    for name, child in _children(value):
        joined = f"{path}{name}" if name.startswith("[") else (f"{path}.{name}" if path else name)
        found.extend(_walk(child, joined))
    return found


_MASKING_NOUN = {"pseudonymize": "Masked (pseudonymized)", "withhold": "Withheld"}
_ACCEPTED: dict[str, type] = {"pseudonymize": Masked, "withhold": Withheld}


def _wrongly_masked_paths(payload: object, policy: DataPolicy) -> list[str]:
    """Paths of text leaves that are not the masking type the policy requires."""
    accepted = _ACCEPTED[policy.masking]
    wrong: list[str] = []
    for path, leaf in _walk(payload):
        # A str Enum member is a closed value set, not free text.
        if isinstance(leaf, Enum) or isinstance(leaf, accepted):
            continue
        if isinstance(leaf, (str, Masked, Withheld)):
            wrong.append(path or "<root>")
    return wrong


def _holds_masked(payload: Any) -> bool:
    return any(isinstance(leaf, Masked) for _, leaf in _walk(payload))
