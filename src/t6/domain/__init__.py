"""T6 domain entities. Pure data: nothing here does I/O.

Entities stored as Notion rows are identified by their Notion page ID, except
a Project, which is identified by its T6 Project ID once registered. Two
instances with the same identity are the same entity, whatever their other
fields say, so a row read again compares equal to the row read earlier.
Relations to other rows are held as page IDs (for example
`Deliverable.project_id`). Links to objects in other tools are ExternalRefs.
Tool events and the jobs they trigger are in `t6.domain.events`, re-exported here.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum

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
    "Department",
    "Episode",
    "Event",
    "EventSource",
    "ExternalRef",
    "FrameioCommentCreated",
    "Job",
    "JobKind",
    "JobOrder",
    "JobOrderStatus",
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
    and external ID. The URL and the internal ID are carried, but neither is
    part of the identity: a URL can change, and the internal ID is the tool's
    own key for the object (for example the project server's project ID).
    """

    system: str
    kind: str
    external_id: str
    url: str | None = field(default=None, compare=False)
    internal_id: str | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        _require(self.system, "system")
        _require(self.kind, "kind")
        _require(self.external_id, "external_id")
        object.__setattr__(self, "internal_id", _blank_to_none(self.internal_id))


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


@dataclass(frozen=True, slots=True, kw_only=True, eq=False)
class Project(NotionEntity):
    """A client show. Identity: T6 Project ID. Human key: Project Code.

    `t6_id` is the T6 Project ID from the Project registry (`t6.registry`).
    Two Projects with a T6 ID are the same Project when the IDs match, whatever
    their page IDs say: a replacement Notion page keeps the Project. A Project
    not yet registered has no T6 ID and is identified by its page ID. `key` is
    that identity in either form, and equality is on `key`. A Project read
    before registration is therefore a different value from the same Project
    read after it: compare through `t6.registry.ProjectRefs.same`.
    """

    t6_id: str | None = field(default=None, compare=False)
    rotation: str | None = field(default=None, compare=False)
    """`in`, `out` or `unclassified`, from the registry. `None` when it is not known."""
    project_code: str | None = field(default=None, compare=False)
    project_type: str | None = field(default=None, compare=False)
    eps: int | None = field(default=None, compare=False)
    status: str | None = field(default=None, compare=False)
    studio_id: str | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        NotionEntity.__post_init__(self)
        object.__setattr__(self, "t6_id", _blank_to_none(self.t6_id))
        object.__setattr__(self, "rotation", _blank_to_none(self.rotation))
        object.__setattr__(self, "project_code", _blank_to_none(self.project_code))
        if self.eps is not None and self.eps < 0:
            raise ValueError(f"eps must be zero or more, got {self.eps}")

    @property
    def key(self) -> str:
        """The T6 Project ID once the Project is registered, otherwise its page ID."""
        return self.t6_id or self.page_id

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Project):
            return NotImplemented
        return self.key == other.key

    def __hash__(self) -> int:
        return hash(self.key)

    @property
    def is_series(self) -> bool:
        return self.project_type in SERIES_PROJECT_TYPES

    @property
    def episodes(self) -> tuple[Episode, ...]:
        """Episodes derived from `Eps`. Empty for non-series Project Types."""
        if not self.is_series:
            return ()
        return tuple(
            Episode(project_id=self.key, number=n) for n in range(1, (self.eps or 0) + 1)
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class Deliverable(NotionEntity):
    """Something a Project owes its client."""

    project_id: str | None = field(default=None, compare=False)
    due: date | None = field(default=None, compare=False)
    status: str | None = field(default=None, compare=False)


class JobOrderStatus(StrEnum):
    """The Job Order statuses T6 knows. Each value is the status's label in Notion.

    `t6.notion.named` keys them by Notion option ID, so a status renamed in
    Notion is still found. A status that is not listed here reads as its plain
    name, a `str`.
    """

    NEEDS_INTAKE_REVIEW = "Needs Intake Review"
    PENDING = "Pending"
    IN_PROGRESS = "In progress"
    NEEDS_COMPLETION_REVIEW = "Needs Completion Review"
    DONE = "Done"
    CANCELLED = "Cancelled"


class Department(StrEnum):
    """The departments T6 knows, by their Notion option label. `MISC` is not a department.

    Notion's own options are the truth (a department added there reads as its plain
    name, a `str`). Display order and aliases are not part of this class.
    """

    TOC = "TOC"
    AE = "AE"
    OE = "OE"
    COOR = "COOR"
    COLOR = "COLOR"
    VFX = "VFX"
    SOUND = "SOUND"
    ADMIN = "ADMIN"


@dataclass(frozen=True, slots=True, kw_only=True)
class JobOrder(NotionEntity):
    """A unit of work on a Project, raised for a department.

    Carries every Job Orders field the apps use, named in studio terms; the
    mapping to Notion is in `t6.notion.job_orders`. `name` is the JO's name.
    Relations are page IDs. `health` is computed by Notion and is read only.
    `source_text` is the page body, kept as blocks in Notion. `status` and
    `department` are a `JobOrderStatus` or `Department` when the value is a
    known one, otherwise the plain option name.
    """

    number: str | None = field(default=None, compare=False)
    department: str | None = field(default=None, compare=False)
    date: dt.date | dt.datetime | None = field(default=None, compare=False)
    deadline: dt.date | dt.datetime | None = field(default=None, compare=False)
    title: str | None = field(default=None, compare=False)
    subtitle: str | None = field(default=None, compare=False)
    task: str | None = field(default=None, compare=False)
    purpose: str | None = field(default=None, compare=False)
    source: str | None = field(default=None, compare=False)
    platform: str | None = field(default=None, compare=False)
    link_settings: str | None = field(default=None, compare=False)
    watermark: str | None = field(default=None, compare=False)
    mode_of_transfer: str | None = field(default=None, compare=False)
    blocker: str | None = field(default=None, compare=False)
    pending: str | None = field(default=None, compare=False)
    notes: str | None = field(default=None, compare=False)
    watchdog_notes: str | None = field(default=None, compare=False)
    telegram_key: str | None = field(default=None, compare=False)
    status: str | None = field(default=None, compare=False)
    health: str | None = field(default=None, compare=False)
    project_id: str | None = field(default=None, compare=False)
    assigned_ids: tuple[str, ...] = field(default=(), compare=False)
    cc_ids: tuple[str, ...] = field(default=(), compare=False)
    coordinator_ids: tuple[str, ...] = field(default=(), compare=False)
    deliverable_ids: tuple[str, ...] = field(default=(), compare=False)
    source_text: str | None = field(default=None, compare=False)


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
