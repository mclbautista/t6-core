"""Typed requests on the `frameio` queue, and the caller's side of asking one.

A caller holds no Frame.io credentials. It submits a request to the `frameio`
queue and waits for the Frame.io worker's result:

    result = ask(QueueClient(transport, secret), ShareLinks(frameio_project_id="..."))
    if isinstance(result, Ok): ...

`ask` never raises: a refused request, an unreachable queue and a timeout all
come back as `Failed` with a reason.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields
from datetime import datetime
import time
from typing import ClassVar, TypeAlias
import uuid

from t6.domain.events import JsonValue
from t6.server.queue import FRAMEIO_QUEUE
from t6.worker.client import QueueClient

DEFAULT_TIMEOUT_SECONDS = 30.0


class _Request:
    """Every text field must be non-blank, and every time must carry a time zone."""

    __slots__ = ()
    kind: ClassVar[str]

    def __post_init__(self) -> None:
        for spec in fields(self):  # type: ignore[arg-type]
            value = getattr(self, spec.name)
            if isinstance(value, datetime):
                if value.tzinfo is None:
                    raise ValueError(f"{spec.name} must carry a time zone")
            elif not isinstance(value, str) or not value.strip():
                raise ValueError(f"{spec.name} must be non-blank text")


@dataclass(frozen=True, slots=True)
class FrameioProjects(_Request):
    """Every Frame.io project in the T6 account."""

    kind: ClassVar[str] = "frameio.projects"


@dataclass(frozen=True, slots=True)
class ShareLinks(_Request):
    """The share links of one Frame.io project."""

    kind: ClassVar[str] = "frameio.share_links"
    frameio_project_id: str


@dataclass(frozen=True, slots=True)
class Comments(_Request):
    """The comments on one Frame.io file."""

    kind: ClassVar[str] = "frameio.comments"
    file_id: str


@dataclass(frozen=True, slots=True)
class Versions(_Request):
    """The versions of one Frame.io file."""

    kind: ClassVar[str] = "frameio.versions"
    file_id: str


@dataclass(frozen=True, slots=True)
class Approvals(_Request):
    """The approvals on one Frame.io file."""

    kind: ClassVar[str] = "frameio.approvals"
    file_id: str


@dataclass(frozen=True, slots=True)
class BriefingFeed(_Request):
    """Share links that are live and had activity after `since`, grouped per show."""

    kind: ClassVar[str] = "frameio.briefing_feed"
    since: datetime


@dataclass(frozen=True, slots=True)
class CreateProjectFolders(_Request):
    """Project Start's Frame.io folder step for one T6 Project.

    It can be delivered twice, and a caller that timed out may ask again, so
    whatever runs it must create nothing the second time.
    """

    kind: ClassVar[str] = "frameio.create_project_folders"
    project_id: str
    """The T6 Project ID (`t6p_...`)."""
    name: str
    """What the Frame.io project is called."""


FrameioRequest: TypeAlias = (
    FrameioProjects | ShareLinks | Comments | Versions | Approvals | BriefingFeed | CreateProjectFolders
)

_BY_KIND: dict[str, type[FrameioRequest]] = {
    cls.kind: cls
    for cls in (FrameioProjects, ShareLinks, Comments, Versions, Approvals, BriefingFeed, CreateProjectFolders)
}
REQUEST_KINDS = frozenset(_BY_KIND)


def to_payload(request: FrameioRequest) -> dict[str, JsonValue]:
    """The request's fields as a job payload."""
    payload: dict[str, JsonValue] = {}
    for spec in fields(request):
        value = getattr(request, spec.name)
        payload[spec.name] = value.isoformat() if isinstance(value, datetime) else value
    return payload


def from_job(kind: str, payload: Mapping[str, JsonValue]) -> FrameioRequest:
    """The typed request a job carries. Raises ValueError when it is not one."""
    cls = _BY_KIND.get(kind)
    if cls is None:
        raise ValueError(f"unknown request kind {kind!r}")
    values: dict[str, object] = {}
    for spec in fields(cls):
        value = payload.get(spec.name)
        if spec.type == "datetime":
            if not isinstance(value, str):
                raise ValueError(f"{spec.name} must be an ISO 8601 time")
            values[spec.name] = datetime.fromisoformat(value)
        else:
            values[spec.name] = value
    return cls(**values)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class Ok:
    """The Frame.io worker ran the request. `value` is what it answered."""

    value: JsonValue


@dataclass(frozen=True, slots=True)
class Failed:
    """The request was not run, or failed. `reason` says why."""

    reason: str


FrameioResult: TypeAlias = Ok | Failed


def ask(
    client: QueueClient,
    request: FrameioRequest,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    poll_seconds: float = 0.5,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> FrameioResult:
    """Submit `request` to the `frameio` queue and wait up to `timeout` seconds for its result.

    Each call is a new job, so asking again after a failure asks Frame.io again.
    """
    try:
        job_id = client.submit(FRAMEIO_QUEUE, request.kind, uuid.uuid4().hex, to_payload(request))
        deadline = clock() + timeout
        while True:
            found = client.result(FRAMEIO_QUEUE, job_id)
            if found is not None:
                status, value = found
                return Ok(value) if status == "ok" else Failed(str(value))
            if clock() >= deadline:
                return Failed(f"no answer from the Frame.io worker within {timeout:g} seconds")
            sleep(poll_seconds)
    except Exception as error:  # noqa: BLE001 - a caller gets Failed, never an exception
        return Failed(f"queue unavailable: {type(error).__name__}: {error}")
