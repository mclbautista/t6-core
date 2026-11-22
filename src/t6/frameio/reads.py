"""Runs one typed request against Frame.io V4: the Frame.io worker's `run`.

`FrameioReads(api)` answers projects, share links, comments, versions and
approvals as JSON lists of the objects in `t6.frameio.model`. The one write is
`CreateProjectFolders` (Project Start, #275): a project and its folders, never a share. With `links`
(the Media locations in t6, kind `frameio-project`) it also answers the briefing
feed. Nothing is written to Notion or to t6: the link lives only in t6.

The T6 account is found from the token (`GET /v4/accounts`). A token that sees
more than one account needs `account_id`. A new project goes in the account's one
workspace (`GET /v4/accounts/{id}/workspaces`); more than one needs `workspace_id`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
import time
from typing import Any
import urllib.parse

from t6.domain.events import JsonValue
from t6.frameio.api import FrameioApi, FrameioError
from t6.frameio.model import (
    Approval,
    BriefingLink,
    BriefingShow,
    Comment,
    FileLocation,
    FrameioFile,
    FrameioProject,
    GUEST_ID,
    ResolvedComment,
    ShareLink,
    Version,
    _number,
    _text,
    _time,
    clip_remaining_seconds,
    format_timecode,
)
from t6.frameio.share_page import PageFetch, guest_name
from t6.media_links import MediaLinks
from t6.frameio.requests import (
    Approvals,
    BriefingFeed,
    Comments,
    CreateProjectFolders,
    FileInfo,
    FileLocate,
    FrameioProjects,
    FrameioRequest,
    ResolveComment,
    ShareLinks,
    Versions,
)

PROJECT_KIND = "frameio-project"
"""The Media location kind that links a Frame.io project to a Project."""
APPROVAL_FIELD = "Status"
SHARE_CACHE_SECONDS = 3600.0
"""How long a share list or a share's asset list is kept in memory (`T6_FRAMEIO_SHARE_CACHE_SECONDS`; 0 is off)."""
SHARE_SCAN_LIMIT = 30
"""How many of a project's newest live shares have their assets read (`T6_FRAMEIO_SHARE_SCAN_LIMIT`)."""
_OLDEST = datetime.min.replace(tzinfo=timezone.utc)
_NOT_A_STACK = frozenset({404, 422})
"""Statuses that mean a file's parent is a folder, not a version stack. Frame.io answers 422 for a folder ID on the version stack route."""
DURATION_FIELD = "duration"
"""Frame.io's file metadata field for the clip length in seconds (compared case-folded)."""
FRAME_RATE_FIELD = "frame rate"
"""Frame.io's file metadata field for frames per second (compared case-folded)."""
"""The metadata field Frame.io V4 keeps a file's approval in."""


def _head_version_id(item: Any) -> str | None:
    """The id of the file at the top of a version stack, or None for anything else."""
    head = item.get("head_version") if isinstance(item, dict) else None
    return _text(head.get("id")) if isinstance(head, dict) else None


def _id(value: str) -> str:
    """An ID as one path segment, so a caller's text cannot name another route."""
    return urllib.parse.quote(value, safe="")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class FrameioReads:
    def __init__(
        self,
        api: FrameioApi,
        links: MediaLinks | None = None,
        account_id: str | None = None,
        now: Callable[[], datetime] = _now,
        *,
        share_cache_seconds: float = SHARE_CACHE_SECONDS,
        share_scan_limit: int = SHARE_SCAN_LIMIT,
        clock: Callable[[], float] = time.monotonic,
        workspace_id: str | None = None,
        page_fetch: PageFetch | None = None,
        share_name: Callable[[str, str, str], str | None] | None = None,
    ) -> None:
        self._page_fetch = page_fetch
        self._share_name = share_name
        self._workspace_id = workspace_id
        self._api = api
        self._links = links
        self._account_id = account_id
        self._now = now
        self._share_cache_seconds = share_cache_seconds
        self._share_scan_limit = share_scan_limit
        self._clock = clock
        self._cache: dict[tuple[str, str, str], tuple[float, Any]] = {}

    def __call__(self, request: FrameioRequest) -> JsonValue:
        if isinstance(request, FrameioProjects):
            return [project.to_json() for project in self.projects()]
        if isinstance(request, ShareLinks):
            return [link.to_json() for link in self.share_links(request.frameio_project_id)]
        if isinstance(request, Comments):
            return [comment.to_json() for comment in self.comments(request.file_id)]
        if isinstance(request, FileInfo):
            return self.file_info(request.file_id).to_json()
        if isinstance(request, FileLocate):
            location = self.file_location(request.file_id)
            answer: dict[str, JsonValue] = {"folder_id": location.folder_id, "stack_id": location.stack_id}
            if location.file_ids is not None:
                answer["file_ids"] = list(location.file_ids)
            return answer
        if isinstance(request, ResolveComment):
            return self.resolve_comment(request).to_json()
        if isinstance(request, Versions):
            return [version.to_json() for version in self.versions(request.file_id)]
        if isinstance(request, Approvals):
            return [approval.to_json() for approval in self.approvals(request.file_id)]
        if isinstance(request, BriefingFeed):
            return [show.to_json() for show in self.briefing_feed(request.since)]
        if isinstance(request, CreateProjectFolders):
            return self.create_project_folders(request)
        raise NotImplementedError(f"{request.kind} is not built yet")

    def _account(self) -> str:
        if self._account_id is None:
            accounts = [str(account["id"]) for account in self._api.pages("/v4/accounts")]
            if len(accounts) != 1:
                raise RuntimeError(
                    f"the Frame.io sign-in sees {len(accounts)} accounts; set T6_FRAMEIO_ACCOUNT_ID to the T6 account"
                )
            self._account_id = accounts[0]
        return f"/v4/accounts/{_id(self._account_id)}"

    def _workspace(self) -> str:
        if self._workspace_id is None:
            workspaces = [str(item["id"]) for item in self._api.pages(f"{self._account()}/workspaces")]
            if len(workspaces) != 1:
                raise RuntimeError(
                    f"the Frame.io account has {len(workspaces)} workspaces; set T6_FRAMEIO_WORKSPACE_ID to the T6 workspace"
                )
            self._workspace_id = workspaces[0]
        return self._workspace_id

    def create_project_folders(self, request: CreateProjectFolders) -> dict[str, JsonValue]:
        """The Frame.io project and a folder per name, made only when missing (see `CreateProjectFolders`).

        Every write is preceded by a read of what exists, so a retry after a crash, a timeout or a redelivered job
        finds the project and the folders and makes nothing. Raises ValueError when the project cannot be told
        (two have the name) and FrameioError when Frame.io refuses a read or a write; nothing is retried here.
        """
        created_project = False
        if request.known_project_id is not None:
            found = self._api.get(f"{self._account()}/projects/{_id(request.known_project_id)}").get("data")
            if not isinstance(found, dict):
                raise ValueError(f"Frame.io gave no project {request.known_project_id}")
            project = FrameioProject.from_api({"id": request.known_project_id, **found})
        else:
            same = [p for p in self.projects() if p.name == request.name and p.id not in request.taken]
            if len(same) > 1:
                raise ValueError(f"more than one Frame.io project is named {request.name!r}: {', '.join(p.id for p in same)}")
            if same:
                project = same[0]
            else:
                made = self._api.post(f"{self._account()}/workspaces/{_id(self._workspace())}/projects", {"name": request.name}).get("data")
                if not isinstance(made, dict) or not made.get("id"):
                    raise ValueError("Frame.io gave no project after creating one")
                project = FrameioProject.from_api(made)
                created_project = True
        if project.root_folder_id is None:
            raise ValueError(f"Frame.io project {project.id} has no root folder")
        existing: dict[str, str] = {}
        if not created_project:
            for child in self._api.pages(f"{self._account()}/folders/{_id(project.root_folder_id)}/children"):
                name, found_id = _text(child.get("name")), _text(child.get("id"))
                if child.get("type", "folder") == "folder" and name and found_id:
                    if name in existing and name in request.folders:
                        raise ValueError(f"more than one folder named {name!r} in Frame.io project {project.id}")
                    existing.setdefault(name, found_id)
        folders: dict[str, JsonValue] = {}
        created: list[JsonValue] = []
        for name in request.folders:
            if name in existing:
                folders[name] = existing[name]
                continue
            made = self._api.post(f"{self._account()}/folders/{_id(project.root_folder_id)}/folders", {"name": name}).get("data")
            if not isinstance(made, dict) or not made.get("id"):
                raise ValueError(f"Frame.io gave no folder after creating {name!r}")
            folders[name] = str(made["id"])
            created.append(name)
        return {"frameio_project_id": project.id, "folders": folders, "created_project": created_project, "created_folders": created}

    def projects(self) -> tuple[FrameioProject, ...]:
        return tuple(FrameioProject.from_api(raw) for raw in self._api.pages(f"{self._account()}/projects"))

    def share_links(self, frameio_project_id: str, *, with_files: bool = True) -> tuple[ShareLink, ...]:
        """The project's share links. With `with_files`, the newest live ones also carry what they hold.

        Frame.io cannot go from a file to its shares, so the assets of each live
        share are read, newest share first, for at most `share_scan_limit`
        shares. A live share past that limit has `files_known=False`. Share
        lists and asset lists are cached (`share_cache_seconds`); a read that
        fails is never cached.
        """
        shares: tuple[ShareLink, ...] = self._cached(
            "shares",
            frameio_project_id,
            lambda: tuple(
                ShareLink.from_api(raw, frameio_project_id)
                for raw in self._api.pages(f"{self._account()}/projects/{_id(frameio_project_id)}/shares")
            ),
        )
        if not with_files:
            return shares
        now = self._now()
        newest = sorted((s for s in shares if s.live(now)), key=lambda s: s.created_at or _OLDEST, reverse=True)
        scanned = {s.id for s in newest[: self._share_scan_limit]}
        return tuple(
            replace(s, file_ids=self._share_file_ids(s.id))
            if s.id in scanned
            else replace(s, files_known=False)
            if s.live(now)
            else s
            for s in shares
        )

    def _share_file_ids(self, share_id: str) -> tuple[str, ...]:
        """What one share holds: each asset's id, and a version stack's head version too. A share that is gone holds nothing."""

        def read() -> tuple[str, ...]:
            ids: list[str] = []
            for item in self._api.pages(f"{self._account()}/shares/{_id(share_id)}/assets"):
                ids.extend(found for found in (_text(item.get("id")), _head_version_id(item)) if found)
            return tuple(ids)

        try:
            ids: tuple[str, ...] = self._cached("assets", share_id, read)
            return ids
        except FrameioError as error:
            if error.status == 404:
                return ()
            raise

    def _cached(self, kind: str, key: str, load: Callable[[], Any]) -> Any:
        """`load()`, remembered for `share_cache_seconds`. Nothing is remembered when `load` raises."""
        if self._share_cache_seconds <= 0:
            return load()
        slot, now = (self._account(), kind, key), self._clock()
        hit = self._cache.get(slot)
        if hit is not None and hit[0] > now:
            return hit[1]
        value = load()
        if len(self._cache) >= 2000:
            self._cache = {k: v for k, v in self._cache.items() if v[0] > now}
        self._cache[slot] = (now + self._share_cache_seconds, value)
        return value

    def comments(self, file_id: str) -> tuple[Comment, ...]:
        rows = self._api.pages(f"{self._account()}/files/{_id(file_id)}/comments", {"include": "owner"})
        return tuple(Comment.from_api(raw, file_id) for raw in rows)

    def file_info(self, file_id: str) -> FrameioFile:
        data = self._api.get(f"{self._account()}/files/{_id(file_id)}").get("data")
        if not isinstance(data, dict):
            raise ValueError(f"Frame.io gave no file {file_id}")
        return FrameioFile.from_api({"id": file_id, **data})

    def file_location(self, file_id: str) -> FileLocation:
        """The folder the file is in and its version stack. A stacked file's folder is its stack's own parent.

        An ID that names a version stack (Frame.io answers 422 "not a file" on the file route) answers the stack's own
        parent folder and the IDs of its files. Raises FrameioError (status 404) when Frame.io knows it as neither.
        """
        try:
            data = self._api.get(f"{self._account()}/files/{_id(file_id)}").get("data")
        except FrameioError as error:
            if error.status != 422:
                raise
            return self._stack_location(file_id)
        parent = _text(data.get("parent_id")) if isinstance(data, dict) else None
        if parent is None:
            raise ValueError(f"Frame.io gave no folder for file {file_id}")
        try:
            stack = self._api.get(f"{self._account()}/version_stacks/{_id(parent)}").get("data")
        except FrameioError as error:
            if error.status not in _NOT_A_STACK:
                raise
            return FileLocation(folder_id=parent)
        folder = _text(stack.get("parent_id")) if isinstance(stack, dict) else None
        if folder is None:
            raise ValueError(f"Frame.io gave no folder for version stack {parent}")
        return FileLocation(folder_id=folder, stack_id=parent)

    def _stack_location(self, stack_id: str) -> FileLocation:
        path = f"{self._account()}/version_stacks/{_id(stack_id)}"
        try:
            stack = self._api.get(path).get("data")
            children = tuple(self._api.pages(f"{path}/children"))
        except FrameioError as error:
            if error.status not in _NOT_A_STACK:
                raise
            raise FrameioError(404, path, "neither a file nor a version stack") from error
        folder = _text(stack.get("parent_id")) if isinstance(stack, dict) else None
        if folder is None:
            raise ValueError(f"Frame.io gave no folder for version stack {stack_id}")
        return FileLocation(folder_id=folder, stack_id=stack_id, file_ids=tuple(found for found in (_text(c.get("id")) for c in children) if found))

    def resolve_comment(self, request: ResolveComment) -> ResolvedComment:
        """One comment by ID, in the account the webhook named."""
        path = f"/v4/accounts/{_id(request.account_id)}/comments/{_id(request.comment_id)}"
        data = self._api.get(path, {"include": "owner"}).get("data")
        if not isinstance(data, dict):
            raise ValueError(f"Frame.io gave no comment {request.comment_id}")
        comment = ResolvedComment.from_api(data, request.frameio_project_id)
        duration, frame_rate = self._clip_metadata(request.account_id, comment.file_id)
        updates: dict[str, object] = {}
        if duration is not None:
            updates["total_timecode"] = format_timecode(duration)
        if comment.frame is not None and frame_rate is not None:
            updates["comment_timecode"] = format_timecode(comment.frame / frame_rate)
            if duration is not None:
                updates["remaining_seconds"] = clip_remaining_seconds(duration, frame_rate, comment.frame)
        if comment.author_id == GUEST_ID and (name := self._guest_name(request, comment)) is not None:
            updates["author_name"] = name
        return comment if not updates else replace(comment, **updates)  # type: ignore[arg-type]

    def _guest_name(self, request: ResolveComment, comment: ResolvedComment) -> str | None:
        """A guest's name from the public share page that holds the file; None on any failure, so the comment keeps `GUEST_NAME`."""
        if self._page_fetch is None and self._share_name is None:
            return None
        try:
            now = self._now()
            for share in self.share_links(request.frameio_project_id):
                if share.url is None or not share.live(now) or not share.file_ids or comment.file_id not in share.file_ids:
                    continue
                page = self._page_fetch(share.url) if self._page_fetch is not None else None
                if page is not None and (name := guest_name(page, comment.id)) is not None:
                    return name
                # `share_name(share id, file id, comment id)`: the share viewer's own comment list (#601)
                if self._share_name is not None and (name := self._share_name(share.id, comment.file_id, comment.id)) is not None:
                    return name
        except Exception:  # noqa: BLE001 - never lose or delay the comment for a name
            return None
        return None

    def _clip_metadata(self, account_id: str, file_id: str) -> tuple[float | None, float | None]:
        """The file's `Duration` (seconds) and `Frame Rate` (fps), or None when missing or unreadable."""
        path = f"/v4/accounts/{_id(account_id)}/files/{_id(file_id)}/metadata"
        try:
            data = self._api.get(path).get("data")
        except FrameioError:
            return None, None
        found: dict[str, float] = {}
        for entry in (data.get("metadata") if isinstance(data, dict) else None) or ():
            name = entry.get("field_definition_name") if isinstance(entry, dict) else None
            number = _number(entry.get("value")) if isinstance(entry, dict) else None
            if isinstance(name, str) and number is not None:
                found.setdefault(name.strip().casefold(), number)
        return found.get(DURATION_FIELD), found.get(FRAME_RATE_FIELD)

    def _remaining_seconds(self, account_id: str, comment: ResolvedComment) -> float | None:
        """Clip seconds left after the comment, from the file's `Duration` and `Frame Rate`; None when unknown.

        The comment is never refused for this: a clip with no length, a missing
        field or a failed read leaves it None, and the server's fixed wait applies.
        """
        if comment.frame is None:
            return None
        duration, frame_rate = self._clip_metadata(account_id, comment.file_id)
        if duration is None or frame_rate is None:
            return None
        return clip_remaining_seconds(duration, frame_rate, comment.frame)

    def versions(self, file_id: str) -> tuple[Version, ...]:
        """Every file in the version stack `file_id` is in, or the file alone when it is in none."""
        data = self._api.get(f"{self._account()}/files/{_id(file_id)}").get("data")
        file = data if isinstance(data, dict) else {}
        parent = file.get("parent_id")
        if isinstance(parent, str) and parent:
            try:
                stack = tuple(self._api.pages(f"{self._account()}/version_stacks/{_id(parent)}/children"))
            except FrameioError as error:
                if error.status not in _NOT_A_STACK:
                    raise
            else:
                return tuple(Version.from_json({**Version.from_api(raw).to_json(), "stack_id": parent}) for raw in stack)
        return (Version.from_api({"id": file_id, **file}),)

    def approvals(self, file_id: str) -> tuple[Approval, ...]:
        """The file's approval field, or nothing when the file has none."""
        data = self._api.get(f"{self._account()}/files/{_id(file_id)}/metadata").get("data")
        found = []
        for entry in (data.get("metadata") if isinstance(data, dict) else None) or ():
            name = entry.get("field_definition_name") if isinstance(entry, dict) else None
            if isinstance(name, str) and name.strip().casefold() == APPROVAL_FIELD.casefold():
                found.append(Approval(file_id=file_id, field=name, values=_values(entry.get("value"))))
        return tuple(found)

    def briefing_feed(self, since: datetime) -> tuple[BriefingShow, ...]:
        """Per Frame.io project, the live share links with activity after `since`.

        A Frame.io project is tied to its Project by a Media location of kind
        `frameio-project`; `project_page_ids` are that Project's Notion pages
        (empty when the project is unlinked). Nothing is written: share links
        have no row and no Last Activity stamp, because the activity is read
        from Frame.io each time. A live share is reported `active`.
        """
        if self._links is None:
            raise RuntimeError(
                "the briefing feed needs T6_API_URL and T6_REGISTRY_READ_SECRET on the Frame.io worker"
            )
        now = self._now()
        pages_by_project: dict[str, tuple[str, ...]] = {}
        for link in self._links.media_links(PROJECT_KIND):
            pages_by_project.setdefault(link.ref, link.pages)
        shows = []
        for project in self.projects():
            pages = pages_by_project.get(project.id, ())
            found = []
            for share in self.share_links(project.id, with_files=False):
                if not share.live(now):
                    continue
                latest = self._last_activity(share)
                if latest is None or latest <= since:
                    continue
                found.append(
                    BriefingLink(share_id=share.id, name=share.name, url=share.url, last_activity=latest, active=True)
                )
            if found:
                shows.append(
                    BriefingShow(
                        frameio_project_id=project.id,
                        frameio_project_name=project.name,
                        project_page_ids=pages,
                        links=tuple(found),
                    )
                )
        return tuple(shows)

    def _last_activity(self, share: ShareLink) -> datetime | None:
        """The latest of the share's last view and its activity list."""
        moments = [share.last_viewed_at]
        for activity in self._api.pages(f"{self._account()}/shares/{_id(share.id)}/activities"):
            moments.append(_time(activity.get("inserted_at")))
        known = [moment for moment in moments if moment is not None]
        return max(known) if known else None


def _values(value: object) -> tuple[str, ...]:
    """A metadata value as text: a select's option names, or the plain value."""
    if isinstance(value, list):
        names = (item.get("display_name") if isinstance(item, dict) else item for item in value)
        return tuple(str(name) for name in names if name not in (None, ""))
    return () if value in (None, "") else (str(value),)
