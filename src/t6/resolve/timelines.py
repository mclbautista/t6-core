"""Resolve timelines as a reader shows them: name, Resolve unique id and the first-frame marker stamp.

The stamp is the `t6t_` ID written by hand into the custom data of a marker at the timeline's first
frame. Reading it needs Resolve running or the bay scanner, so the reader is a seam. Nothing here writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True, kw_only=True)
class ResolveTimeline:
    """One timeline in one Resolve project. `uuid` changes on duplicate and import; `stamp` is None when unstamped."""

    uuid: str
    name: str
    stamp: str | None


class TimelineReader(Protocol):
    def timelines(self, resolve_project: str) -> tuple[ResolveTimeline, ...] | None:
        """The timelines of the Resolve project `<database>/<folder>/<project>`, or None when it cannot be read."""
        ...
