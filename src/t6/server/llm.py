"""Server-owned LLM execution, configuration and privacy boundary."""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import replace
from typing import Any, Literal, Mapping

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from referencing.exceptions import Unresolvable
from referencing import Registry as SchemaRegistry

from t6.domain.message_filter import FILTER_RULES, verdict
from t6.llm.config import ConfigError, LLMConfig
from t6.llm.decisions import DECISIONS_URL, JEV_MODEL, CandidateDecisions, MalformedReply, OpenRouterDecisions, candidate_key
from t6.llm.jsontext import repair_quotes, strip_fence
from t6.llm.policy import RETENTIONS, DataPolicy, Retention
from t6.llm.transport import Choice, Completion, DecisionsRequest, Message, OpenAIChat, Provider, ProviderError, ProviderTimeout, RateLimited
from t6.llm.result import Reason
from t6.verbs import Done, LLMDecideInput, LLMDecideOutput, LLMFilterInput, LLMFilterOutput, LLMRunInput, LLMRunOutput, Refused
from t6.privacy import FIXED_TEXT, STRICT, Person, Project, Registry, Unresolved, pseudonymize, withhold


def _hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class LLMService:
    def __init__(self, providers: Mapping[str, Provider], default: str, registry: Registry,
                 *, require_zdr: bool = True, max_bytes: int = 100_000,
                 min_retention_by_provider: Mapping[str, Retention] | None = None,
                 decisions: OpenRouterDecisions | None = None, filters: Any = None) -> None:
        if default not in providers or type(require_zdr) is not bool or type(max_bytes) is not int or max_bytes < 1:
            raise ValueError('Invalid LLM policy')
        floors = dict(min_retention_by_provider or {})
        if any((name not in providers and not (name == 'decisions' and decisions)) or value not in RETENTIONS for name, value in floors.items()):
            raise ValueError('Invalid LLM policy')
        self.decisions = decisions
        self.thresholds = _thresholds(filters)
        self.min_retention_by_provider = floors
        self.providers, self.default, self.registry = dict(providers), default, registry
        self.require_zdr, self.max_bytes = require_zdr, max_bytes

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

    def _run(self, inputs: LLMRunInput, meta: dict[str, Any] | None = None) -> Done | Refused:
        meta = {} if meta is None else meta
        schema_bytes = json.dumps(inputs.output_schema, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
        prompt_bytes = inputs.prompt_template.encode()
        example_bytes = [len(e.input_text.encode()) + len(json.dumps(e.output, ensure_ascii=False, default=str).encode()) for e in inputs.examples]
        if sum(map(len, (schema_bytes, prompt_bytes, inputs.input_text.encode()))) + sum(example_bytes) > self.max_bytes:
            return Refused(rule='size', reason='LLM request exceeds the configured size limit')
        try:
            Draft202012Validator.check_schema(inputs.output_schema)
        except SchemaError:
            return Refused(rule='schema', reason='Invalid JSON Schema')
        validator = Draft202012Validator(inputs.output_schema, registry=SchemaRegistry())
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
        # `strict` hides ids as `[id]` for apps that restore them themselves; here the same session restores them, so a quoted line equals its source.
        options = replace(STRICT, id_restore='original') if inputs.masking_preset == 'strict' else inputs.masking_preset
        masked, session = pseudonymize(withhold(inputs.input_text).text, self.registry, allow=allow, options=options)
        # The template and schema are texts the app wrote: a name counts only when it is no ordinary word. The masked
        # input and the reply are checked the way the caller's preset masks.
        _, name_check = pseudonymize('', self.registry, options=FIXED_TEXT)
        schema_text = schema_bytes.decode()
        if name_check.leaks(inputs.prompt_template) or withhold(inputs.prompt_template).text != inputs.prompt_template:
            return Refused(rule='masking', reason='Unresolved registered names in the prompt template')
        # The serialised text also holds the property names, which `leaks` does not read from a dict.
        if name_check.leaks(inputs.output_schema) or name_check.leaks(schema_text) or withhold(schema_text).text != schema_text:
            return Refused(rule='masking', reason='Unresolved registered names in the output schema')
        if session.leaks(masked.text):
            return Refused(rule='masking', reason='Unresolved registered names in the masked input')
        # The template has exactly one substitution marker; no Python formatting is evaluated.
        if inputs.prompt_template.count('{input}') != 1:
            return Refused(rule='template', reason='Template must contain exactly one {input} marker')
        if inputs.dry_run:
            # Every check before the provider call has passed; nothing is sent.
            meta['dry_run'] = True
            return Done(result=LLMRunOutput(output=None, model=provider.model, provider=name,
                        prompt_hash=_hash(prompt_bytes), schema_hash=_hash(schema_bytes),
                        tokens={'prompt': 0, 'completion': 0, 'total': 0}, zdr=require_zdr, output_valid=True, failure=None, dry_run=True))
        prompt = inputs.prompt_template.replace('{input}', masked.text)
        messages: list[Message] = [{'role': 'system', 'content': 'Return JSON matching this schema: ' + schema_bytes.decode()}]
        for example in inputs.examples:
            # Same Session as the real input, so one name keeps one placeholder across every turn.
            example_input = session.mask(withhold(example.input_text).text).text
            example_output = session.mask(withhold(json.dumps(example.output, ensure_ascii=False)).text).text
            messages.append({'role': 'user', 'content': inputs.prompt_template.replace('{input}', example_input)})
            messages.append({'role': 'assistant', 'content': example_output})
        messages.append({'role': 'user', 'content': prompt})
        try:
            completion = provider.complete(messages, timeout=60, zdr=require_zdr)
        except ProviderTimeout:
            return Refused(rule='provider', reason='LLM provider failed', failure='timeout')
        except RateLimited as limited:
            return Refused(rule='provider', reason='LLM provider failed', failure='api_error', retry_after=limited.retry_after)
        except ProviderError:
            return Refused(rule='provider', reason='LLM provider failed', failure='api_error')
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
        output_valid = failure is None
        if not output_valid:
            data, unresolved, masked_output = None, [], None
        return Done(result=LLMRunOutput(output=data, model=completion.model, provider=name,
                    prompt_hash=_hash(prompt_bytes), schema_hash=_hash(schema_bytes),
                    tokens={'prompt': completion.tokens.prompt, 'completion': completion.tokens.completion,
                            'total': completion.tokens.total}, zdr=require_zdr, output_valid=output_valid, failure=failure,
                    repaired=repaired, unresolved=unresolved, masked_output=masked_output))


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
        from dataclasses import replace
        providers[name] = replace(llm, default=name, tasks={}).provider_for('', env=env)
    names = config['privacy_registry']
    privacy = Registry(people=[Person(**p) for p in names.get('people', [])],
                       projects=[Project(**p) for p in names.get('projects', [])], ids=names.get('ids', []))
    decisions = _decisions_from(config['decisions'], env) if 'decisions' in config else None
    floors = config.get('provider_min_retention', {})
    if not isinstance(floors, dict) or any((name not in providers and not (name == 'decisions' and decisions)) or value not in RETENTIONS for name, value in floors.items()):
        raise ConfigError('provider_min_retention must map a configured provider name (or "decisions") to "unknown" or "zdr_enforced"')
    return LLMService(providers, llm.default or '', privacy,
                      require_zdr=config.get('require_zdr', True), max_bytes=config.get('max_bytes', 100_000),
                      min_retention_by_provider=floors, decisions=decisions, filters=config.get('filters'))
