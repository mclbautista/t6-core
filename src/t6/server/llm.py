"""Server-owned LLM execution, configuration and privacy boundary."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import time
from collections.abc import Collection, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from referencing import Registry as SchemaRegistry
from referencing.exceptions import Unresolvable

from t6.domain.message_filter import FILTER_RULES, verdict
from t6.llm.config import ConfigError, LLMConfig
from t6.llm.decisions import DECISIONS_URL, JEV_MODEL, CandidateDecisions, MalformedReply, OpenRouterDecisions, candidate_key
from t6.llm.jsontext import repair_quotes, strip_fence
from t6.llm.policy import RETENTIONS, DataPolicy, Retention
from t6.llm.result import Reason, ScriptRun, Tokens
from t6.llm.skill import Skill, SkillError, run_script
from t6.llm.task import assistant_tool_message, execute_tool_call, mask_for_model
from t6.llm.transport import Choice, Completion, DecisionsRequest, Message, OpenAIChat, Provider, ProviderError, ProviderTimeout, RateLimited, ToolSpec
from t6.privacy import FIXED_TEXT, STRICT, Person, Project, Registry, Session, Unresolved, pseudonymize, withhold
from t6.verbs import Done, LLMDecideInput, LLMDecideOutput, LLMFilterInput, LLMFilterOutput, LLMRunInput, LLMRunOutput, Refused


def _hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _unwrap_script_payload(parsed: Any, *, key: str, protected_keys: Collection[str] = ()) -> Any:
    """Unwrap a ``{key: value}`` script envelope unless ``key`` is a real top-level schema field."""
    if not isinstance(parsed, dict) or set(parsed) != {key}:
        return parsed
    if key in protected_keys:
        inner = parsed[key]
        if isinstance(inner, dict) and key in inner:
            return inner
        return parsed
    return parsed[key]


def _tool_metadata_bytes(skill: Skill) -> int:
    if not skill.tools:
        return 0
    payload = [{'name': t.name, 'description': t.description, 'parameters': t.parameters} for t in skill.tools]
    return len(json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode())


def _check_fixed_texts(
    name_check: Session,
    prompt_template: str,
    output_schema: dict[str, Any],
    schema_text: str,
    skill: Skill | None,
    skill_prompt: str,
) -> Refused | None:
    """Verify static app/skill prompts and schemas contain no non-ordinary registered names or credentials."""
    if name_check.leaks(prompt_template) or withhold(prompt_template).text != prompt_template:
        return Refused(rule='masking', reason='Unresolved registered names in the prompt template')
    # The serialised text also holds the property names, which `leaks` does not read from a dict.
    if name_check.leaks(output_schema) or name_check.leaks(schema_text) or withhold(schema_text).text != schema_text:
        return Refused(rule='masking', reason='Unresolved registered names in the output schema')
    if skill is None:
        return None
    if name_check.leaks(skill_prompt) or withhold(skill_prompt).text != skill_prompt:
        return Refused(rule='masking', reason='Unresolved registered names in the skill prompt')
    for tool in skill.tools:
        params_text = json.dumps(tool.parameters, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
        if (
            name_check.leaks(tool.name)
            or withhold(tool.name).text != tool.name
            or name_check.leaks(tool.description)
            or withhold(tool.description).text != tool.description
            or name_check.leaks(tool.parameters)
            or name_check.leaks(params_text)
            or withhold(params_text).text != params_text
        ):
            return Refused(rule='masking', reason='Unresolved registered names in the skill tools')
    return None


def _complete_chat(
    provider: Provider,
    messages: Sequence[Message],
    *,
    require_zdr: bool,
    tools: Sequence[ToolSpec] = (),
) -> Completion | Refused:
    try:
        if tools:
            return provider.complete(messages, timeout=60, zdr=require_zdr, tools=tools)
        return provider.complete(messages, timeout=60, zdr=require_zdr)
    except ProviderTimeout:
        return Refused(rule='provider', reason='LLM provider failed', failure='timeout')
    except RateLimited as limited:
        return Refused(rule='provider', reason='LLM provider failed', failure='api_error', retry_after=limited.retry_after)
    except ProviderError:
        return Refused(rule='provider', reason='LLM provider failed', failure='api_error')


class LLMService:
    def __init__(self, providers: Mapping[str, Provider], default: str, registry: Registry,
                 *, require_zdr: bool = True, max_bytes: int = 100_000,
                 min_retention_by_provider: Mapping[str, Retention] | None = None,
                 decisions: OpenRouterDecisions | None = None, filters: Any = None,
                 skills_dir: str | Path | None = None,
                 allowed_script_env: Mapping[str, str] | None = None,
                 blocked_script_env: Sequence[str] = ()) -> None:
        if default not in providers or type(require_zdr) is not bool or type(max_bytes) is not int or max_bytes < 1:
            raise ValueError('Invalid LLM policy')
        if skills_dir is not None and (not isinstance(skills_dir, (str, Path)) or not str(skills_dir).strip()):
            raise ValueError('Invalid LLM policy')
        floors = dict(min_retention_by_provider or {})
        if any((name not in providers and not (name == 'decisions' and decisions)) or value not in RETENTIONS for name, value in floors.items()):
            raise ValueError('Invalid LLM policy')
        self.decisions = decisions
        self.thresholds = _thresholds(filters)
        self.min_retention_by_provider = floors
        self.providers, self.default, self.registry = dict(providers), default, registry
        self.require_zdr, self.max_bytes = require_zdr, max_bytes
        self.skills_dir: Path | None = Path(skills_dir).resolve() if skills_dir is not None else None
        self.allowed_script_env: dict[str, str] | None = dict(allowed_script_env) if allowed_script_env is not None else None
        self.blocked_script_env: tuple[str, ...] = tuple(blocked_script_env)

    def run(self, inputs: LLMRunInput, *, actor: str, registry: Any) -> Done | Refused:
        # Audit only fixed metadata, never caller-supplied text or provider output.
        meta: dict[str, Any] = {}
        outcome = self._run(inputs, meta)
        with registry._transaction() as db:
            db.execute('INSERT INTO audit (project_id, action, actor, at, before, after) VALUES (?, ?, ?, ?, ?, ?)',
                       (actor, 'llm_run', actor, time.time(), None,
                        json.dumps({'outcome': outcome.outcome, **meta,
                                    # A refusal's rule and failure class are fixed words, never caller text.
                                    **({'refused_rule': outcome.rule, 'failure': outcome.failure} if isinstance(outcome, Refused) else {})})))
        return outcome

    def decide(self, inputs: LLMDecideInput, *, actor: str, registry: Any) -> Done | Refused:
        # Audit only fixed metadata, never caller-supplied text or provider output.
        meta: dict[str, Any] = {}
        outcome = self._decide(inputs, meta)
        with registry._transaction() as db:
            db.execute('INSERT INTO audit (project_id, action, actor, at, before, after) VALUES (?, ?, ?, ?, ?, ?)',
                       (actor, 'llm_decide', actor, time.time(), None,
                        json.dumps({'outcome': outcome.outcome, **meta})))
        return outcome

    def _decide(self, inputs: LLMDecideInput, meta: dict[str, Any]) -> Done | Refused:
        provider = self.decisions
        if provider is None:
            return Refused(rule='config', reason='Decisions provider is not configured')
        if inputs.model_hint and inputs.model_hint != provider.model:
            return Refused(rule='policy', reason='Model is not allowed', failure='policy')
        if inputs.question.count('{candidate}') != 1:
            return Refused(rule='template', reason='Question must contain exactly one {candidate} marker')
        if set(inputs.criteria) != {'match', 'maybe', 'no'}:
            return Refused(rule='criteria', reason='Criteria must hold exactly match, maybe and no')
        if not inputs.candidates:
            return Refused(rule='candidates', reason='At least one candidate is required')
        texts = [*inputs.state, *inputs.state.values(), inputs.question, *inputs.criteria.values(), *inputs.candidates]
        if sum(len(text.encode()) for text in texts) > self.max_bytes:
            return Refused(rule='size', reason='LLM request exceeds the configured size limit')
        require_zdr, refusal = self._decisions_policy(provider, meta, inputs.require_zdr)
        if refusal:
            return refusal
        _, session = pseudonymize('', self.registry, options='names')
        _, name_check = pseudonymize('', self.registry, options=FIXED_TEXT)
        # The question, criteria and state keys are sent as written, so a registered name that is no ordinary word is refused; values are masked.
        fixed = [('the question', [inputs.question]), ('the criteria', [*inputs.criteria, *inputs.criteria.values()]), ('a state key', [*inputs.state])]
        for part, found in fixed:
            if any(name_check.leaks(text) or withhold(text).text != text for text in found):
                return Refused(rule='masking', reason=f'Unresolved registered names in {part}')
        state = {key: session.mask(withhold(value).text).text for key, value in inputs.state.items()}
        names = [session.mask(withhold(name).text).text for name in inputs.candidates]
        if any(session.leaks(text) for text in state.values()):
            return Refused(rule='masking', reason='Unresolved registered names in a state value')
        if any(session.leaks(text) for text in names):
            return Refused(rule='masking', reason='Unresolved registered names in a candidate')
        request = DecisionsRequest(state, {
            candidate_key(i): Choice(inputs.question.replace('{candidate}', name), dict(inputs.criteria))
            for i, name in enumerate(names)})
        asked = self._ask_decisions(provider, request, require_zdr)
        if isinstance(asked, Refused):
            return asked
        decisions, completion = asked
        # The reply holds only keys and numbers, so there is nothing to restore.
        answers = [{'index': i, 'choice': decisions.answers[candidate_key(i)].choice,
                    'probabilities': decisions.answers[candidate_key(i)].probabilities.model_dump()}
                   for i in range(len(names))]
        return Done(result=LLMDecideOutput.model_validate({
            'answers': answers, 'model': completion.model, 'provider': 'decisions',
            'tokens': {'prompt': completion.tokens.prompt, 'completion': completion.tokens.completion, 'total': completion.tokens.total},
            'zdr': require_zdr}))

    def filter(self, inputs: LLMFilterInput, *, actor: str, registry: Any) -> Done | Refused:
        # Audit only fixed metadata: the rule name and the verdict, never caller-supplied text or probabilities.
        meta: dict[str, Any] = {}
        outcome = self._filter(inputs, meta)
        with registry._transaction() as db:
            db.execute('INSERT INTO audit (project_id, action, actor, at, before, after) VALUES (?, ?, ?, ?, ?, ?)',
                       (actor, 'llm_filter', actor, time.time(), None,
                        json.dumps({'outcome': outcome.outcome, **meta})))
        return outcome

    def _filter(self, inputs: LLMFilterInput, meta: dict[str, Any]) -> Done | Refused:
        provider = self.decisions
        if provider is None:
            return Refused(rule='config', reason='Decisions provider is not configured')
        rule = FILTER_RULES.get(inputs.rule)
        if rule is None:
            return Refused(rule='input', reason='Unknown filter rule')
        texts = [inputs.text, *([inputs.reply_to_text] if inputs.reply_to_text is not None else []), rule.question, *rule.criteria.values()]
        if sum(len(text.encode()) for text in texts) > self.max_bytes:
            return Refused(rule='size', reason='LLM request exceeds the configured size limit')
        meta['rule'] = rule.name
        require_zdr, refusal = self._decisions_policy(provider, meta, False)
        if refusal:
            return refusal
        # The question and criteria come from the rule table, not from the caller; only the message text is masked.
        _, session = pseudonymize('', self.registry, options='names')
        state = {'message': inputs.text} | ({'replied_to': inputs.reply_to_text} if inputs.reply_to_text is not None else {})
        state = {key: session.mask(withhold(value).text).text for key, value in state.items()}
        if any(session.leaks(text) for text in state.values()):
            return Refused(rule='masking', reason='Unresolved registered names in the message')
        asked = self._ask_decisions(provider, DecisionsRequest(state, {candidate_key(0): Choice(rule.question, dict(rule.criteria))}), require_zdr)
        if isinstance(asked, Refused):
            return asked
        decisions, completion = asked
        answer = decisions.answers[candidate_key(0)]
        probabilities = answer.probabilities.model_dump()
        pass_at, drop_at = self.thresholds.get(rule.name, (rule.pass_at, rule.drop_at))
        meta['verdict'] = verdict(probabilities, pass_at, drop_at)
        return Done(result=LLMFilterOutput.model_validate({
            'verdict': meta['verdict'], 'probabilities': probabilities, 'rule': rule.name, 'model': completion.model, 'provider': 'decisions',
            'tokens': {'prompt': completion.tokens.prompt, 'completion': completion.tokens.completion, 'total': completion.tokens.total},
            'zdr': require_zdr}))

    def _decisions_policy(self, provider: OpenRouterDecisions, meta: dict[str, Any], require_zdr: bool) -> tuple[bool, Refused | None]:
        """The retention check of the Decisions provider: its floor is the `decisions` entry of `provider_min_retention`."""
        meta['provider'] = 'decisions'
        floor = self.min_retention_by_provider.get('decisions', 'zdr_enforced' if self.require_zdr else 'unknown')
        require_zdr = floor == 'zdr_enforced' or require_zdr
        meta['zdr'] = require_zdr
        if not DataPolicy(min_retention='zdr_enforced' if require_zdr else 'unknown').allows(provider.retention):
            return require_zdr, Refused(rule='policy', reason='Provider does not meet retention policy', failure='policy')
        return require_zdr, None

    def _ask_decisions(self, provider: OpenRouterDecisions, request: DecisionsRequest, require_zdr: bool) -> tuple[CandidateDecisions, Completion] | Refused:
        try:
            completion = provider.complete(request, timeout=60, zdr=require_zdr)
        except MalformedReply as bad:
            return Refused(rule='provider', reason=f'Malformed Decisions reply from {bad.model}: '
                           f'{bad.tokens.prompt} prompt and {bad.tokens.completion} completion tokens', failure='schema_violation')
        except ProviderTimeout:
            return Refused(rule='provider', reason='LLM provider failed', failure='timeout')
        except RateLimited as limited:
            return Refused(rule='provider', reason='LLM provider failed', failure='api_error', retry_after=limited.retry_after)
        except ProviderError:
            return Refused(rule='provider', reason='LLM provider failed', failure='api_error')
        return CandidateDecisions.model_validate_json(completion.content or ''), completion

    def _allowed_names(self, entries: list[str]) -> list[str] | None:
        """Every name and code of the Projects the entries name, or None when an entry is not a Project or one of those names is also a person."""
        def key(text: str) -> str:
            return ' '.join(text.lstrip('@').split()).casefold()
        people = {key(name) for kind, name, _ in self.registry.terms() if kind == 'person'}
        allowed: list[str] = []
        for entry in entries:
            found = [p for p in self.registry.projects if key(entry) in (key(p.name), key(p.code or ''))]
            if not key(entry) or not found or key(entry) in people:
                return None
            allowed += [name for p in found for name in (p.name, p.code or '') if name]
        # A Project found by its name can bring a code that is also a person's name; that would unmask the person.
        return None if any(key(name) in people for name in allowed) else allowed

    def _resolve_skill(self, raw_name: str) -> Skill | Refused:
        """Load a named skill directory confined strictly inside ``self.skills_dir``."""
        if self.skills_dir is None:
            return Refused(rule='skill', reason='Skills directory is not configured')
        if (
            not isinstance(raw_name, str)
            or not raw_name.strip()
            or raw_name != raw_name.strip()
            or '\\' in raw_name
            or '\x00' in raw_name
        ):
            return Refused(rule='skill', reason='Invalid skill path')
        candidate = Path(raw_name)
        if candidate.is_absolute() or '..' in candidate.parts or raw_name in ('.', '..'):
            return Refused(rule='skill', reason='Skill path escapes configured skills directory')
        base = self.skills_dir
        if not base.is_dir():
            return Refused(rule='skill', reason='Skills directory does not exist')
        skill_path = (base / candidate).resolve()
        if not skill_path.is_relative_to(base) or skill_path == base:
            return Refused(rule='skill', reason='Skill path escapes configured skills directory')
        if not skill_path.is_dir():
            return Refused(rule='skill', reason='Unknown skill')
        try:
            return Skill.load(skill_path)
        except SkillError as error:
            return Refused(rule='skill', reason=f'Invalid skill: {error}')

    def _run_pre_hooks(
        self,
        skill: Skill,
        raw_input: str,
        script_runs: list[dict[str, Any]],
        meta: dict[str, Any],
    ) -> tuple[str, list[tuple[str, str]]] | Reason:
        current_input: Any = raw_input
        raw_errors: list[tuple[str, str]] = []
        for hook in skill.pre:
            outcome = run_script(
                hook,
                {'input': current_input},
                skill_dir=skill.root,
                env_names=skill.env,
                allowed_env=self.allowed_script_env,
                blocked_env=self.blocked_script_env,
            )
            script_runs.append(dataclasses.asdict(
                ScriptRun(name=hook.name, phase='pre', exit_code=outcome.exit_code, duration_ms=outcome.duration_ms)
            ))
            meta['script_runs'] = list(script_runs)
            if not outcome.ok:
                raw_error = outcome.error or f'script exited with code {outcome.exit_code}'
                if hook.on_error == 'return_to_model':
                    raw_errors.append((hook.name, raw_error))
                    continue
                return 'script_error'
            if outcome.stdout.strip():
                try:
                    parsed = json.loads(outcome.stdout)
                except ValueError:
                    return 'script_error'
                current_input = _unwrap_script_payload(parsed, key='input')
        if isinstance(current_input, str):
            return current_input, raw_errors
        if isinstance(current_input, (dict, list)):
            return json.dumps(current_input, ensure_ascii=False), raw_errors
        return 'script_error'

    def _run_post_hooks(
        self,
        skill: Skill,
        data: Any,
        output_schema: dict[str, Any],
        script_runs: list[dict[str, Any]],
        meta: dict[str, Any],
    ) -> tuple[Any, Reason | None]:
        raw_props = output_schema.get('properties')
        protected = set(raw_props) if isinstance(raw_props, dict) else ()
        current = data
        for hook in skill.post:
            outcome = run_script(
                hook,
                {'output': current},
                skill_dir=skill.root,
                env_names=skill.env,
                allowed_env=self.allowed_script_env,
                blocked_env=self.blocked_script_env,
            )
            script_runs.append(dataclasses.asdict(
                ScriptRun(name=hook.name, phase='post', exit_code=outcome.exit_code, duration_ms=outcome.duration_ms)
            ))
            meta['script_runs'] = list(script_runs)
            if not outcome.ok:
                return None, 'script_error'
            if outcome.stdout.strip():
                try:
                    parsed = json.loads(outcome.stdout)
                except ValueError:
                    return None, 'script_error'
                current = _unwrap_script_payload(parsed, key='output', protected_keys=protected)
        return current, None

    def _run(self, inputs: LLMRunInput, meta: dict[str, Any] | None = None) -> Done | Refused:
        meta = {} if meta is None else meta
        skill: Skill | None = None
        script_runs: list[dict[str, Any]] = []
        if inputs.skill is not None:
            resolved = self._resolve_skill(inputs.skill)
            if isinstance(resolved, Refused):
                return resolved
            skill = resolved
            meta['skill'] = skill.name
            meta['skill_hash'] = skill.skill_hash
            meta['turns'] = 0
            meta['script_runs'] = []
        output_schema = inputs.output_schema if inputs.output_schema is not None else (skill.output_schema if skill is not None else None)
        if output_schema is None:
            return Refused(rule='schema', reason='Output schema is required')
        schema_bytes = json.dumps(output_schema, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
        prompt_bytes = inputs.prompt_template.encode()
        skill_prompt = skill.system_prompt() if skill is not None else ''
        example_bytes = [len(e.input_text.encode()) + len(json.dumps(e.output, ensure_ascii=False, default=str).encode()) for e in inputs.examples]
        total_bytes = (
            sum(map(len, (schema_bytes, prompt_bytes, inputs.input_text.encode(), skill_prompt.encode())))
            + sum(example_bytes)
            + (_tool_metadata_bytes(skill) if skill is not None else 0)
        )
        if total_bytes > self.max_bytes:
            return Refused(rule='size', reason='LLM request exceeds the configured size limit')
        try:
            Draft202012Validator.check_schema(output_schema)
        except SchemaError:
            return Refused(rule='schema', reason='Invalid JSON Schema')
        validator = Draft202012Validator(output_schema, registry=SchemaRegistry())
        try:
            for example in inputs.examples:
                validator.validate(example.output)
        except (ValidationError, Unresolvable):
            return Refused(rule='schema', reason='Example output does not match the schema')
        selected = next(((name, p) for name, p in self.providers.items() if p.model == inputs.model_hint), None) if inputs.model_hint else (self.default, self.providers[self.default])
        if selected is None:
            return Refused(rule='policy', reason='Model is not allowed', failure='policy')
        name, provider = selected
        meta['provider'] = name
        if inputs.temperature_zero and isinstance(provider, OpenAIChat):
            provider = provider.with_temperature(0)
        floor = self.min_retention_by_provider.get(name, 'zdr_enforced' if self.require_zdr else 'unknown')
        require_zdr = floor == 'zdr_enforced' or inputs.require_zdr
        meta['zdr'] = require_zdr
        if not DataPolicy(min_retention='zdr_enforced' if require_zdr else 'unknown').allows(provider.retention):
            return Refused(rule='policy', reason='Provider does not meet retention policy', failure='policy')
        meta['allow_projects'] = len(inputs.allow_projects)
        allow = self._allowed_names(inputs.allow_projects)
        if allow is None:
            return Refused(rule='allow', reason='Only registered Projects can be allowed')
        # The template, schema and skill prompt/tools are fixed texts: a name counts only when it is no ordinary word.
        _, name_check = pseudonymize('', self.registry, options=FIXED_TEXT)
        schema_text = schema_bytes.decode()
        fixed_refusal = _check_fixed_texts(name_check, inputs.prompt_template, output_schema, schema_text, skill, skill_prompt)
        if fixed_refusal is not None:
            return fixed_refusal
        # The template has exactly one substitution marker; no Python formatting is evaluated.
        if inputs.prompt_template.count('{input}') != 1:
            return Refused(rule='template', reason='Template must contain exactly one {input} marker')
        # `strict` hides ids as `[id]` for apps that restore them themselves; here the same session restores them, so a quoted line equals its source.
        options = replace(STRICT, id_restore='original') if inputs.masking_preset == 'strict' else inputs.masking_preset
        prompt_hash, schema_hash = _hash(prompt_bytes), _hash(schema_bytes)
        if inputs.dry_run:
            # Every check before the provider call has passed; nothing is sent or executed.
            masked, session = pseudonymize(withhold(inputs.input_text).text, self.registry, allow=allow, options=options)
            if session.leaks(masked.text):
                return Refused(rule='masking', reason='Unresolved registered names in the masked input')
            meta['dry_run'] = True
            return self._run_result(
                output=None, model=provider.model, provider=name, prompt_hash=prompt_hash, schema_hash=schema_hash,
                tokens=Tokens(0, 0), zdr=require_zdr, output_valid=True, failure=None, dry_run=True,
                skill=skill, turns=0, script_runs=script_runs,
            )
        raw_input = inputs.input_text
        raw_pre_errors: list[tuple[str, str]] = []
        if skill is not None and skill.pre:
            pre_outcome = self._run_pre_hooks(skill, raw_input, script_runs, meta)
            if isinstance(pre_outcome, str):
                return self._run_result(
                    output=None, model=provider.model, provider=name, prompt_hash=prompt_hash, schema_hash=schema_hash,
                    tokens=Tokens(0, 0), zdr=require_zdr, output_valid=False, failure=pre_outcome,
                    skill=skill, turns=0, script_runs=script_runs,
                )
            raw_input, raw_pre_errors = pre_outcome
        masked, session = pseudonymize(withhold(raw_input).text, self.registry, allow=allow, options=options)
        if session.leaks(masked.text):
            return Refused(rule='masking', reason='Unresolved registered names in the masked input')
        pre_notes: list[Message] = []
        for hook_name, raw_error in raw_pre_errors:
            safe_error = mask_for_model(raw_error, session)
            if session.leaks(safe_error):
                return Refused(rule='masking', reason='Unresolved registered names in pre-hook error', failure='leak')
            pre_notes.append({'role': 'user', 'content': json.dumps({'pre_error': hook_name, 'error': safe_error}, ensure_ascii=False)})
        prompt = inputs.prompt_template.replace('{input}', masked.text)
        messages: list[Message] = []
        if skill_prompt:
            messages.append({'role': 'system', 'content': skill_prompt})
        messages.append({'role': 'system', 'content': 'Return JSON matching this schema: ' + schema_text})
        for example in inputs.examples:
            # Same Session as the real input, so one name keeps one placeholder across every turn.
            example_input = session.mask(withhold(example.input_text).text).text
            example_output = session.mask(withhold(json.dumps(example.output, ensure_ascii=False)).text).text
            messages.append({'role': 'user', 'content': inputs.prompt_template.replace('{input}', example_input)})
            messages.append({'role': 'assistant', 'content': example_output})
        messages.append({'role': 'user', 'content': prompt})
        messages.extend(pre_notes)

        tokens = Tokens()
        turns = 1
        completion: Completion
        if skill is None:
            completed = _complete_chat(provider, messages, require_zdr=require_zdr)
            if isinstance(completed, Refused):
                return completed
            completion = completed
            tokens = completion.tokens
        else:
            tool_specs = tuple(tool.to_spec() for tool in skill.tools)
            completion_opt: Completion | None = None
            last_model = provider.model
            for turn in range(1, skill.max_turns + 1):
                turns = turn
                meta['turns'] = turns
                step = _complete_chat(provider, messages, require_zdr=require_zdr, tools=tool_specs)
                if isinstance(step, Refused):
                    return step
                last_model = step.model
                tokens = tokens.add(step.tokens)
                if not step.tool_calls:
                    completion_opt = step
                    break
                if turn >= skill.max_turns:
                    return self._run_result(
                        output=None, model=last_model, provider=name, prompt_hash=prompt_hash, schema_hash=schema_hash,
                        tokens=tokens, zdr=require_zdr, output_valid=False, failure='max_turns_exceeded',
                        skill=skill, turns=turns, script_runs=script_runs,
                    )
                messages.append(assistant_tool_message(step))
                for call in step.tool_calls:
                    tool_outcome = execute_tool_call(
                        call,
                        skill,
                        session=session,
                        allowed_env=self.allowed_script_env,
                        blocked_env=self.blocked_script_env,
                    )
                    if tool_outcome.script_run is not None:
                        script_runs.append(dataclasses.asdict(tool_outcome.script_run))
                        meta['script_runs'] = list(script_runs)
                    if tool_outcome.failure_reason is not None:
                        if tool_outcome.failure_reason == 'leak':
                            return Refused(rule='masking', reason='Tool call or output contains unmasked registered names', failure='leak')
                        return self._run_result(
                            output=None, model=last_model, provider=name, prompt_hash=prompt_hash, schema_hash=schema_hash,
                            tokens=tokens, zdr=require_zdr, output_valid=False, failure=tool_outcome.failure_reason,
                            skill=skill, turns=turns, script_runs=script_runs,
                        )
                    assert tool_outcome.message is not None
                    messages.append(tool_outcome.message)
            assert completion_opt is not None
            completion = completion_opt

        failure: Reason | None = None
        data: Any = None
        repaired = False
        unresolved: list[str] = []
        masked_output: Any = None
        content = strip_fence(completion.content or '')
        if not content.strip():
            failure = 'empty'
        else:
            try:
                data = json.loads(content)
            except ValueError:
                try:
                    if not inputs.repair_quotes:
                        raise
                    data, repaired = json.loads(repair_quotes(content)), True
                except ValueError:
                    failure = 'invalid_json'
        if failure is None:
            try:
                validator.validate(data)
                if session.leaks(data):
                    return Refused(rule='masking', reason='Output contains unmasked registered names', failure='leak')
                if inputs.restore == 'per_field':
                    masked_output = data
                    data, unresolved = session.restore_fields(data)
                else:
                    data = session.restore(data)
                validator.validate(data)
            except Unresolved:
                failure = 'unresolved_placeholder'
            except (ValidationError, Unresolvable):
                failure = 'schema_violation'
        if failure is None and skill is not None and skill.post:
            data, post_failure = self._run_post_hooks(skill, data, output_schema, script_runs, meta)
            if post_failure is not None:
                failure = post_failure
            else:
                try:
                    validator.validate(data)
                except (ValidationError, Unresolvable):
                    failure = 'schema_violation'
        output_valid = failure is None
        if not output_valid:
            data, unresolved, masked_output = None, [], None
        return self._run_result(
            output=data, model=completion.model, provider=name, prompt_hash=prompt_hash, schema_hash=schema_hash,
            tokens=tokens, zdr=require_zdr, output_valid=output_valid, failure=failure,
            repaired=repaired, unresolved=unresolved, masked_output=masked_output,
            skill=skill, turns=turns, script_runs=script_runs,
        )

    @staticmethod
    def _run_result(
        *,
        output: Any,
        model: str,
        provider: str,
        prompt_hash: str,
        schema_hash: str,
        tokens: Tokens,
        zdr: bool,
        output_valid: bool,
        failure: Reason | None,
        repaired: bool = False,
        unresolved: list[str] | None = None,
        masked_output: Any = None,
        dry_run: bool = False,
        skill: Skill | None = None,
        turns: int = 1,
        script_runs: Sequence[dict[str, Any]] = (),
    ) -> Done:
        skill_fields: dict[str, Any] = (
            {
                'skill': skill.name,
                'skill_hash': skill.skill_hash,
                'turns': turns,
                'script_runs': list(script_runs),
            }
            if skill is not None else {}
        )
        return Done(result=LLMRunOutput(
            output=output,
            model=model,
            provider=provider,
            prompt_hash=prompt_hash,
            schema_hash=schema_hash,
            tokens={'prompt': tokens.prompt, 'completion': tokens.completion, 'total': tokens.total},
            zdr=zdr,
            output_valid=output_valid,
            failure=failure,
            repaired=repaired,
            unresolved=unresolved or [],
            masked_output=masked_output,
            dry_run=dry_run,
            **skill_fields,
        ))


def _thresholds(filters: Any) -> dict[str, tuple[float, float]]:
    """The `filters` key: per rule, optional `pass` and `drop` thresholds between 0 and 1. Raises :class:`ConfigError`."""
    if filters is None:
        return {}
    if not isinstance(filters, Mapping):
        raise ConfigError('filters must map a rule name to its pass and drop thresholds')
    result: dict[str, tuple[float, float]] = {}
    for name, entry in filters.items():
        if name not in FILTER_RULES or not isinstance(entry, Mapping) or set(entry) - {'pass', 'drop'}:
            raise ConfigError(f'filters: {name!r} must be a known rule with only pass and drop')
        if any(type(v) not in (int, float) or not 0 <= v <= 1 for v in entry.values()):
            raise ConfigError(f'filters: {name} thresholds must be numbers from 0 to 1')
        rule = FILTER_RULES[name]
        result[name] = (float(entry.get('pass', rule.pass_at)), float(entry.get('drop', rule.drop_at)))
    return result


def _decisions_from(entry: Any, env: Mapping[str, str]) -> OpenRouterDecisions:
    """The optional `decisions` key: url, model, retention (what the endpoint really has) and the key's variable name."""
    if not isinstance(entry, dict) or set(entry) - {'url', 'model', 'retention', 'api_key_env'}:
        raise ConfigError('decisions must be a mapping of url, model, retention and api_key_env')
    retention = entry.get('retention', 'unknown')
    texts = {key: entry.get(key, default) for key, default in (('url', DECISIONS_URL), ('model', JEV_MODEL), ('api_key_env', None))}
    if retention not in RETENTIONS or not all(isinstance(v, str) and v.strip() for v in texts.values()):
        raise ConfigError('decisions: retention must be "unknown" or "zdr_enforced"; url, model and api_key_env must be non-empty text')
    api_key = env.get(texts['api_key_env'] or '', '')
    if not api_key:
        raise ConfigError(f"decisions: environment variable {texts['api_key_env']} is not set")
    return OpenRouterDecisions(api_key=api_key, model=texts['model'] or '', url=texts['url'] or '', retention=retention)


def _valid_env_name(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value == value.strip() and '=' not in value and not any(c.isspace() for c in value)


def _script_env_from(
    raw_allowed: Any,
    env: Mapping[str, str],
    provider_key_envs: Collection[str],
) -> dict[str, str]:
    """Parse optional ``allowed_script_env`` from config, rejecting any provider ``api_key_env``."""
    if raw_allowed is None:
        return {}
    if isinstance(raw_allowed, list):
        if any(not _valid_env_name(name) for name in raw_allowed):
            raise ConfigError('allowed_script_env entries must be valid environment variable names')
        if any(name in provider_key_envs for name in raw_allowed):
            raise ConfigError('allowed_script_env must not include provider api_key_env')
        return {name: env[name] for name in raw_allowed if name in env}
    if isinstance(raw_allowed, Mapping):
        if any(not _valid_env_name(k) or not isinstance(v, str) for k, v in raw_allowed.items()):
            raise ConfigError('allowed_script_env mapping must map valid environment variable names to strings')
        if any(k in provider_key_envs for k in raw_allowed):
            raise ConfigError('allowed_script_env must not include provider api_key_env')
        return {str(k): v for k, v in raw_allowed.items()}
    raise ConfigError('allowed_script_env must be a list of variable names or a mapping')


def _skills_dir_from(raw_skills_dir: Any, config_path: str) -> Path | None:
    if raw_skills_dir is None:
        return None
    if not isinstance(raw_skills_dir, str) or not raw_skills_dir.strip():
        raise ConfigError('skills_dir must be a non-empty string')
    candidate = Path(raw_skills_dir.strip())
    return candidate if candidate.is_absolute() else (Path(config_path).resolve().parent / candidate)


def service_from_env(env: Mapping[str, str]) -> LLMService | None:
    """Load server policy and names from a JSON file; absent configuration disables the verb."""
    path = env.get('T6_LLM_CONFIG')
    if not path:
        return None
    with open(path, encoding='utf-8') as file:
        config = json.load(file)
    llm = LLMConfig.from_mapping(config['llm'])
    providers: dict[str, Provider] = {}
    for name in llm.providers:
        providers[name] = replace(llm, default=name, tasks={}).provider_for('', env=env)
    names = config['privacy_registry']
    privacy = Registry(people=[Person(**p) for p in names.get('people', [])],
                       projects=[Project(**p) for p in names.get('projects', [])], ids=names.get('ids', []))
    decisions = _decisions_from(config['decisions'], env) if 'decisions' in config else None
    floors = config.get('provider_min_retention', {})
    if not isinstance(floors, dict) or any((name not in providers and not (name == 'decisions' and decisions)) or value not in RETENTIONS for name, value in floors.items()):
        raise ConfigError('provider_min_retention must map a configured provider name (or "decisions") to "unknown" or "zdr_enforced"')
    provider_key_envs = {entry.api_key_env for entry in llm.providers.values()}
    if 'decisions' in config and isinstance(config['decisions'].get('api_key_env'), str):
        provider_key_envs.add(config['decisions']['api_key_env'].strip())
    return LLMService(providers, llm.default or '', privacy,
                      require_zdr=config.get('require_zdr', True), max_bytes=config.get('max_bytes', 100_000),
                      min_retention_by_provider=floors, decisions=decisions, filters=config.get('filters'),
                      skills_dir=_skills_dir_from(config.get('skills_dir'), path),
                      allowed_script_env=_script_env_from(config.get('allowed_script_env'), env, provider_key_envs),
                      blocked_script_env=tuple(sorted(provider_key_envs)))
