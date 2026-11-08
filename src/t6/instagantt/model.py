"""Instagantt timeline data. Pure data: nothing here does I/O."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from t6.domain import ExternalRef


class InstaganttError(ValueError):
    """An export could not be read. The message names the line at fault."""


@dataclass(frozen=True, slots=True)
class Task:
    """One task bar on an Instagantt project. A milestone has `end == start`."""

    name: str
    start: date
    end: date


@dataclass(frozen=True, slots=True)
class Timeline:
    """One Instagantt project and its tasks, in export order.

    `external_id` is what External Links stores: the export's project ID when
    it has one, otherwise the project name.
    """

    project: str
    external_id: str
    tasks: tuple[Task, ...]

    @property
    def ref(self) -> ExternalRef:
        return ExternalRef(system="instagantt", kind="project", external_id=self.external_id)


@dataclass(frozen=True, slots=True)
class LinkedTimeline:
    """A timeline tied to the Notion Project its External Links row relates to."""

    timeline: Timeline
    project_id: str


@dataclass(frozen=True, slots=True)
class Unmapped:
    """An Instagantt project that could not be tied to exactly one Notion Project.

    Reported, never guessed: nobody should see a timeline under the wrong show.
    """

    timeline: Timeline
    reason: str


@dataclass(frozen=True, slots=True)
class LinkReport:
    linked: tuple[LinkedTimeline, ...]
    unmapped: tuple[Unmapped, ...]
