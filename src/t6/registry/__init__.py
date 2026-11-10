"""The Project registry: T6-owned Project identity. Standard library only.

The registry, not Notion, says which Projects exist. It issues each Project an
immutable T6 Project ID and holds its code, rotation, page bindings and audit
history (see `docs/adr/0001-project-registry-owns-project-identity.md`).
`ProjectRegistry` is the contract; `SqliteProjectRegistry` is the durable
implementation the server runs.
"""

from t6.registry.core import (
    NOTION,
    PROJECT_ID_PREFIX,
    AuditEntry,
    Changed,
    ChangeOutcome,
    CodeHeld,
    KeyReused,
    NotFound,
    Observed,
    PageAlreadyBound,
    PageBinding,
    ProjectRegistry,
    RegisterOutcome,
    Registered,
    RegisteredProject,
    RegistryError,
    RegistryUnavailable,
    ReviewItem,
    RevisionConflict,
    Rotation,
    Shown,
    SyncIntent,
    SyncStatus,
    normalize_code,
    normalize_page_id,
)
from t6.registry.refs import ProjectRefs
from t6.registry.sqlite import SqliteProjectRegistry

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
    "ProjectRefs",
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
    "SqliteProjectRegistry",
    "SyncIntent",
    "SyncStatus",
    "normalize_code",
    "normalize_page_id",
]
