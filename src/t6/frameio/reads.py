"""Runs one typed request against Frame.io V4: the Frame.io worker's `run`.

`FrameioReads(api)` answers projects, share links, comments, versions and
approvals as JSON lists of the objects in `t6.frameio.model`. With `links`
(the External Links rows in Notion) it also answers the briefing feed, and
gives every live share link an External Links row.

The T6 account is found from the token (`GET /v4/accounts`). A token that sees
more than one account needs `account_id`.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, Protocol
import urllib.parse

from t6.domain import ExternalRef
from t6.domain.events import JsonValue
from t6.frameio.api import FrameioApi, FrameioError
from t6.frameio.model import (
    Approval,
    BriefingLink,
    BriefingShow,
    Comment,
    FrameioFile,
    FrameioProject,
    ResolvedComment,
    ShareLink,
    Version,
    _time,
)
from t6.frameio.requests import (
    Approvals,
    BriefingFeed,
    Comments,
    FileInfo,
    FrameioProjects,
    FrameioRequest,
    ResolveComment,
    ShareLinks,
    Versions,
)

SYSTEM = "frameio"
PROJECT_KIND = "project"
SHARE_LINK_KIND = "share_link"
APPROVAL_FIELD = "Status"
"""The metadata field Frame.io V4 keeps a file's approval in."""


class Link(Protocol):
    """One External Links row (`t6.notion.external_links.ExternalLink`)."""

    @property
    def page_id(self) -> str: ...
    @property
    def ref(self) -> ExternalRef: ...
    @property
    def active(self) -> bool: ...
    @property
    def last_activity(self) -> datetime | None: ...
    @property
    def project_ids(self) -> tuple[str, ...]: ...


class Links(Protocol):
    """The External Links rows (`t6.notion.external_links.ExternalLinks`)."""

    def links_for_system(self, system: str, kind: str) -> tuple[Any, ...]: ...

    def ensure(self, ref: ExternalRef, *, project_id: str | None = None) -> Any: ...

    def touch(self, page_id: str, when: datetime) -> Any: ...


def _id(value: str) -> str:
    """An ID as one path segment, so a caller's text cannot name another route."""
    return urllib.parse.quote(value, safe="")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class FrameioReads:
    def __init__(
        self,
        api: FrameioApi,
        links: Links | None = None,
        account_id: str | None = None,
        now: Callable[[], datetime] = _now,
    ) -> None:
        self._api = api
        self._links = links
        self._account_id = account_id
        self._now = now

    def __call__(self, request: FrameioRequest) -> JsonValue:
        if isinstance(request, FrameioProjects):
            return [project.to_json() for project in self.projects()]
        if isinstance(request, ShareLinks):
            return [link.to_json() for link in self.share_links(request.frameio_project_id)]
        if isinstance(request, Comments):
            return [comment.to_json() for comment in self.comments(request.file_id)]
        if isinstance(request, FileInfo):
            return self.file_info(request.file_id).to_json()
        if isinstance(request, ResolveComment):
            return self.resolve_comment(request).to_json()
        if isinstance(request, Versions):
            return [version.to_json() for version in self.versions(request.file_id)]
        if isinstance(request, Approvals):
            return [approval.to_json() for approval in self.approvals(request.file_id)]
        if isinstance(request, BriefingFeed):
            return [show.to_json() for show in self.briefing_feed(request.since)]
        raise NotImplementedError(f"{request.kind} is not built yet (t6-core#23)")

    def _account(self) -> str:
        if self._account_id is None:
            accounts = [str(account["id"]) for account in self._api.pages("/v4/accounts")]
            if len(accounts) != 1:
                raise RuntimeError(
                    f"the Frame.io sign-in sees {len(accounts)} accounts; set T6_FRAMEIO_ACCOUNT_ID to the T6 account"
                )
            self._account_id = accounts[0]
        return f"/v4/accounts/{_id(self._account_id)}"

    def projects(self) -> tuple[FrameioProject, ...]:
        return tuple(FrameioProject.from_api(raw) for raw in self._api.pages(f"{self._account()}/projects"))

    def share_links(self, frameio_project_id: str) -> tuple[ShareLink, ...]:
        rows = self._api.pages(f"{self._account()}/projects/{_id(frameio_project_id)}/shares")
        return tuple(ShareLink.from_api(raw, frameio_project_id) for raw in rows)

    def comments(self, file_id: str) -> tuple[Comment, ...]:
        rows = self._api.pages(f"{self._account()}/files/{_id(file_id)}/comments", {"include": "owner"})
        return tuple(Comment.from_api(raw, file_id) for raw in rows)

    def file_info(self, file_id: str) -> FrameioFile:
        data = self._api.get(f"{self._account()}/files/{_id(file_id)}").get("data")
        if not isinstance(data, dict):
            raise ValueError(f"Frame.io gave no file {file_id}")
        return FrameioFile.from_api({"id": file_id, **data})

    def resolve_comment(self, request: ResolveComment) -> ResolvedComment:
        """One comment by ID, in the account the webhook named."""
        path = f"/v4/accounts/{_id(request.account_id)}/comments/{_id(request.comment_id)}"
        data = self._api.get(path, {"include": "owner"}).get("data")
        if not isinstance(data, dict):
            raise ValueError(f"Frame.io gave no comment {request.comment_id}")
        return ResolvedComment.from_api(data, request.frameio_project_id)

    def versions(self, file_id: str) -> tuple[Version, ...]:
        """Every file in the version stack `file_id` is in, or the file alone when it is in none."""
        data = self._api.get(f"{self._account()}/files/{_id(file_id)}").get("data")
        file = data if isinstance(data, dict) else {}
        parent = file.get("parent_id")
        if isinstance(parent, str) and parent:
            try:
                stack = tuple(self._api.pages(f"{self._account()}/version_stacks/{_id(parent)}/children"))
            except FrameioError as error:
                if error.status != 404:  # 404: the parent is a folder, not a version stack
                    raise
            else:
                return tuple(Version.from_api(raw) for raw in stack)
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

        Every live share link gets an External Links row if it has none
        (related to the show when the Frame.io project has a row of its own).
        A row's Last Activity is moved forward to the link's latest activity.
        Active is never written.
        """
        if self._links is None:
            raise RuntimeError(
                "the briefing feed needs T6_NOTION_TOKEN and T6_NOTION_EXTERNAL_LINKS_DATA_SOURCE on the Frame.io worker"
            )
        now = self._now()
        project_rows = {row.ref.external_id: row for row in self._links.links_for_system(SYSTEM, PROJECT_KIND)}
        share_rows = {row.ref.external_id: row for row in self._links.links_for_system(SYSTEM, SHARE_LINK_KIND)}
        shows = []
        for project in self.projects():
            project_row = project_rows.get(project.id)
            pages: tuple[str, ...] = project_row.project_ids if project_row is not None else ()
            found = []
            for share in self.share_links(project.id):
                if not share.live(now):
                    continue
                row: Link | None = share_rows.get(share.id)
                if row is None:
                    ref = ExternalRef(system=SYSTEM, kind=SHARE_LINK_KIND, external_id=share.id, url=share.url)
                    row = self._links.ensure(ref, project_id=pages[0] if pages else None)
                latest = self._last_activity(share)
                if latest is None or latest <= since:
                    continue
                if row.last_activity is None or row.last_activity < latest:
                    self._links.touch(row.page_id, latest)
                found.append(
                    BriefingLink(share_id=share.id, name=share.name, url=share.url, last_activity=latest, active=row.active)
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
