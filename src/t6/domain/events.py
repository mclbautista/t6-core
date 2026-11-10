"""Tool events and the jobs they trigger. Pure data: nothing here does I/O.

An Event is something that happened in a tool (a Frame.io comment, a Notion
Project Status change). It can reach T6 from a webhook or from poll-and-diff.
The same tool event has the same `dedupe_key` whichever way it arrived, so the
queue stores it once. How it arrived (`source`) and when (`received_at`) are
carried but are not part of its identity.

A Job is work for a worker. It carries its kind, a JSON-ready payload and a
dedupe key. Jobs made from an event take their key from the event and the kind,
so one event never queues the same kind of job twice.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import TypeAlias

__all__ = [
    "Event",
    "EventSource",
    "FrameioCommentCreated",
    "Job",
    "JobKind",
    "JsonValue",
    "ProjectStatusChanged",
]

JsonValue: TypeAlias = "str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]"
"""A value that survives a round trip through JSON."""


def _require(value: str, name: str) -> None:
    if not value.strip():
        raise ValueError(f"{name} must not be blank")


class EventSource(str, Enum):
    """How an event reached T6. Never part of the dedupe key."""

    WEBHOOK = "webhook"
    POLL = "poll"


@dataclass(frozen=True, slots=True, kw_only=True)
class Event:
    """Shared shape of every tool event.

    Subclasses define `dedupe_key` from the tool's own identifiers only.
    Equality and hashing go through the dedupe key, so the webhook copy and the
    poll-and-diff copy of one event are equal.
    """

    source: EventSource = field(default=EventSource.WEBHOOK, compare=False)
    received_at: datetime | None = field(default=None, compare=False)

    @property
    def dedupe_key(self) -> str:
        raise NotImplementedError

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Event):
            return NotImplemented
        return self.dedupe_key == other.dedupe_key

    def __hash__(self) -> int:
        return hash(self.dedupe_key)


@dataclass(frozen=True, slots=True, kw_only=True, eq=False)
class FrameioCommentCreated(Event):
    """A comment was added in Frame.io.

    Identified by the Frame.io comment ID. `file_id` is optional because the
    Frame.io webhook names only the comment; poll-and-diff may know the file.
    """

    comment_id: str
    frameio_project_id: str
    file_id: str | None = None

    def __post_init__(self) -> None:
        _require(self.comment_id, "comment_id")
        _require(self.frameio_project_id, "frameio_project_id")

    @property
    def dedupe_key(self) -> str:
        return f"frameio:comment.created:{self.comment_id}"


@dataclass(frozen=True, slots=True, kw_only=True, eq=False)
class ProjectStatusChanged(Event):
    """A Notion Project's Status was set to `status`.

    Identified by the Project page ID and the new Status. The previous Status
    is not part of the key: a Notion webhook does not carry it.
    """

    page_id: str
    status: str

    def __post_init__(self) -> None:
        _require(self.page_id, "page_id")
        _require(self.status, "status")

    @property
    def dedupe_key(self) -> str:
        return f"notion:project.status:{self.page_id}:{self.status}"


class JobKind(str, Enum):
    """The kinds of work a worker runs. Values are stored in the queue."""

    FRAMEIO_REVIEW_DONE = "frameio.review_done"
    PROJECT_START_APPROVAL = "project_start.approval"
    PROJECT_START_FOLDERS = "project_start.folders"
    PROJECT_START_FRAMEIO_FOLDERS = "project_start.frameio_folders"
    PROJECT_START_INSTAGANTT = "project_start.instagantt"
    PROJECT_START_NOTION_PAGES = "project_start.notion_pages"
    NOTION_PAGE_CHANGED = "notion.page_changed"
    """A Notion page changed. A Notion webhook names the changed properties, not
    their new values, so a worker reads the page to find the current state."""
    REGISTRY_SYNC = "registry.sync"
    """A Project changed in the registry. A worker shows its current state in Notion."""
    TAP = "tap"
    """A person tapped a button on a card the gateway sent for t6-core."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Job:
    """Work for a worker, identified by its dedupe key.

    The payload is copied into a read-only mapping. Nested lists and dicts are
    not copied, so callers must not change them after building the Job.
    """

    kind: JobKind = field(compare=False)
    dedupe_key: str
    payload: Mapping[str, JsonValue] = field(default_factory=dict, compare=False)

    def __post_init__(self) -> None:
        _require(self.dedupe_key, "dedupe_key")
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))

    @classmethod
    def for_event(
        cls,
        kind: JobKind,
        event: Event,
        payload: Mapping[str, JsonValue] | None = None,
    ) -> Job:
        """Build the `kind` job for `event`, keyed by the kind and the event."""
        return cls(kind=kind, dedupe_key=f"{kind.value}/{event.dedupe_key}", payload=payload or {})
