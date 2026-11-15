"""Tie each Instagantt project to its Project through Media locations.

Reads active Media locations of kind `timeline` only; an export's project ID
is the Timeline ID (`t6t_`) of the location. It reads no Notion date
properties, and it writes nothing.
"""

from __future__ import annotations

from collections.abc import Iterable

from t6.instagantt.model import LinkedTimeline, LinkReport, Timeline, Unmapped
from t6.media_links import MediaLinks

MEDIA_KIND = "timeline"


def link_timelines(timelines: Iterable[Timeline], links: MediaLinks) -> LinkReport:
    """Resolve every timeline. Media locations that name it in exactly one Project link it.

    No location, or locations in several Projects, is reported as unmapped with
    the reason; nothing is guessed.
    """
    projects: dict[str, set[str]] = {}
    for link in links.media_links(MEDIA_KIND):
        projects.setdefault(link.ref, set()).add(link.project_id)
    linked: list[LinkedTimeline] = []
    unmapped: list[Unmapped] = []
    for timeline in timelines:
        owners = projects.get(timeline.external_id, set())
        if not owners:
            unmapped.append(Unmapped(timeline, "no Media location"))
        elif len(owners) > 1:
            unmapped.append(Unmapped(timeline, "Media locations relate to several Projects"))
        else:
            linked.append(LinkedTimeline(timeline, next(iter(owners))))
    return LinkReport(tuple(linked), tuple(unmapped))
