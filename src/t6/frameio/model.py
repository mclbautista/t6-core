"""What the Frame.io reads answer, as typed objects.

The Frame.io worker answers a request with JSON on the job. A caller turns it
back into these objects with `decode`:

    result = ask(client, ShareLinks(frameio_project_id="..."))
    if isinstance(result, Ok):
        links = decode(ShareLinks, result.value)   # tuple[ShareLink, ...]

Every object is built from Frame.io V4 fields only. Times are timezone-aware
(a time Frame.io gives without a zone is read as UTC).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from typing import Any, overload

from t6.domain.events import JsonValue
from t6.frameio.requests import Approvals, BriefingFeed, Comments, FrameioProjects, ShareLinks, Versions


def _time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    moment = datetime.fromisoformat(value)
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


class _Json:
    """JSON codec for a flat dataclass of text, flags, times and tuples of text."""

    __slots__ = ()

    def to_json(self) -> dict[str, JsonValue]:
        out: dict[str, JsonValue] = {}
        for spec in fields(self):  # type: ignore[arg-type]
            value = getattr(self, spec.name)
            if isinstance(value, datetime):
                value = value.isoformat()
            elif isinstance(value, tuple):
                value = [item.to_json() if isinstance(item, _Json) else item for item in value]
            out[spec.name] = value
        return out


@dataclass(frozen=True, slots=True, kw_only=True)
class FrameioProject(_Json):
    id: str
    name: str
    workspace_id: str | None = None
    root_folder_id: str | None = None
    url: str | None = None
    updated_at: datetime | None = None

    @classmethod
    def from_api(cls, raw: Mapping[str, Any]) -> FrameioProject:
        return cls(
            id=str(raw["id"]),
            name=str(raw.get("name") or ""),
            workspace_id=_text(raw.get("workspace_id")),
            root_folder_id=_text(raw.get("root_folder_id")),
            url=_text(raw.get("view_url")),
            updated_at=_time(raw.get("updated_at")),
        )

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> FrameioProject:
        return cls(**{**raw, "updated_at": _time(raw.get("updated_at"))})


@dataclass(frozen=True, slots=True, kw_only=True)
class ShareLink(_Json):
    """One share of a Frame.io project. `url` is the link a client opens."""

    id: str
    frameio_project_id: str
    name: str
    url: str | None = None
    enabled: bool = False
    expires_at: datetime | None = None
    last_viewed_at: datetime | None = None
    created_at: datetime | None = None
    file_ids: tuple[str, ...] = ()
    """The files the share was made from (Frame.io's `asset_ids`). A share of a folder lists the folder."""

    def live(self, now: datetime) -> bool:
        """A client can open it now: it is turned on and has not expired."""
        return self.enabled and (self.expires_at is None or self.expires_at > now)

    @classmethod
    def from_api(cls, raw: Mapping[str, Any], frameio_project_id: str) -> ShareLink:
        return cls(
            id=str(raw["id"]),
            frameio_project_id=frameio_project_id,
            name=str(raw.get("name") or ""),
            url=_text(raw.get("short_url")),
            enabled=raw.get("enabled") is True,
            expires_at=_time(raw.get("expiration")),
            last_viewed_at=_time(raw.get("last_viewed_at")),
            created_at=_time(raw.get("inserted_at") or raw.get("created_at")),
            file_ids=tuple(str(item) for item in raw.get("asset_ids") or () if item),
        )

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> ShareLink:
        times = {name: _time(raw.get(name)) for name in ("expires_at", "last_viewed_at", "created_at")}
        return cls(**{**raw, **times, "file_ids": tuple(raw.get("file_ids") or ())})


@dataclass(frozen=True, slots=True, kw_only=True)
class Comment(_Json):
    id: str
    file_id: str
    text: str
    author: str | None = None
    created_at: datetime | None = None
    completed_at: datetime | None = None

    @classmethod
    def from_api(cls, raw: Mapping[str, Any], file_id: str) -> Comment:
        owner = raw.get("owner")
        return cls(
            id=str(raw["id"]),
            file_id=file_id,
            text=str(raw.get("text") or ""),
            author=_text(owner.get("name")) if isinstance(owner, Mapping) else None,
            created_at=_time(raw.get("created_at")),
            completed_at=_time(raw.get("completed_at")),
        )

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> Comment:
        times = {name: _time(raw.get(name)) for name in ("created_at", "completed_at")}
        return cls(**{**raw, **times})


@dataclass(frozen=True, slots=True, kw_only=True)
class FrameioFile(_Json):
    """One Frame.io file. `url` is the internal link a signed-in person opens."""

    id: str
    name: str
    url: str | None = None

    @classmethod
    def from_api(cls, raw: Mapping[str, Any]) -> FrameioFile:
        return cls(id=str(raw["id"]), name=str(raw.get("name") or ""), url=_text(raw.get("view_url")))

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> FrameioFile:
        return cls(**raw)


@dataclass(frozen=True, slots=True, kw_only=True)
class ResolvedComment(_Json):
    """One comment read by its own ID: which file it is on and who wrote it."""

    id: str
    file_id: str
    frameio_project_id: str
    author_id: str
    author_name: str | None
    created_at: datetime

    @classmethod
    def from_api(cls, raw: Mapping[str, Any], frameio_project_id: str) -> ResolvedComment:
        """Raises ValueError when the comment has no file, no author or no time."""
        owner = raw.get("owner")
        owner = owner if isinstance(owner, Mapping) else {}
        file_id = _text(raw.get("file_id")) or _text(raw.get("resource_id"))
        author_id = _text(owner.get("id"))
        created_at = _time(raw.get("created_at"))
        if file_id is None or author_id is None or created_at is None:
            raise ValueError(f"comment {raw.get('id')} has no file, author or time")
        return cls(
            id=str(raw["id"]),
            file_id=file_id,
            frameio_project_id=frameio_project_id,
            author_id=author_id,
            author_name=_text(owner.get("name")),
            created_at=created_at,
        )

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> ResolvedComment:
        created = _time(raw.get("created_at"))
        if created is None:
            raise ValueError("created_at is missing")
        return cls(**{**raw, "created_at": created})


@dataclass(frozen=True, slots=True, kw_only=True)
class Version(_Json):
    """One file in a version stack. A file outside a stack is its own only version."""

    file_id: str
    name: str
    url: str | None = None
    created_at: datetime | None = None

    @classmethod
    def from_api(cls, raw: Mapping[str, Any]) -> Version:
        return cls(
            file_id=str(raw["id"]),
            name=str(raw.get("name") or ""),
            url=_text(raw.get("view_url")),
            created_at=_time(raw.get("created_at")),
        )

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> Version:
        return cls(**{**raw, "created_at": _time(raw.get("created_at"))})


@dataclass(frozen=True, slots=True, kw_only=True)
class Approval(_Json):
    """The value of a file's approval field. Frame.io V4 keeps approval as a metadata field, `Status`."""

    file_id: str
    field: str
    values: tuple[str, ...] = ()

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> Approval:
        return cls(**{**raw, "values": tuple(raw.get("values") or ())})


@dataclass(frozen=True, slots=True, kw_only=True)
class BriefingLink(_Json):
    """A live share link with activity after the briefing's `since`."""

    share_id: str
    name: str
    url: str | None
    last_activity: datetime
    active: bool
    """The External Links row's Active box, which people set."""

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> BriefingLink:
        moment = _time(raw.get("last_activity"))
        if moment is None:
            raise ValueError("a briefing link needs last_activity")
        return cls(**{**raw, "last_activity": moment})


@dataclass(frozen=True, slots=True, kw_only=True)
class BriefingShow(_Json):
    """One show's links. `project_page_ids` are the Notion Projects pages its Frame.io project is linked to;
    empty when the Frame.io project has no External Links row yet."""

    frameio_project_id: str
    frameio_project_name: str
    project_page_ids: tuple[str, ...]
    links: tuple[BriefingLink, ...]

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> BriefingShow:
        return cls(
            frameio_project_id=raw["frameio_project_id"],
            frameio_project_name=raw["frameio_project_name"],
            project_page_ids=tuple(raw.get("project_page_ids") or ()),
            links=tuple(BriefingLink.from_json(link) for link in raw.get("links") or ()),
        )


_RESULTS: dict[type, Any] = {
    FrameioProjects: FrameioProject,
    ShareLinks: ShareLink,
    Comments: Comment,
    Versions: Version,
    Approvals: Approval,
    BriefingFeed: BriefingShow,
}


@overload
def decode(request: type[FrameioProjects], value: JsonValue) -> tuple[FrameioProject, ...]: ...
@overload
def decode(request: type[ShareLinks], value: JsonValue) -> tuple[ShareLink, ...]: ...
@overload
def decode(request: type[Comments], value: JsonValue) -> tuple[Comment, ...]: ...
@overload
def decode(request: type[Versions], value: JsonValue) -> tuple[Version, ...]: ...
@overload
def decode(request: type[Approvals], value: JsonValue) -> tuple[Approval, ...]: ...
@overload
def decode(request: type[BriefingFeed], value: JsonValue) -> tuple[BriefingShow, ...]: ...


def decode(request: type, value: JsonValue) -> tuple[Any, ...]:
    """The typed objects in an `Ok` value for this kind of request. Raises ValueError when it is not one."""
    cls = _RESULTS.get(request)
    if cls is None:
        raise ValueError(f"{request.__name__} has no typed result")
    if not isinstance(value, list):
        raise ValueError(f"the answer to {request.__name__} is not a list")
    try:
        return tuple(cls.from_json(item) for item in value)
    except (TypeError, KeyError, AttributeError) as error:
        raise ValueError(f"the answer to {request.__name__} is malformed: {error}") from error
