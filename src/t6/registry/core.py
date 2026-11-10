"""The Project registry contract: the types and the interface, with no storage."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
import re
from typing import Protocol, TypeAlias

from t6.domain.events import JsonValue

__all__ = [
    "NOTION",
    "PROJECT_ID_PREFIX",
    "AuditEntry",
    "KeyReused",
    "PageAlreadyBound",
    "PageBinding",
    "ProjectRegistry",
    "RegisterOutcome",
    "Registered",
    "RegisteredProject",
    "RegistryError",
    "Rotation",
    "SyncIntent",
    "SyncStatus",
    "normalize_page_id",
]

PROJECT_ID_PREFIX = "t6p_"
"""Every T6 Project ID starts with this. The rest is opaque."""

NOTION = "notion"

_UUID = re.compile(r"[0-9a-f]{32}")


class RegistryError(ValueError):
    """A bad argument, such as a blank actor or idempotency key."""


class Rotation(StrEnum):
    IN = "in"
    OUT = "out"


class SyncStatus(StrEnum):
    PENDING = "pending"
    """An accepted change has not reached Notion yet."""
    SYNCED = "synced"


def normalize_page_id(page_id: str) -> str:
    """One spelling per page: a Notion UUID loses its dashes and case."""
    text = page_id.strip().lower()
    plain = text.replace("-", "")
    return plain if _UUID.fullmatch(plain) else text


@dataclass(frozen=True, slots=True)
class PageBinding:
    """An external page that stands for a Project. A page belongs to one Project only."""

    system: str
    page_id: str
    bound_at: float


@dataclass(frozen=True, slots=True)
class RegisteredProject:
    id: str
    """The T6 Project ID. It never changes."""
    revision: int
    """Goes up by one with every accepted change."""
    code: str | None
    rotation: Rotation
    bindings: tuple[PageBinding, ...]
    sync: SyncStatus
    created_at: float


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """One accepted change. Entries are only ever added."""

    project_id: str
    action: str
    actor: str
    at: float
    before: Mapping[str, JsonValue] | None
    after: Mapping[str, JsonValue]


@dataclass(frozen=True, slots=True)
class SyncIntent:
    """A committed change that still has to be shown in Notion."""

    id: int
    project_id: str
    revision: int
    created_at: float


@dataclass(frozen=True, slots=True)
class Registered:
    project: RegisteredProject
    created: bool
    """False when the idempotency key was seen before: this is the original Project."""


@dataclass(frozen=True, slots=True)
class PageAlreadyBound:
    page_id: str
    project_id: str
    """The Project that holds the page."""


@dataclass(frozen=True, slots=True)
class KeyReused:
    """The idempotency key was already used for a different request."""

    idempotency_key: str


RegisterOutcome: TypeAlias = Registered | PageAlreadyBound | KeyReused


class ProjectRegistry(Protocol):
    def register(
        self, *, actor: str, idempotency_key: str, notion_page_id: str | None = None
    ) -> RegisterOutcome:
        """Mint a Project, out of rotation and without a code. Never matches by code or name."""
        ...

    def get(self, project_id: str) -> RegisteredProject | None: ...

    def audit(self, project_id: str) -> tuple[AuditEntry, ...]:
        """The Project's audit entries, oldest first."""
        ...

    def pending_sync(self) -> tuple[SyncIntent, ...]:
        """Sync intents not yet done, oldest first."""
        ...
