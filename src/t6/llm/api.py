"""Shared LLM API request, response, and success models."""
from typing import Any, Literal

from pydantic import BaseModel, Field


class Done(BaseModel):
    outcome: Literal["done"] = "done"
    result: Any


class LLMRunInput(BaseModel):
    task_name: str = Field(min_length=1, max_length=200)
    prompt_template: str
    output_schema: dict[str, Any]
    input_text: str
    masking_preset: Literal['names', 'strict']
    model_hint: str | None = None
    require_zdr: bool = False
    temperature_zero: bool = False


class LLMRunOutput(BaseModel):
    output: Any
    model: str
    provider: str
    prompt_hash: str
    schema_hash: str
    tokens: dict[str, int | None]
    zdr: bool = False
    output_valid: bool = True


