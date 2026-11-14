"""Shared, JSON-serializable shapes for LLM eval cases and run records.

Apps own their prompts and synthetic cases. Real cases and runs belong off-repo;
bringing a real case into a repo requires privacy masking, a names denylist
check and human review. These models record provenance, not those checks.
Grader names identify app-owned checks; expected properties describe behavior,
not an exact output.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints

NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Grader = NonEmpty
"""Name of the grader to use for a case."""


class SyntheticProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["synthetic"]


class RealProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["real"]
    reviewer: NonEmpty
    date: date


Provenance = Annotated[SyntheticProvenance | RealProvenance, Field(discriminator="kind")]


class Case(BaseModel):
    """A masked task input and properties its output should satisfy."""

    model_config = ConfigDict(extra="forbid")

    id: NonEmpty
    task: NonEmpty
    masked_input: JsonValue
    expected_properties: Annotated[list[NonEmpty], Field(min_length=1)]
    grader: Grader
    provenance: Provenance


class CaseResult(BaseModel):
    """The outcome and short explanation for one case."""

    model_config = ConfigDict(extra="forbid")

    case_id: NonEmpty
    passed: Annotated[bool, Field(strict=True)]
    reason: NonEmpty


class RunRecord(BaseModel):
    """Evidence for one eval run, stored as one JSON file."""

    model_config = ConfigDict(extra="forbid")

    model_id: NonEmpty
    provider: NonEmpty
    prompt_hash: NonEmpty
    schema_hash: NonEmpty
    t6_version: NonEmpty
    date: date
    results: list[CaseResult]

    def write(self, path: str | Path) -> None:
        """Write the run record to a JSON file."""
        Path(path).write_text(self.model_dump_json(indent=2) + "\n", encoding="utf-8")

    @classmethod
    def read(cls, path: str | Path) -> RunRecord:
        """Read and validate a run-record JSON file."""
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))


__all__ = ["Case", "CaseResult", "Grader", "Provenance", "RealProvenance", "RunRecord", "SyntheticProvenance"]
