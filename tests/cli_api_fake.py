"""In-process API boundary for CLI tests with fake adapters."""
from __future__ import annotations

import os
from typing import Any, Callable
from unittest.mock import patch

from t6.server.verbs import VerbAPI
from t6.verbs import VERBS, Outcome, invoke
from pydantic import TypeAdapter

_outcome: TypeAdapter[Outcome] = TypeAdapter(Outcome)


def client_patch(open_notion: Callable[..., Any]) -> Any:
    def call(_client: Any, noun: str, verb: str, inputs: Any) -> Any:
        from t6.verbs import _people
        people = _people()
        entry = next(v for v in VERBS if (v.noun, v.verb) == (noun, verb))
        actor = os.environ.get('T6_PERSON_ID', '')
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
