"""Closed-choice PM Notes matching task sent through llm run."""
from __future__ import annotations

import json
from typing import Any

from t6.verbs import LLMRunInput, Refused

PROMPT = '''Classify the single PM Notes line. A Concern is a delay or risk waiting on a person or party, not a general observation. Choose continues or settles only for a matching offered Concern ID, new only for a line waiting on someone, otherwise nothing. Rewording of an existing Concern continues it. Respect human_decision: match it to the existing ID rather than creating a duplicate. Return a verbatim quote from the line. For new, waiting_on must be the exact words in the line naming the party. Input: {input}'''
SCHEMA: dict[str, Any] = {
    'type': 'object', 'additionalProperties': False,
    'properties': {'choice': {'enum': ['continues', 'settles', 'new', 'nothing']},
                   'quote': {'type': 'string', 'minLength': 1},
                   'concern_id': {'type': ['string', 'null']}, 'waiting_on': {'type': ['string', 'null']}},
    'required': ['choice', 'quote'],
}


class ClassifyRefused(Exception):
    """`llm run` refused the request: the same refusal holds for every other line, so the caller stops."""

    def __init__(self, rule: str) -> None:
        super().__init__(rule)
        self.rule = rule


def classify(service: Any, payload: dict[str, Any], *, actor: str, registry: Any) -> Any:
    inputs = LLMRunInput(task_name='concern-pm-notes', prompt_template=PROMPT, output_schema=SCHEMA,
                         input_text=json.dumps(payload, ensure_ascii=False), masking_preset='names', require_zdr=True, temperature_zero=True)
    result = service.run(inputs, actor=actor, registry=registry)
    if isinstance(result, Refused):
        raise ClassifyRefused(result.rule)
    # An invalid reply is an answer the scanner counts and gives up on after MAX_ATTEMPTS (#504); None would mean "try again forever".
    return result.result.output if result.result.output_valid else {}
