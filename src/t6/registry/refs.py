"""Resolve the references that name a Project, old and new, to one T6 Project ID.

Relations in Notion (Job Orders, Deliverables, External Links), Drive
Assignments and Project Start events still carry a Notion page ID. The registry
holds every page a Project was ever bound to, so a page ID, a replaced page's
ID and the T6 ID all resolve to the same Project. Nothing here matches by code
or name, and nothing here writes.

A reference the registry does not know is not an error: it is a Project that
has not been migrated yet, and is identified by the reference itself.
`RegistryUnavailable` is not caught: with no registry there is no answer.
"""

from __future__ import annotations

import dataclasses

from t6.domain import Project
from t6.registry.core import NOTION, PROJECT_ID_PREFIX, ProjectRegistry, RegisteredProject

__all__ = ["ProjectRefs"]


class ProjectRefs:
    def __init__(self, registry: ProjectRegistry) -> None:
        self._registry = registry

    def project(self, ref: str | None) -> RegisteredProject | None:
        """The registered Project that a T6 ID or any bound Notion page ID names."""
        if ref is None or not ref.strip():
            return None
        ref = ref.strip()
        if ref.startswith(PROJECT_ID_PREFIX):
            return self._registry.get(ref)
        return self._registry.get_by_page(ref)

    def identity(self, ref: str | None) -> str | None:
        """The T6 Project ID for `ref`, or None when the registry does not know it."""
        found = self.project(ref)
        return None if found is None else found.id

    def pages(self, ref: str) -> tuple[str, ...]:
        """Every Notion page ID of the Project, the active page first, then replaced ones.

        An unregistered page ID gives itself. An unknown T6 ID gives nothing.
        """
        found = self.project(ref)
        if found is None:
            return () if ref.strip().startswith(PROJECT_ID_PREFIX) else (ref,)
        ordered = sorted((b for b in found.bindings if b.system == NOTION), key=lambda b: not b.active)
        return tuple(b.page_id for b in ordered)

    def same(self, a: str | None, b: str | None) -> bool:
        """Whether two references name one Project, in either form.

        Two references the registry does not know are the same only when equal.
        """
        first, second = self.identity(a), self.identity(b)
        if first is None and second is None:
            return a is not None and a == b
        return first == second

    def identified(self, project: Project) -> Project:
        """`project` with its T6 ID filled in from its page. Unregistered pages are returned as they are."""
        found = self.identity(project.page_id)
        return project if found is None else dataclasses.replace(project, t6_id=found)
