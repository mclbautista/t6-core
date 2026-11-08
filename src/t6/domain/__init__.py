"""T6 domain entities. Pure data: nothing here does I/O.

Entities stored as Notion rows are identified by their Notion page ID. Two
instances with the same page ID are the same entity, whatever their other
fields say, so a row read again compares equal to the row read earlier.
Relations to other rows are held as page IDs (for example
`Deliverable.project_id`). Links to objects in other tools are ExternalRefs.
Tool events and the jobs they trigger are in `t6.domain.events`, re-exported here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from t6.domain.events import (
    Event,
    EventSource,
    FrameioCommentCreated,
    Job,
    JobKind,
    JsonValue,
    ProjectStatusChanged,
)

__all__ = [
    "SERIES_PROJECT_TYPES",
    "Camera",
    "Contact",
    "Deliverable",
    "Episode",
    "Event",
    "EventSource",
    "ExternalRef",
    "FrameioCommentCreated",
    "Job",
    "JobKind",
    "JobOrder",
    "JsonValue",
    "Machine",
    "NotionEntity",
    "Project",
    "ProjectStatusChanged",
    "Studio",
]

SERIES_PROJECT_TYPES: frozenset[str] = frozenset(
    {
        "Serial TV",
        "Serial TV - Color",
        "Streamer Originals Series",
        "Limited Series",
        "Limited Documentary Series",
    }
)
"""Project Types that have Episodes. Every other type, Film included, has none."""


def _require(value: str, name: str) -> None:
    if not value.strip():
        raise ValueError(f"{name} must not be blank")


def _blank_to_none(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class ExternalRef:
    """A pointer from a T6 entity to an object in another tool.

    Mirrors one External Links row. The object is identified by system, kind
    and external ID. The URL is carried, but it can change without changing
    which object it is.
    """

    system: str
    kind: str
    external_id: str
    url: str | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        _require(self.system, "system")
        _require(self.kind, "kind")
        _require(self.external_id, "external_id")


@dataclass(frozen=True, slots=True, kw_only=True)
class NotionEntity:
    """Shared shape of every entity stored as a Notion row."""

    page_id: str
    name: str = field(compare=False)
    external_refs: tuple[ExternalRef, ...] = field(default=(), compare=False)

    def __post_init__(self) -> None:
        _require(self.page_id, "page_id")


@dataclass(frozen=True, slots=True, kw_only=True)
class Episode:
    """One episode of a series Project, numbered from 1."""

    project_id: str
    number: int


@dataclass(frozen=True, slots=True, kw_only=True)
class Project(NotionEntity):
    """A client show. System key: Notion page ID. Human key: Project Code."""

    project_code: str | None = field(default=None, compare=False)
    project_type: str | None = field(default=None, compare=False)
    eps: int | None = field(default=None, compare=False)
    status: str | None = field(default=None, compare=False)
    studio_id: str | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        NotionEntity.__post_init__(self)
        object.__setattr__(self, "project_code", _blank_to_none(self.project_code))
        if self.eps is not None and self.eps < 0:
            raise ValueError(f"eps must be zero or more, got {self.eps}")

    @property
    def is_series(self) -> bool:
        return self.project_type in SERIES_PROJECT_TYPES

    @property
    def episodes(self) -> tuple[Episode, ...]:
        """Episodes derived from `Eps`. Empty for non-series Project Types."""
        if not self.is_series:
            return ()
        return tuple(
            Episode(project_id=self.page_id, number=n) for n in range(1, (self.eps or 0) + 1)
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class Deliverable(NotionEntity):
    """Something a Project owes its client."""

    project_id: str | None = field(default=None, compare=False)
    due: date | None = field(default=None, compare=False)
    status: str | None = field(default=None, compare=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class JobOrder(NotionEntity):
    """A unit of work on a Project, raised for a department."""

    project_id: str | None = field(default=None, compare=False)
    department: str | None = field(default=None, compare=False)
    telegram_key: str | None = field(default=None, compare=False)
    status: str | None = field(default=None, compare=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class Studio(NotionEntity):
    """A client company. `aliases` mirrors the Notion `Aliases` property."""

    aliases: tuple[str, ...] = field(default=(), compare=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class Contact(NotionEntity):
    """A person T6 works with."""

    studio_id: str | None = field(default=None, compare=False)
    email: str | None = field(default=None, compare=False)
    telegram_handle: str | None = field(default=None, compare=False)
    telegram_user_id: int | None = field(default=None, compare=False)
    aliases: tuple[str, ...] = field(default=(), compare=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class Machine(NotionEntity):
    """A computer in the fleet. Edit and color suites are called Bays."""

    hostname: str | None = field(default=None, compare=False)
    serial_number: str | None = field(default=None, compare=False)
    lan_ip: str | None = field(default=None, compare=False)
    os_version: str | None = field(default=None, compare=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class Camera(NotionEntity):
    """A camera model, used by the OCF Drive Planner."""

    tags: tuple[str, ...] = field(default=(), compare=False)
