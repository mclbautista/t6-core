"""In-memory fakes of t6 seams, for tests."""

from __future__ import annotations

from t6.media_links import MediaLink


class StaticMediaLinks:
    """An in-memory `MediaLinks` for tests. It records what it was asked."""

    def __init__(self, links: tuple[MediaLink, ...] = ()) -> None:
        self._links = list(links)
        self.calls: list[tuple[str, str | None]] = []

    def add(self, link: MediaLink) -> None:
        self._links.append(link)

    def all(self) -> tuple[MediaLink, ...]:
        return tuple(self._links)

    def media_links(self, kind: str, role: str | None = None) -> tuple[MediaLink, ...]:
        self.calls.append((kind, role))
        return tuple(sorted((l for l in self._links if l.kind == kind and role in (None, l.role)), key=lambda l: l.id))
