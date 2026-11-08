"""Tie each Instagantt project to its Notion Project through External Links.

Reads External Links rows only (System `instagantt`, Kind `project`). It
reads no Notion date properties, and it writes nothing.
"""

from __future__ import annotations

from collections.abc import Iterable

from t6.instagantt.model import LinkedTimeline, LinkReport, Timeline, Unmapped
from t6.notion.external_links import ExternalLinks


def link_timelines(timelines: Iterable[Timeline], links: ExternalLinks) -> LinkReport:
    """Resolve every timeline. One row relating to exactly one Project links it.

    No row, a row with no Project, or a row with several Projects is reported
    as unmapped with the reason; nothing is guessed.
    """
    linked: list[LinkedTimeline] = []
    unmapped: list[Unmapped] = []
    for timeline in timelines:
        row = links.find(timeline.ref)
        if row is None:
            unmapped.append(Unmapped(timeline, "no External Links row"))
        elif not row.project_ids:
            unmapped.append(Unmapped(timeline, "External Links row has no Project"))
        elif len(row.project_ids) > 1:
            unmapped.append(Unmapped(timeline, "External Links row relates to several Projects"))
        else:
            linked.append(LinkedTimeline(timeline, row.project_ids[0]))
    return LinkReport(tuple(linked), tuple(unmapped))
