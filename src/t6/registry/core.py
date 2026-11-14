"""The Project registry contract: the types and the interface, with no storage."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
import re
from typing import Any, Protocol, TypeAlias

from t6.domain.events import JsonValue
from t6.lookup.normalize import Normalizers

__all__ = [
    "NOTION",
    "PROJECT_ID_PREFIX",
    "AuditEntry",
    "ChangeOutcome",
    "Changed",
    "CodeHeld",
    "KeyReused",
    "NotFound",
    "Observed",
    "PageAlreadyBound",
    "PageBinding",
    "ProjectRegistry",
    "RegisterOutcome",
    "Registered",
    "RegisteredProject",
    "RegistryError",
    "RegistryUnavailable",
    "ReviewItem",
    "RevisionConflict",
    "Rotation",
    "Shown",
    "SyncIntent",
    "SyncStatus",
    "normalize_code",
    "normalize_page_id",
]

PROJECT_ID_PREFIX = "t6p_"
"""Every T6 Project ID starts with this. The rest is opaque."""

NOTION = "notion"

_UUID = re.compile(r"[0-9a-f]{32}")


class RegistryError(ValueError):
    """A bad argument, such as a blank actor or idempotency key."""


class RegistryUnavailable(Exception):
    """The registry cannot be reached. Nothing was changed and nothing may be assumed."""


class Rotation(StrEnum):
    IN = "in"
    """Holds its code: no other Project may take it."""
    OUT = "out"
    """Keeps its code on record but does not hold it."""
    UNCLASSIFIED = "unclassified"
    """Imported and not reviewed yet. Blocks its code until someone classifies it."""


class SyncStatus(StrEnum):
    PENDING = "pending"
    """An accepted change has not reached Notion yet."""
    SYNCED = "synced"
    FAILED = "failed"
    """The sync job used up its attempts. The change stands; Notion does not show it."""


def normalize_code(code: str | None) -> tuple[str | None, str | None]:
    """A code as entered (trimmed) and its comparison form. Blank is no code.

    The comparison form is `t6.lookup`'s code normalization, so `BH-101` and
    `bh 101` are the same code here and in lookup.
    """
    if code is None or not code.strip():
        return None, None
    form = Normalizers().code(code)
    if not form:
        raise RegistryError("a code needs at least one letter or digit")
    return code.strip(), form


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
    active: bool = True
    """False for a legacy binding: a replaced page that still resolves to its Project."""
    available: bool = True
    """False while the page is deleted or out of reach. The binding itself stays."""


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
    sync_error: str | None = None
    """Why the last sync attempt failed, while a sync is pending or failed."""


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
class ActivityEntry:
    """One Project activity, with an optional link to its raw audit record."""

    project_id: str
    subject_id: str | None
    at: float
    actor: str
    action: str
    summary: str
    audit_id: int | None


@dataclass(frozen=True, slots=True)
class SyncIntent:
    """A committed change that still has to be shown in Notion."""

    id: int
    project_id: str
    revision: int
    created_at: float
    status_held: bool | None = None
    """Requested Project Status projection, or None for an identity intent."""


@dataclass(frozen=True, slots=True)
class Shown:
    """The registry-owned values a Notion page displays right now."""

    t6_id: str | None
    code: str | None
    rotation: str | None


@dataclass(frozen=True, slots=True)
class ReviewItem:
    """Something a person has to look at. Open until the cause goes away."""

    id: int
    kind: str
    """`unbound_page`, `projection_conflict` or `page_unavailable`."""
    page_id: str
    project_id: str | None
    detail: Mapping[str, JsonValue]
    created_at: float


@dataclass(frozen=True, slots=True)
class Observed:
    """What the registry made of a page as Notion shows it now."""

    outcome: str
    """`in_sync`, `pending` (a sync is on its way), `conflict`, `unbound`,
    `unavailable`, `restored`, `legacy` or `ignored`."""
    project: RegisteredProject | None


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


@dataclass(frozen=True, slots=True)
class CodeHeld:
    """The code is taken, so the Project cannot hold it in rotation. Nothing changed."""

    code: str
    held_by: tuple[str, ...]
    """The Projects in the way, oldest first."""
    reason: str
    """`in_rotation`, or `unclassified` when only unreviewed imports hold it."""


@dataclass(frozen=True, slots=True)
class NotFound:
    project_id: str


@dataclass(frozen=True, slots=True)
class RevisionConflict:
    """The Project changed since the caller read it."""

    project_id: str
    revision: int
    """The Project's revision now."""


@dataclass(frozen=True, slots=True)
class Changed:
    project: RegisteredProject
    changed: bool
    """False when the Project already had the value, or for a replay."""
    replayed: bool = False
    """True when the idempotency key was seen before: nothing was done again."""


RegisterOutcome: TypeAlias = Registered | PageAlreadyBound | KeyReused | CodeHeld
ChangeOutcome: TypeAlias = Changed | NotFound | RevisionConflict | CodeHeld | PageAlreadyBound | KeyReused


class ProjectRegistry(Protocol):
    def register(
        self,
        *,
        actor: str,
        idempotency_key: str,
        notion_page_id: str | None = None,
        code: str | None = None,
        rotation: Rotation = Rotation.OUT,
    ) -> RegisterOutcome:
        """Mint a Project. Never matches by code or name."""
        ...

    def set_code(
        self, project_id: str, code: str | None, *, actor: str, idempotency_key: str, expected_revision: int
    ) -> ChangeOutcome:
        """Change the code, or clear it with `None`. Checked when the Project is in rotation."""
        ...

    def set_rotation(
        self, project_id: str, rotation: Rotation, *, actor: str, idempotency_key: str, expected_revision: int
    ) -> ChangeOutcome:
        """Enter or leave rotation. Also classifies an unclassified Project."""
        ...

    def bind_page(
        self, project_id: str, notion_page_id: str, *, actor: str, idempotency_key: str, expected_revision: int
    ) -> ChangeOutcome:
        """Make this page the Project's active page. Earlier pages stay as legacy bindings."""
        ...

    def get(self, project_id: str) -> RegisteredProject | None: ...

    def list_projects(self, rotation: Rotation | None = None) -> tuple[RegisteredProject, ...]:
        """Every Project, or those in one rotation state, oldest first."""
        ...

    def get_by_page(self, notion_page_id: str) -> RegisteredProject | None:
        """The Project a page is bound to, through an active or a legacy binding."""
        ...

    def audit(self, project_id: str) -> tuple[AuditEntry, ...]:
        """The Project's audit entries, oldest first."""
        ...

    def activity(self, project_id: str, *, since: float | None = None, limit: int | None = None) -> tuple[ActivityEntry, ...]:
        """The Project activity, newest first."""
        ...

    def pending_sync(self) -> tuple[SyncIntent, ...]:
        """Sync intents not yet done, oldest first."""
        ...

    def undispatched_sync(self) -> tuple[SyncIntent, ...]:
        """Sync intents that have no queue job yet, oldest first."""
        ...

    def undispatched_jo_sync(self) -> tuple[SyncIntent, ...]: ...
    def undispatched_status_sync(self) -> tuple[SyncIntent, ...]: ...
    def status_sync_state(self, intent_id: int) -> dict[str, Any] | None: ...
    def report_status_sync(self, intent_id: int) -> None: ...
    def jo_sync_state(self, intent_id: int) -> dict[str, Any] | None: ...
    def begin_jo_create(self, intent_id: int) -> bool: ...
    def report_jo_sync(self, intent_id: int, *, page_id: str | None = None, error: str | None = None) -> bool: ...

    def mark_dispatched(self, intent_ids: Iterable[int]) -> None: ...

    def report_sync(
        self, project_id: str, revision: int, *, ok: bool, detail: str = "", final: bool = False
    ) -> RegisteredProject | None:
        """Record that Notion shows `revision`, or why it does not. Never changes the Project."""
        ...

    def observe_page(
        self, notion_page_id: str, *, actor: str, available: bool, shown: Shown | None = None,
        editor: str | None = None, changed: tuple[str, ...] | None = None, editor_id: str | None = None
    ) -> Observed:
        """Reconcile a page's current Notion state with the registry. Registers nothing."""
        ...

    def review_items(self) -> tuple[ReviewItem, ...]:
        """Open review items, oldest first."""
        ...
