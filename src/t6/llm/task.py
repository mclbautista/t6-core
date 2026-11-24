"""Typed tasks: the one way an app calls an LLM.

A :class:`Task` names a prompt (its tag), the input model the app fills, the
output model the reply must fit, and its :class:`~t6.llm.DataPolicy`. Prompts
stay in the app: ``render`` turns an input into chat messages, or into a
:class:`~t6.llm.transport.DecisionsRequest` for a Decisions task (Jev). :func:`run`
checks the input is masked the way the policy says, checks the provider's
retention meets it, sends, strips a code fence, parses (repairing quotes if the
task opts in), validates, then runs ``Session.leaks`` and ``Session.restore``.
When a :class:`~t6.llm.skill.Skill` is attached, :func:`run` enforces the
pre/tool/post privacy airlock and multi-turn tool loop up to ``skill.max_turns``.
It returns ``Ok`` or ``Failed`` and never raises for a bad reply or a policy
mismatch. Only programmer errors raise: input masked the wrong way or not at
all, pseudonymized input without its :class:`~t6.privacy.Session`, or a skill
paired with a :class:`~t6.llm.transport.DecisionsRequest`.
"""

from __future__ import annotations

import dataclasses
import json
import time
from collections.abc import Callable, Mapping, Sequence
from enum import Enum
from types import UnionType
from typing import Annotated, Any, Generic, TypeVar, Union, cast, get_args, get_origin, get_type_hints

from pydantic import BaseModel, ValidationError

from t6.llm.decisions import MalformedReply
from t6.llm.jsontext import repair_quotes, strip_fence
from t6.llm.policy import DataPolicy
from t6.llm.result import Failed, Ok, Reason, Result, ScriptRun, Tokens, Trace
from t6.llm.skill import Skill, run_script
from t6.llm.transport import (
    Completion,
    DecisionsRequest,
    Message,
    Provider,
    ProviderError,
    ProviderTimeout,
    RateLimited,
    Request,
    ToolCall,
    ToolSpec,
)
from t6.privacy import Masked, Session, Unresolved, Withheld, withhold

In = TypeVar("In")
Out = TypeVar("Out", bound=BaseModel)

DEFAULT_TIMEOUT = 60.0
DEFAULT_RATE_LIMIT_WAIT = 1.0
"""Seconds to wait after a 429 that sent no usable ``Retry-After``."""
MAX_RATE_LIMIT_WAIT = 60.0
"""A 429 asking for a longer wait than this fails at once with ``api_error``."""

_MISSING = object()


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
    skill: Skill | None = None


@dataclasses.dataclass(frozen=True)
class ToolCallOutcome:
    """Result of leak-checking, restoring, running, and masking one tool call."""

    message: Message | None
    script_run: ScriptRun | None = None
    failure_reason: Reason | None = None
    failure_detail: str = ""


def mask_for_model(text: str, session: Session | None = None) -> str:
    """Strip credentials with ``withhold`` and pseudonymize names when a ``session`` is active."""
    cleaned = withhold(text).text
    return session.mask(cleaned).text if session is not None else cleaned


def assistant_tool_message(completion: Completion) -> Message:
    """Format an assistant :class:`Message` carrying ``completion.tool_calls``."""
    return {
        "role": "assistant",
        "content": completion.content or "",
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(dict(call.arguments), ensure_ascii=False),
                },
            }
            for call in completion.tool_calls
        ],
    }


def execute_tool_call(
    call: ToolCall,
    skill: Skill,
    *,
    session: Session | None = None,
    allowed_env: Mapping[str, str] | None = None,
    blocked_env: Sequence[str] = (),
) -> ToolCallOutcome:
    """Validate, restore, run, and mask one model-requested tool call."""
    args = dict(call.arguments)
    if session is not None:
        if session.leaks(call.name):
            return ToolCallOutcome(
                message=None,
                failure_reason="leak",
                failure_detail="raw registry names in tool call name",
            )
        arg_leaks = session.leaks(args)
        if arg_leaks:
            paths = ", ".join(sorted({leak.path or "<root>" for leak in arg_leaks}))
            return ToolCallOutcome(
                message=None,
                failure_reason="leak",
                failure_detail=f"raw registry names in tool arguments at {paths}",
            )
        if session.leaks(_collect_dict_keys(args)):
            return ToolCallOutcome(
                message=None,
                failure_reason="leak",
                failure_detail="raw registry names in tool argument keys",
            )

    tool = next((item for item in skill.tools if item.name == call.name), None)
    if tool is None:
        error_json = json.dumps({"error": mask_for_model(f"unknown tool: {call.name}", session)}, ensure_ascii=False)
        return ToolCallOutcome(message={"role": "tool", "tool_call_id": call.id, "content": error_json})

    if session is not None:
        try:
            restored_args = session.restore(args)
        except Unresolved as error:
            return ToolCallOutcome(
                message=None,
                failure_reason="unresolved_placeholder",
                failure_detail=str(error),
            )
    else:
        restored_args = args

    outcome = run_script(
        tool,
        restored_args,
        skill_dir=skill.root,
        env_names=skill.env,
        allowed_env=allowed_env,
        blocked_env=blocked_env,
    )
    script_run = ScriptRun(
        name=tool.name,
        phase="tool",
        exit_code=outcome.exit_code,
        duration_ms=outcome.duration_ms,
    )

    if not outcome.ok:
        raw_error = outcome.error or f"script exited with code {outcome.exit_code}"
        masked_error = mask_for_model(raw_error, session)
        if session is not None and session.leaks(masked_error):
            return ToolCallOutcome(
                message=None,
                script_run=script_run,
                failure_reason="leak",
                failure_detail="raw registry names in masked tool error",
            )
        if tool.on_error == "fail":
            return ToolCallOutcome(
                message=None,
                script_run=script_run,
                failure_reason="script_error",
                failure_detail=f"tool {tool.name!r} failed (exit {outcome.exit_code}): {masked_error}",
            )
        error_dict: dict[str, Any] = {"error": masked_error, "exit_code": outcome.exit_code}
        if outcome.stdout.strip():
            error_dict["stdout"] = _mask_stdout_for_model(outcome.stdout, session)
        error_payload = json.dumps(error_dict, ensure_ascii=False)
        if session is not None and session.leaks(error_payload):
            return ToolCallOutcome(
                message=None,
                script_run=script_run,
                failure_reason="leak",
                failure_detail="raw registry names in masked tool error",
            )
        return ToolCallOutcome(
            message={"role": "tool", "tool_call_id": call.id, "content": error_payload},
            script_run=script_run,
        )

    masked_stdout = _mask_stdout_for_model(outcome.stdout, session)
    if session is not None and session.leaks(masked_stdout):
        return ToolCallOutcome(
            message=None,
            script_run=script_run,
            failure_reason="leak",
            failure_detail="raw registry names in masked tool output",
        )
    return ToolCallOutcome(
        message={"role": "tool", "tool_call_id": call.id, "content": masked_stdout},
        script_run=script_run,
    )


def run(
    task: Task[In, Out],
    payload: In,
    *,
    provider: Provider,
    session: Session | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    sleep: Callable[[float], None] = time.sleep,
    skill: Skill | None = None,
    allowed_env: Mapping[str, str] | None = None,
    blocked_env: Sequence[str] = (),
) -> Result[Out]:
    """Run ``task`` on ``payload`` through ``provider``. Returns ``Ok`` or ``Failed``, never raises for the reply.

    Raises :class:`UnmaskedInput` if any text in ``payload`` is not masked the
    way ``task.policy.masking`` requires (a plain ``str`` never is),
    :class:`MissingSession` if it holds ``Masked`` text and ``session`` is ``None``,
    and ``TypeError`` if a :class:`~t6.llm.skill.Skill` is attached to a
    :class:`~t6.llm.transport.DecisionsRequest` task.
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

    active_skill = skill if skill is not None else task.skill
    request = task.render(payload)
    if active_skill is not None and isinstance(request, DecisionsRequest):
        raise TypeError(f"{task.prompt_tag}: skills are not supported with DecisionsRequest tasks")

    started = time.monotonic()
    model, tokens, repaired = provider.model, Tokens(), False
    cost: float | None = 0.0 if active_skill is not None else None
    skill_name = active_skill.name if active_skill is not None else None
    skill_hash = active_skill.skill_hash if active_skill is not None else None
    turns = 0 if active_skill is not None else 1
    script_runs: list[ScriptRun] = []

    def trace() -> Trace:
        return Trace(
            task.prompt_tag,
            model,
            tokens,
            round((time.monotonic() - started) * 1000),
            repaired,
            skill_name=skill_name,
            skill_hash=skill_hash,
            turns=turns,
            script_runs=tuple(script_runs),
            cost=cost,
        )

    def failed(reason: Reason, detail: str) -> Failed:
        return Failed(reason, trace(), detail)

    if not task.policy.allows(provider.retention):
        return failed(
            "policy",
            f"provider retention {provider.retention!r} is below the task minimum {task.policy.min_retention!r}",
        )

    if active_skill is None:
        outcome = _complete_with_retry(
            provider,
            request,
            timeout=timeout,
            zdr=task.policy.min_retention == "zdr_enforced",
            tools=(),
            sleep=sleep,
        )
        if isinstance(outcome, _CompletionFailure):
            if outcome.model is not None:
                model = outcome.model
            if outcome.tokens is not None:
                tokens = outcome.tokens
            return failed(outcome.reason, outcome.detail)
        completion = outcome
        model, tokens = completion.model, completion.tokens
        cost = completion.cost
    else:
        if session is not None:
            leak_detail = _check_skill_leaks(active_skill, session)
            if leak_detail is not None:
                return failed("leak", leak_detail)

        pre_notes: list[Message] = []
        if active_skill.pre:
            pre_result = _run_pre_hooks(
                task,
                payload,
                active_skill,
                session=session,
                allowed_env=allowed_env,
                blocked_env=blocked_env,
                script_runs=script_runs,
            )
            if isinstance(pre_result, _FailureSpec):
                return failed(pre_result.reason, pre_result.detail)
            rebuilt_payload, pre_notes = pre_result
            request = task.render(rebuilt_payload)
            if isinstance(request, DecisionsRequest):
                raise TypeError(f"{task.prompt_tag}: skills are not supported with DecisionsRequest tasks")

        messages: list[Message] = list(cast(Sequence[Message], request))
        sys_prompt = active_skill.system_prompt()
        if sys_prompt:
            messages = [{"role": "system", "content": sys_prompt}, *messages]
        if pre_notes:
            messages.extend(pre_notes)

        tool_specs = tuple(tool.to_spec() for tool in active_skill.tools)
        completion_opt: Completion | None = None
        for turn in range(1, active_skill.max_turns + 1):
            turns = turn
            outcome = _complete_with_retry(
                provider,
                messages,
                timeout=timeout,
                zdr=task.policy.min_retention == "zdr_enforced",
                tools=tool_specs,
                sleep=sleep,
            )
            if isinstance(outcome, _CompletionFailure):
                if outcome.model is not None:
                    model = outcome.model
                if outcome.tokens is not None:
                    tokens = tokens.add(outcome.tokens)
                cost = None
                return failed(outcome.reason, outcome.detail)
            model = outcome.model
            tokens = tokens.add(outcome.tokens)
            cost = cost + outcome.cost if cost is not None and outcome.cost is not None else None
            if not outcome.tool_calls:
                completion_opt = outcome
                break
            if turn >= active_skill.max_turns:
                return failed(
                    "max_turns_exceeded",
                    f"skill {active_skill.name!r} exceeded max_turns ({active_skill.max_turns})",
                )
            messages.append(assistant_tool_message(outcome))
            for call in outcome.tool_calls:
                tool_outcome = execute_tool_call(
                    call,
                    active_skill,
                    session=session,
                    allowed_env=allowed_env,
                    blocked_env=blocked_env,
                )
                if tool_outcome.script_run is not None:
                    script_runs.append(tool_outcome.script_run)
                if tool_outcome.failure_reason is not None:
                    return failed(tool_outcome.failure_reason, tool_outcome.failure_detail)
                assert tool_outcome.message is not None
                messages.append(tool_outcome.message)

        assert completion_opt is not None
        completion = completion_opt

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
        return failed("schema_violation", _format_validation_error(error))

    if session is not None:
        leaks = session.leaks(value)
        if leaks:
            # Field paths only: the names themselves must not reach logs.
            return failed("leak", "raw registry names at " + ", ".join(sorted({leak.path or "<root>" for leak in leaks})))
        try:
            value = session.restore(value)
        except Unresolved as error:
            return failed("unresolved_placeholder", str(error))

    if active_skill is not None and active_skill.post:
        post_result = _run_post_hooks(
            task.output_model,
            value,
            active_skill,
            session=session,
            allowed_env=allowed_env,
            blocked_env=blocked_env,
            script_runs=script_runs,
        )
        if isinstance(post_result, _FailureSpec):
            return failed(post_result.reason, post_result.detail)
        value = post_result

    return Ok(value, trace())


@dataclasses.dataclass(frozen=True)
class _FailureSpec:
    reason: Reason
    detail: str


@dataclasses.dataclass(frozen=True)
class _CompletionFailure:
    reason: Reason
    detail: str
    model: str | None = None
    tokens: Tokens | None = None


def _complete_with_retry(
    provider: Provider,
    request: Request,
    *,
    timeout: float,
    zdr: bool,
    tools: Sequence[ToolSpec],
    sleep: Callable[[float], None],
) -> Completion | _CompletionFailure:
    waited = False
    while True:
        try:
            if tools:
                return provider.complete(request, timeout=timeout, zdr=zdr, tools=tools)
            return provider.complete(request, timeout=timeout, zdr=zdr)
        except MalformedReply as error:
            # The Decisions transport read the reply but it is off the alpha shape.
            return _CompletionFailure("schema_violation", str(error), model=error.model, tokens=error.tokens)
        except ProviderTimeout as error:
            return _CompletionFailure("timeout", str(error))
        except RateLimited as error:
            if waited:
                return _CompletionFailure("api_error", f"{error} again after one wait")
            wait = DEFAULT_RATE_LIMIT_WAIT if error.retry_after is None else error.retry_after
            if wait > MAX_RATE_LIMIT_WAIT:
                return _CompletionFailure("api_error", f"{error}, longer than the {MAX_RATE_LIMIT_WAIT:g}s the adapter waits")
            sleep(wait)
            waited = True
        except ProviderError as error:
            return _CompletionFailure("api_error", str(error))


def _format_validation_error(error: ValidationError) -> str:
    return "; ".join(f"{'.'.join(map(str, e['loc'])) or '<root>'}: {e['type']}" for e in error.errors())


def _collect_dict_keys(value: object) -> list[str]:
    keys: list[str] = []
    if isinstance(value, Mapping):
        for k, v in value.items():
            if isinstance(k, str):
                keys.append(k)
            keys.extend(_collect_dict_keys(v))
    elif isinstance(value, (list, tuple)):
        for item in value:
            keys.extend(_collect_dict_keys(item))
    return keys


def _check_skill_leaks(skill: Skill, session: Session) -> str | None:
    if session.leaks(skill.system_prompt()):
        return "raw registry names in skill system prompt"
    for idx, tool in enumerate(skill.tools):
        if session.leaks(tool.name):
            return f"raw registry names in skill.tools[{idx}].name"
        if session.leaks(tool.description):
            return f"raw registry names in skill.tools[{idx}].description"
        if session.leaks(tool.parameters) or session.leaks(_collect_dict_keys(tool.parameters)):
            return f"raw registry names in skill.tools[{idx}].parameters"
    return None


def _mask_json_for_model(value: Any, session: Session | None) -> Any:
    if isinstance(value, str):
        return mask_for_model(value, session)
    if isinstance(value, list):
        return [_mask_json_for_model(item, session) for item in value]
    if isinstance(value, dict):
        return {
            (mask_for_model(k, session) if isinstance(k, str) else k): _mask_json_for_model(v, session)
            for k, v in value.items()
        }
    return value


def _mask_stdout_for_model(stdout: str, session: Session | None) -> str:
    stripped = stdout.strip()
    if stripped:
        try:
            parsed = json.loads(stripped)
        except ValueError:
            parsed = None
        else:
            if isinstance(parsed, (dict, list, str)):
                return json.dumps(_mask_json_for_model(parsed, session), ensure_ascii=False)
    return mask_for_model(stdout, session)


def _to_plain_json(value: Any) -> Any:
    if isinstance(value, (Masked, Withheld)):
        return value.text
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, BaseModel):
        return {name: _to_plain_json(getattr(value, name)) for name in type(value).model_fields}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _to_plain_json(getattr(value, f.name)) for f in dataclasses.fields(value) if f.init}
    if isinstance(value, dict):
        return {str(k): _to_plain_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_plain_json(v) for v in value]
    return value


def _safe_type_hints(cls: type[Any]) -> dict[str, Any]:
    try:
        return get_type_hints(cls)
    except Exception:
        return {}


def _unwrap_optional(hint: Any) -> tuple[Any, bool]:
    origin = get_origin(hint)
    if origin is Annotated:
        return _unwrap_optional(get_args(hint)[0])
    if origin in (Union, UnionType):
        args = get_args(hint)
        allows_none = type(None) in args
        non_none = [arg for arg in args if arg is not type(None)]
        if len(non_none) == 1:
            inner, inner_none = _unwrap_optional(non_none[0])
            return inner, allows_none or inner_none
    return hint, False


def _mask_text_leaf(text: str, *, policy: DataPolicy, session: Session | None, prompt_tag: str) -> Masked | Withheld:
    cleaned = withhold(text)
    if policy.masking == "withhold":
        return cleaned
    if session is None:
        raise MissingSession(f"{prompt_tag}: pseudonymized input needs the Session that masked it")
    return session.mask(cleaned.text)


def _rebuild_input_value(
    raw: Any,
    *,
    hint: Any,
    template: Any,
    policy: DataPolicy,
    session: Session | None,
    prompt_tag: str,
) -> Any:
    inner_hint, allows_none = _unwrap_optional(hint)
    if raw is None:
        if allows_none or template is None or template is _MISSING:
            return None
        raise ValueError("unexpected null value")

    if inner_hint is Masked or isinstance(template, Masked):
        if not isinstance(raw, str):
            raise ValueError(f"expected string for masked text field, got {type(raw).__name__}")
        return _mask_text_leaf(raw, policy=policy, session=session, prompt_tag=prompt_tag)

    if inner_hint is Withheld or isinstance(template, Withheld):
        if not isinstance(raw, str):
            raise ValueError(f"expected string for withheld text field, got {type(raw).__name__}")
        return _mask_text_leaf(raw, policy=policy, session=session, prompt_tag=prompt_tag)

    if isinstance(inner_hint, type) and issubclass(inner_hint, Enum):
        return inner_hint(raw)
    if isinstance(template, Enum):
        return type(template)(raw)

    if isinstance(inner_hint, type) and issubclass(inner_hint, BaseModel):
        if not isinstance(raw, Mapping):
            raise ValueError(f"expected mapping for {inner_hint.__name__}, got {type(raw).__name__}")
        hints = _safe_type_hints(inner_hint)
        rebuilt: dict[str, Any] = {}
        for k, v in raw.items():
            key = str(k)
            tpl = getattr(template, key, _MISSING) if template is not _MISSING else _MISSING
            rebuilt[key] = _rebuild_input_value(
                v, hint=hints.get(key, Any), template=tpl, policy=policy, session=session, prompt_tag=prompt_tag
            )
        return inner_hint.model_validate(rebuilt)

    if isinstance(inner_hint, type) and dataclasses.is_dataclass(inner_hint):
        if not isinstance(raw, Mapping):
            raise ValueError(f"expected mapping for {inner_hint.__name__}, got {type(raw).__name__}")
        hints = _safe_type_hints(inner_hint)
        kwargs: dict[str, Any] = {}
        for field in dataclasses.fields(inner_hint):
            if not field.init:
                continue
            if field.name in raw:
                tpl = getattr(template, field.name, _MISSING) if template is not _MISSING else _MISSING
                kwargs[field.name] = _rebuild_input_value(
                    raw[field.name],
                    hint=hints.get(field.name, Any),
                    template=tpl,
                    policy=policy,
                    session=session,
                    prompt_tag=prompt_tag,
                )
            elif field.default is dataclasses.MISSING and field.default_factory is dataclasses.MISSING:
                raise ValueError(f"missing required field {field.name!r}")
        return inner_hint(**kwargs)

    origin = get_origin(inner_hint)
    args = get_args(inner_hint)
    if origin in (list, tuple, Sequence) or isinstance(raw, list):
        if not isinstance(raw, (list, tuple)):
            raise ValueError(f"expected sequence, got {type(raw).__name__}")
        tpl_seq = template if isinstance(template, (list, tuple)) else ()
        rebuilt_items: list[Any] = []
        for idx, item in enumerate(raw):
            if origin is tuple and args and (len(args) != 2 or args[1] is not Ellipsis):
                item_hint = args[idx] if idx < len(args) else Any
            elif args:
                item_hint = args[0]
            else:
                item_hint = Any
            tpl_item = tpl_seq[idx] if idx < len(tpl_seq) else (tpl_seq[0] if tpl_seq else _MISSING)
            rebuilt_items.append(
                _rebuild_input_value(
                    item, hint=item_hint, template=tpl_item, policy=policy, session=session, prompt_tag=prompt_tag
                )
            )
        return tuple(rebuilt_items) if origin is tuple or isinstance(template, tuple) else rebuilt_items

    if origin in (dict, Mapping) or inner_hint is dict or isinstance(raw, dict):
        if not isinstance(raw, Mapping):
            raise ValueError(f"expected mapping, got {type(raw).__name__}")
        val_hint = args[1] if len(args) >= 2 else Any
        tpl_dict = template if isinstance(template, Mapping) else {}
        fallback_tpl = next(iter(tpl_dict.values()), _MISSING) if tpl_dict else _MISSING
        return {
            str(k): _rebuild_input_value(
                v,
                hint=val_hint,
                template=tpl_dict.get(k, fallback_tpl),
                policy=policy,
                session=session,
                prompt_tag=prompt_tag,
            )
            for k, v in raw.items()
        }

    if isinstance(raw, str):
        return _mask_text_leaf(raw, policy=policy, session=session, prompt_tag=prompt_tag)
    return raw


def _input_model_field_names(input_model: type[Any]) -> set[str]:
    if isinstance(input_model, type) and issubclass(input_model, BaseModel):
        return set(input_model.model_fields)
    if dataclasses.is_dataclass(input_model):
        return {f.name for f in dataclasses.fields(input_model)}
    return set()


def _unwrap_envelope(parsed: Any, *, key: str, model_fields: set[str], original_dict: Mapping[str, Any] | None = None) -> Any:
    if not isinstance(parsed, dict) or set(parsed) != {key}:
        return parsed
    if key in model_fields or (original_dict is not None and key in original_dict):
        inner = parsed[key]
        if isinstance(inner, dict) and key in inner:
            return inner
        return parsed
    return parsed[key]


def _run_pre_hooks(
    task: Task[In, Any],
    payload: In,
    skill: Skill,
    *,
    session: Session | None,
    allowed_env: Mapping[str, str] | None,
    blocked_env: Sequence[str],
    script_runs: list[ScriptRun],
) -> tuple[In, list[Message]] | _FailureSpec:
    if session is not None:
        try:
            unmasked_payload = session.restore(payload)
        except Unresolved as error:
            return _FailureSpec("unresolved_placeholder", str(error))
    else:
        unmasked_payload = payload

    current_input = _to_plain_json(unmasked_payload)
    orig_dict = current_input if isinstance(current_input, Mapping) else None
    field_names = _input_model_field_names(task.input_model)
    notes: list[Message] = []

    for hook in skill.pre:
        outcome = run_script(
            hook,
            {"input": current_input},
            skill_dir=skill.root,
            env_names=skill.env,
            allowed_env=allowed_env,
            blocked_env=blocked_env,
        )
        script_runs.append(
            ScriptRun(name=hook.name, phase="pre", exit_code=outcome.exit_code, duration_ms=outcome.duration_ms)
        )
        if not outcome.ok:
            safe_error = mask_for_model(outcome.error or f"script exited with code {outcome.exit_code}", session)
            if session is not None and session.leaks(safe_error):
                return _FailureSpec("leak", "raw registry names in masked pre-hook error")
            if hook.on_error == "return_to_model":
                notes.append(
                    {
                        "role": "user",
                        "content": json.dumps({"pre_error": hook.name, "error": safe_error}, ensure_ascii=False),
                    }
                )
                continue
            return _FailureSpec(
                "script_error", f"pre hook {hook.name!r} failed (exit {outcome.exit_code}): {safe_error}"
            )
        if outcome.stdout.strip():
            try:
                parsed = json.loads(outcome.stdout)
            except ValueError as error:
                return _FailureSpec("script_error", f"pre hook {hook.name!r} returned invalid JSON: {error}")
            current_input = _unwrap_envelope(parsed, key="input", model_fields=field_names, original_dict=orig_dict)

    try:
        rebuilt = _rebuild_input_value(
            current_input,
            hint=task.input_model,
            template=payload,
            policy=task.policy,
            session=session,
            prompt_tag=task.prompt_tag,
        )
    except MissingSession:
        raise
    except (TypeError, ValueError, ValidationError) as error:
        safe_detail = mask_for_model(str(error), session)
        return _FailureSpec("script_error", f"pre hook output does not match {task.input_model.__name__}: {safe_detail}")

    if not isinstance(rebuilt, task.input_model):
        return _FailureSpec("script_error", f"pre hook output must produce {task.input_model.__name__}")
    wrong = _wrongly_masked_paths(rebuilt, task.policy)
    if wrong:
        return _FailureSpec("leak", f"unmasked text after pre hook at {', '.join(wrong)}")
    if session is not None and (session.leaks(rebuilt) or session.leaks(_collect_dict_keys(rebuilt))):
        return _FailureSpec("leak", "raw registry names remained after masking pre hook output")
    return rebuilt, notes


def _run_post_hooks(
    output_model: type[Out],
    value: Out,
    skill: Skill,
    *,
    session: Session | None,
    allowed_env: Mapping[str, str] | None,
    blocked_env: Sequence[str],
    script_runs: list[ScriptRun],
) -> Out | _FailureSpec:
    current_output: Any = value.model_dump(mode="json")
    model_fields = set(output_model.model_fields)

    for hook in skill.post:
        outcome = run_script(
            hook,
            {"output": current_output},
            skill_dir=skill.root,
            env_names=skill.env,
            allowed_env=allowed_env,
            blocked_env=blocked_env,
        )
        script_runs.append(
            ScriptRun(name=hook.name, phase="post", exit_code=outcome.exit_code, duration_ms=outcome.duration_ms)
        )
        if not outcome.ok:
            safe_error = mask_for_model(outcome.error or f"script exited with code {outcome.exit_code}", session)
            return _FailureSpec(
                "script_error", f"post hook {hook.name!r} failed (exit {outcome.exit_code}): {safe_error}"
            )
        if outcome.stdout.strip():
            try:
                parsed = json.loads(outcome.stdout)
            except ValueError as error:
                return _FailureSpec("script_error", f"post hook {hook.name!r} returned invalid JSON: {error}")
            current_output = _unwrap_envelope(parsed, key="output", model_fields=model_fields)

    try:
        return output_model.model_validate(current_output)
    except ValidationError as error:
        return _FailureSpec("schema_violation", _format_validation_error(error))


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
