"""Resolve a Frame.io project to its active Project Chat links."""
from __future__ import annotations

from typing import Any

from t6.media_links import MediaLinks


class ReviewTarget:
    """The review chat of the Project a Frame.io project is linked to (Media location kind frameio-project)."""

    def __init__(self, links: MediaLinks, registry: Any) -> None:
        self._links = links
        self._registry = registry

    def linked(self, frameio_project_id: str) -> bool:
        """True when a Project holds this Frame.io project (an active Media location of kind frameio-project)."""
        return any(link.ref == frameio_project_id for link in self._links.media_links("frameio-project"))

    def __call__(self, frameio_project_id: str) -> tuple[int, int | None] | None:
        for link in self._links.media_links("frameio-project"):
            if link.ref != frameio_project_id:
                continue
            for page in link.pages:
                candidates = self._registry.review_chat_links(page)
                if candidates:
                    topic = candidates[0]["topic"]
                    return candidates[0]["chat"], None if topic == 1 else topic
        return None
