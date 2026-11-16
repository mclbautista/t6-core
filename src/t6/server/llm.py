"""Server-owned LLM execution, configuration and privacy boundary."""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Literal, Mapping

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from referencing.exceptions import Unresolvable
from referencing import Registry as SchemaRegistry

from t6.llm.config import ConfigError, LLMConfig
from t6.llm.jsontext import strip_fence
from t6.llm.policy import RETENTIONS, DataPolicy, Retention
from t6.llm.transport import Message, OpenAIChat, Provider, ProviderError, ProviderTimeout, RateLimited
from t6.llm.result import Reason
from t6.verbs import Done, LLMRunInput, LLMRunOutput, Refused
from t6.privacy import Person, Project, Registry, Unresolved, pseudonymize, withhold


def _hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class LLMService:
    def __init__(self, providers: Mapping[str, Provider], default: str, registry: Registry,
                 *, require_zdr: bool = True, max_bytes: int = 100_000,
                 min_retention_by_provider: Mapping[str, Retention] | None = None) -> None:
        if default not in providers or type(require_zdr) is not bool or type(max_bytes) is not int or max_bytes < 1:
            raise ValueError('Invalid LLM policy')
        floors = dict(min_retention_by_provider or {})
        if any(name not in providers or value not in RETENTIONS for name, value in floors.items()):
            raise ValueError('Invalid LLM policy')
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
                        json.dumps({'outcome': outcome.outcome, **meta})))
        return outcome

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
        masked, session = pseudonymize(withhold(inputs.input_text).text, self.registry, options=inputs.masking_preset)
        _, name_check = pseudonymize('', self.registry, options='names')
        if (name_check.leaks(masked.text) or name_check.leaks(inputs.prompt_template)
                or name_check.leaks(inputs.output_schema)
                or withhold(inputs.prompt_template).text != inputs.prompt_template
                or withhold(schema_bytes.decode()).text != schema_bytes.decode()):
            return Refused(rule='masking', reason='Unresolved registered names')
        # The template has exactly one substitution marker; no Python formatting is evaluated.
        if inputs.prompt_template.count('{input}') != 1:
            return Refused(rule='template', reason='Template must contain exactly one {input} marker')
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
        content = completion.content or ''
        if not content.strip():
            failure = 'empty'
        else:
            try:
                data = json.loads(strip_fence(content))
            except ValueError:
                failure = 'invalid_json'
        if failure is None:
            try:
                validator.validate(data)
                if name_check.leaks(data):
                    return Refused(rule='masking', reason='Output contains unmasked registered names', failure='leak')
                data = session.restore(data)
                validator.validate(data)
            except Unresolved:
                failure = 'unresolved_placeholder'
            except (ValidationError, Unresolvable):
                failure = 'schema_violation'
        output_valid = failure is None
        if not output_valid:
            data = None
        return Done(result=LLMRunOutput(output=data, model=completion.model, provider=name,
                    prompt_hash=_hash(prompt_bytes), schema_hash=_hash(schema_bytes),
                    tokens={'prompt': completion.tokens.prompt, 'completion': completion.tokens.completion,
                            'total': completion.tokens.total}, zdr=require_zdr, output_valid=output_valid, failure=failure))


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
    floors = config.get('provider_min_retention', {})
    if not isinstance(floors, dict) or any(name not in providers or value not in RETENTIONS for name, value in floors.items()):
        raise ConfigError('provider_min_retention must map a configured provider name to "unknown" or "zdr_enforced"')
    return LLMService(providers, llm.default or '', privacy,
                      require_zdr=config.get('require_zdr', True), max_bytes=config.get('max_bytes', 100_000),
                      min_retention_by_provider=floors)
