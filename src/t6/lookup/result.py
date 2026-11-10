"""The one result shape every lookup returns."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Literal, TypeVar

from t6.domain import NotionEntity

E_co = TypeVar("E_co", bound=NotionEntity, covariant=True)

Method = Literal["exact_id", "normalized", "fuzzy", "llm", "review", "none"]
"""What decided. `review`: left to a human. `none`: nothing matched at all."""


@dataclass(frozen=True, slots=True)
class Candidate(Generic[E_co]):
    """An entity the mention might mean, with how well it matched (0 to 1)."""

    entity: E_co
    score: float


@dataclass(frozen=True, slots=True)
class Resolution(Generic[E_co]):
    """The answer to one lookup.

    `entity` is set only when a strategy was confident. Otherwise it is `None`,
    and `needs_review` says whether a human should look at `candidates`
    (ranked best first). `score` is the deciding strategy's score, or the best
    candidate's when undecided.
    """

    entity: E_co | None
    method: Method
    score: float
    candidates: tuple[Candidate[E_co], ...]
    needs_review: bool
    reason: str | None = None
    """Why a Project Code lookup went to review, when it is not the usual tie:
    `no_in_rotation_holder`, `several_in_rotation_holders` or `unclassified`."""
