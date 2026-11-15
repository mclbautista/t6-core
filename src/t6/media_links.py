"""Read Media locations by kind and role, for readers that do not own the t6 database.

Frame.io reads, Resolve `map_projects`, Instagantt linking and the review
message each ask a `MediaLinks` for the active Media locations of one kind (and
optionally one role). Where it comes from is a seam: the server reads its own
database (`t6.media.active_links`), a worker asks the server over the registry
route (`t6.worker.registry.RegistryClient.media_links`), and tests use
`t6.testing.StaticMediaLinks`. Nothing here writes, and nothing here reads Notion.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True, kw_only=True)
class MediaLink:
    """One active Media location, as a reader needs it.

    `project_id` is the T6 ID of the Project that owns it (directly or through
    a Deliverable). `pages` are that Project's Notion page IDs, the active page
    first; they are only for callers that still address a Project by page.
    """

    id: str
    owner: str
    project_id: str
    kind: str
    ref: str
    role: str
    pages: tuple[str, ...] = ()


class MediaLinks(Protocol):
    def media_links(self, kind: str, role: str | None = None) -> tuple[MediaLink, ...]:
        """Every active Media location of `kind`, of `role` too when it is given, in ID order."""
        ...
