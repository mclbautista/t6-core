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
from dataclasses import MISSING, dataclass, fields
from datetime import datetime
import time
from typing import ClassVar, Protocol, TypeAlias
import uuid

from t6.domain.events import JsonValue
from t6.server.queue import FRAMEIO_QUEUE

DEFAULT_TIMEOUT_SECONDS = 30.0


class RequestQueue(Protocol):
    """What `ask` needs of a queue: the methods of `t6.worker.QueueClient`."""

    def submit(self, queue: str, kind: str, dedupe_key: str, payload: Mapping[str, JsonValue] | None = None) -> int: ...

    def result(self, queue: str, job_id: int) -> tuple[str, JsonValue] | None: ...


class _Request:
    """Every text field must be non-blank, every time must carry a time zone, and every text in a list must be non-blank.

    A field whose default is None may be left out.
    """

    __slots__ = ()
    kind: ClassVar[str]

    def __post_init__(self) -> None:
        for spec in fields(self):  # type: ignore[arg-type]
            value = getattr(self, spec.name)
            if value is None and spec.default is None:
                continue
            if isinstance(value, datetime):
                if value.tzinfo is None:
                    raise ValueError(f"{spec.name} must carry a time zone")
            elif isinstance(value, tuple):
                if not all(isinstance(item, str) and item.strip() for item in value):
                    raise ValueError(f"{spec.name} must hold non-blank text only")
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
class FileInfo(_Request):
    """The title and internal link of one Frame.io file."""

    kind: ClassVar[str] = "frameio.file"
    file_id: str


@dataclass(frozen=True, slots=True)
class FileLocate(_Request):
    """Where one Frame.io file is: its folder, and its version stack when it is in one."""

    kind: ClassVar[str] = "frameio.file_location"
    file_id: str


@dataclass(frozen=True, slots=True)
class ResolveComment(_Request):
    """Read one comment that a webhook named, to learn its file and its author.

    The Frame.io worker also starts the (file, author) review timer for it, so
    a caller outside the server does not need this request.
    """

    kind: ClassVar[str] = "frameio.resolve_comment"
    comment_id: str
    account_id: str
    frameio_project_id: str


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
    """Project Start's Frame.io step for one T6 Project: its Frame.io project, and one folder per Deliverable code.

    This is a write, and the one place the Frame.io worker creates anything. It can be delivered twice, and a
    caller that timed out may ask again, so it creates nothing that exists:

    - the project is `known_project_id` when the caller has one stored (the id is never searched for by name);
    - otherwise the project in the T6 account named exactly `name`, unless its id is in `taken` (a Frame.io
      project that belongs to another T6 Project);
    - otherwise a new project. Two projects with the name, or an unknown `known_project_id`, fail the request:
      the worker never guesses and never makes a second one.

    A folder is found by its exact name under the project's root folder, or made there. No share is made.
    The answer is `{"frameio_project_id", "folders": {code: folder id}, "created_project", "created_folders"}`.
    """

    kind: ClassVar[str] = "frameio.create_project_folders"
    project_id: str
    """The T6 Project ID (`t6p_...`). Frame.io never sees it."""
    name: str
    """What the Frame.io project is called: the Project's name."""
    folders: tuple[str, ...]
    """Folder names, one per Deliverable code. Each is unique."""
    known_project_id: str | None = None
    """The Frame.io project T6 already records for this Project."""
    taken: tuple[str, ...] = ()
    """Frame.io projects T6 records for other Projects."""

    def __post_init__(self) -> None:
        _Request.__post_init__(self)
        if len(set(self.folders)) != len(self.folders):
            raise ValueError("folders must be unique")


FrameioRequest: TypeAlias = (
    FrameioProjects
    | ShareLinks
    | Comments
    | FileInfo
    | FileLocate
    | ResolveComment
    | Versions
    | Approvals
    | BriefingFeed
    | CreateProjectFolders
)

_BY_KIND: dict[str, type[FrameioRequest]] = {
    cls.kind: cls
    for cls in (
        FrameioProjects,
        ShareLinks,
        Comments,
        FileInfo,
        FileLocate,
        ResolveComment,
        Versions,
        Approvals,
        BriefingFeed,
        CreateProjectFolders,
    )
}
REQUEST_KINDS = frozenset(_BY_KIND)


def to_payload(request: FrameioRequest) -> dict[str, JsonValue]:
    """The request's fields as a job payload."""
    payload: dict[str, JsonValue] = {}
    for spec in fields(request):
        value = getattr(request, spec.name)
        payload[spec.name] = value.isoformat() if isinstance(value, datetime) else list(value) if isinstance(value, tuple) else value
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
        elif spec.type == "tuple[str, ...]":
            if value is None and spec.default is not MISSING:
                continue
            if not isinstance(value, list):
                raise ValueError(f"{spec.name} must be a list")
            values[spec.name] = tuple(value)
        elif value is None and spec.default is not MISSING:
            continue
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


@dataclass(frozen=True, slots=True)
class FileNotFound(Failed):
    """Frame.io does not know the file (it was deleted, or the ID is wrong). Asking again will not help."""


FrameioResult: TypeAlias = Ok | Failed


def ask(
    client: RequestQueue,
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
