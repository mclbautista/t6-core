"""Shared LLM API request, response, and success models."""
from typing import Any, Literal

from pydantic import BaseModel, Field

from t6.llm.result import Reason


class Done(BaseModel):
    outcome: Literal["done"] = "done"
    result: Any


class LLMExample(BaseModel):
    input_text: str
    output: Any


class LLMRunInput(BaseModel):
    task_name: str = Field(min_length=1, max_length=200)
    prompt_template: str
    output_schema: dict[str, Any]
    input_text: str
    masking_preset: Literal['names', 'strict']
    model_hint: str | None = None
    require_zdr: bool = False
    temperature_zero: bool = False
    examples: list[LLMExample] = []
    allow_projects: list[str] = []
    repair_quotes: bool = False
    restore: Literal['whole', 'per_field'] = 'whole'


class LLMRunOutput(BaseModel):
    output: Any
    model: str
    provider: str
    prompt_hash: str
    schema_hash: str
    tokens: dict[str, int | None]
    zdr: bool = False
    output_valid: bool = True
    failure: Reason | None = None
    repaired: bool = False
    unresolved: list[str] = []
    masked_output: Any = None




class LLMDecideInput(BaseModel):
    task_name: str = Field(min_length=1, max_length=200)
    state: dict[str, str]
    question: str
    criteria: dict[str, str]
    candidates: list[str]
    model_hint: str | None = None
    require_zdr: bool = False


class LLMDecideAnswer(BaseModel):
    index: int
    choice: Literal['match', 'maybe', 'no']
    probabilities: dict[Literal['match', 'maybe', 'no'], float]


class LLMDecideOutput(BaseModel):
    answers: list[LLMDecideAnswer]
    model: str
    provider: str
    tokens: dict[str, int | None]
    zdr: bool = False
