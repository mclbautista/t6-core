"""Resolve a Frame.io project to its active Project Chat links."""
from __future__ import annotations

from typing import Any

from t6.domain import ExternalRef


class ReviewTarget:
    def __init__(self, links: Any, registry: Any) -> None:
        self._links = links
        self._registry = registry

    def __call__(self, frameio_project_id: str) -> tuple[int, int | None] | None:
        row = self._links.find(ExternalRef(system="frameio", kind="project", external_id=frameio_project_id))
        if row is None:
            return None
        for page in row.project_ids:
            candidates = self._registry.review_chat_links(page)
            if candidates:
                topic = candidates[0]["topic"]
                return candidates[0]["chat"], None if topic == 1 else topic
        return None
