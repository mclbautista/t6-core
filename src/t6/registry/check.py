"""Audit the registry against the Notion Projects pages. Standard library only.

This is the Project half of `t6 schema-check`. It reads the registry and the
page IDs Notion shows and reports each kind of problem under its own name, so
each has its own action. It writes nothing.

What is **not** a finding: a Project with no code, in rotation or not, and a
code that older Projects keep on record while one other Project holds it.
Those are valid since the registry (spec #67 supersedes the blanket blank and
duplicate rules of #10 and #12).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from t6.domain.events import JsonValue
from t6.registry.core import ProjectRegistry, Rotation, normalize_code, normalize_page_id

__all__ = ["KINDS", "ProjectFindings", "audit_projects"]

KINDS = (
    "active_collisions",
    "unclassified",
    "unbound_pages",
    "unavailable_bindings",
    "missing_pages",
    "projection_conflicts",
)
"""Finding kinds, in report order. Each one is a list in the report; empty is good."""


@dataclass(frozen=True, slots=True)
class ProjectFindings:
    active_collisions: tuple[dict[str, JsonValue], ...] = ()
    """Several in-rotation Projects with one normalized code. The registry prevents it; a hand-edited
    or damaged file can show it. Take all but one out of rotation, or change their codes."""
    unclassified: tuple[dict[str, JsonValue], ...] = ()
    """Imported Projects nobody classified. They block their codes. Classify them."""
    unbound_pages: tuple[dict[str, JsonValue], ...] = ()
    """Notion Project pages no Project is bound to. Register or bind them explicitly."""
    unavailable_bindings: tuple[dict[str, JsonValue], ...] = ()
    """A Project's page is deleted or out of reach. Restore it or bind a replacement."""
    missing_pages: tuple[dict[str, JsonValue], ...] = ()
    """A Project's active page is not among the Notion pages and nothing flagged it. Reconcile the page."""
    projection_conflicts: tuple[dict[str, JsonValue], ...] = ()
    """Someone edited a registry-owned value in Notion. Reconciliation put it back; decide if it was right."""

    @property
    def empty(self) -> bool:
        return not any(getattr(self, kind) for kind in KINDS)

    def to_json(self) -> dict[str, JsonValue]:
        return {kind: list(getattr(self, kind)) for kind in KINDS}


def _row(**values: JsonValue) -> dict[str, JsonValue]:
    return dict(values)


def audit_projects(registry: ProjectRegistry, pages: Iterable[str] | None) -> ProjectFindings:
    """Findings for the registry, and for the Notion pages when they were read.

    `pages` is every Projects page ID Notion returned, or `None` when Notion
    could not be read: the findings that need pages are then left out and the
    caller must report the check as incomplete. Raises `RegistryUnavailable`
    when the registry cannot be read.
    """
    projects = registry.list_projects()
    in_rotation: dict[str, list[str]] = {}
    for project in projects:
        if project.rotation is Rotation.IN and project.code is not None:
            in_rotation.setdefault(normalize_code(project.code)[1] or "", []).append(project.id)
    collisions = tuple(
        _row(code=next(p.code for p in projects if p.id == ids[0]), t6_ids=list(ids))
        for ids in in_rotation.values()
        if len(ids) > 1
    )
    unclassified = tuple(
        _row(t6_id=p.id, code=p.code, page_ids=[b.page_id for b in p.bindings])
        for p in projects
        if p.rotation is Rotation.UNCLASSIFIED
    )
    conflicts = tuple(
        _row(t6_id=item.project_id, page_id=item.page_id, detail=dict(item.detail))
        for item in registry.review_items()
        if item.kind == "projection_conflict"
    )
    if pages is None:
        return ProjectFindings(
            active_collisions=collisions, unclassified=unclassified, projection_conflicts=conflicts
        )
    shown = {normalize_page_id(page) for page in pages}
    bound = {b.page_id for p in projects for b in p.bindings}
    unbound = tuple(_row(page_id=page) for page in sorted(shown - bound))
    unavailable: list[dict[str, JsonValue]] = []
    missing: list[dict[str, JsonValue]] = []
    for project in projects:
        for binding in project.bindings:
            if not binding.active:
                continue  # a replaced page may be gone; the Project has moved on
            finding = _row(t6_id=project.id, page_id=binding.page_id)
            if not binding.available:
                unavailable.append(finding)
            elif binding.page_id not in shown:
                missing.append(finding)
    return ProjectFindings(
        collisions, unclassified, unbound, tuple(unavailable), tuple(missing), conflicts
    )
