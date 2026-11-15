"""Closed-choice PM Notes matching task sent through llm run."""
from __future__ import annotations

import json
from typing import Any

from t6.verbs import LLMRunInput, Done

PROMPT = '''Classify the single PM Notes line. A Concern is a delay or risk waiting on a person or party, not a general observation. Choose continues or settles only for a matching offered Concern ID, new only for a line waiting on someone, otherwise nothing. Rewording of an existing Concern continues it. Respect human_decision: match it to the existing ID rather than creating a duplicate. Return a verbatim quote from the line. For new, waiting_on must be the exact words in the line naming the party. Input: {input}'''
SCHEMA: dict[str, Any] = {
    'type': 'object', 'additionalProperties': False,
    'properties': {'choice': {'enum': ['continues', 'settles', 'new', 'nothing']},
                   'quote': {'type': 'string', 'minLength': 1},
                   'concern_id': {'type': ['string', 'null']}, 'waiting_on': {'type': ['string', 'null']}},
    'required': ['choice', 'quote'],
}


def classify(service: Any, payload: dict[str, Any], *, actor: str, registry: Any) -> Any:
    inputs = LLMRunInput(task_name='concern-pm-notes', prompt_template=PROMPT, output_schema=SCHEMA,
                         input_text=json.dumps(payload, ensure_ascii=False), masking_preset='names', require_zdr=True, temperature_zero=True)
    result = service.run(inputs, actor=actor, registry=registry)
    return result.result.output if isinstance(result, Done) and result.result.output_valid else None
