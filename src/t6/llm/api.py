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
    output_schema: dict[str, Any] | None = None
    input_text: str
    masking_preset: Literal['names', 'strict']
    model_hint: str | None = None
    require_zdr: bool = False
    temperature_zero: bool = False
    examples: list[LLMExample] = []
    allow_projects: list[str] = []
    repair_quotes: bool = False
    restore: Literal['whole', 'per_field'] = 'whole'
    dry_run: bool = False
    skill: str | None = None


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
    dry_run: bool = False
    skill: str | None = None
    skill_hash: str | None = None
    turns: int = 1
    script_runs: list[dict[str, Any]] = []




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


class LLMFilterInput(BaseModel):
    rule: str = Field(min_length=1, max_length=200)
    text: str
    reply_to_text: str | None = None


class LLMFilterOutput(BaseModel):
    verdict: Literal['pass', 'drop', 'unsure']
    probabilities: dict[Literal['match', 'maybe', 'no'], float]
    rule: str
    model: str
    provider: str
    tokens: dict[str, int | None]
    zdr: bool = False


class LLMDecideOutput(BaseModel):
    answers: list[LLMDecideAnswer]
    model: str
    provider: str
    tokens: dict[str, int | None]
    zdr: bool = False
