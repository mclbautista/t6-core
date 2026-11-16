"""In-process API boundary for CLI tests with fake adapters."""
from __future__ import annotations

import json
import os
from typing import Any, Callable
from unittest.mock import patch

from t6.server.verbs import VerbAPI
from t6.verbs import VERBS, Outcome, Refused, invoke
from pydantic import TypeAdapter

_outcome: TypeAdapter[Outcome] = TypeAdapter(Outcome)


def client_patch(open_notion: Callable[..., Any]) -> Any:
    def call(_client: Any, noun: str, verb: str, inputs: Any) -> Any:
        from t6.verbs import _people
        people = _people()
        entry = next(v for v in VERBS if (v.noun, v.verb) == (noun, verb))
        actor = os.environ.get('T6_PERSON_ID', '')
        if (noun, verb) in {('person', 'get'), ('departments', '')}:
            # Like Client.call: the server reads with its own data sources, so the file is not sent.
            sources = json.loads(inputs.data_sources.read_text(encoding='utf-8')) if getattr(inputs, 'data_sources', None) else {}
            inputs = entry.input_model.model_validate(inputs.model_dump(exclude={'data_sources'}))
            api = object.__new__(VerbAPI)
            api.people = people
            api.open_notion = open_notion
            api.data_sources = sources
            try:
                outcome = people.authorize(actor, entry.role) or api._typed_read(noun, verb, inputs)
            except (OSError, ValueError, KeyError) as error:
                outcome = Refused(rule='read', reason=str(error))
            return _outcome.validate_python(outcome.model_dump(mode='json'))
        if entry.handler is not None:
            result = invoke(entry, inputs, actor=actor, people=people, open_notion=open_notion)
            return _outcome.validate_python(result.model_dump(mode="json"))
        api = object.__new__(VerbAPI)
        api.people = people
        api.open_notion = open_notion
        if (noun, verb) == ('camera', 'enrich'):
            api.data_sources = {'cameras': 'ds-cameras'}
            return people.authorize(actor, entry.role) or api._camera_enrich(inputs, actor)
        return people.authorize(actor, entry.role) or api._legacy_read(noun, verb, inputs)
    return patch('t6.client.Client.call', new=call)
