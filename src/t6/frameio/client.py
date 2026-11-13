"""A token-free client for the Frame.io worker's read API (`t6.frameio.readapi`).

    reader = FrameioReader(HttpTransport(read_api_url, timeout=120), secret)
    shows = reader.briefing(since)
    if isinstance(shows, SignInNeeded): ...   # a person must sign in to Frame.io
    elif isinstance(shows, Failed): ...       # shows.reason says why
    else: ...                                 # tuple[BriefingShow, ...]

The caller holds the read secret and never a Frame.io token. Methods never
raise: a failure comes back as `Failed`, or `SignInNeeded` when the worker has
no usable Frame.io sign-in. The briefing feed can take minutes, so give
`HttpTransport` a long `timeout`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
import urllib.parse

from t6.domain.events import JsonValue
from t6.frameio.model import Approval, BriefingShow, Comment, FrameioProject, ShareLink, decode
from t6.frameio.requests import Approvals, BriefingFeed, Comments, Failed, FrameioProjects, ShareLinks
from t6.worker.client import Transport

SIGN_IN_REQUIRED = "sign_in_required"


@dataclass(frozen=True, slots=True)
class SignInNeeded(Failed):
    """The Frame.io worker is signed out. Reads fail until a person signs in."""

    reason: str = "Frame.io sign-in needed"


def _id(value: str) -> str:
    return urllib.parse.quote(value, safe="")


class FrameioReader:
    def __init__(self, transport: Transport, secret: str) -> None:
        self._transport = transport
        self._headers = {"Authorization": f"Bearer {secret}"}

    def signed_in(self) -> bool | Failed:
        """Whether the worker has a usable Frame.io sign-in."""
        value = self._get("/frameio/status")
        if isinstance(value, Failed):
            return value
        return bool(value.get("signed_in")) if isinstance(value, dict) else Failed("malformed status answer")

    def projects(self) -> tuple[FrameioProject, ...] | Failed:
        return cast('tuple[FrameioProject, ...] | Failed', self._decoded(FrameioProjects, self._get("/frameio/projects")))

    def share_links(self, frameio_project_id: str) -> tuple[ShareLink, ...] | Failed:
        return cast('tuple[ShareLink, ...] | Failed', self._decoded(ShareLinks, self._get(f"/frameio/projects/{_id(frameio_project_id)}/share-links")))

    def comments(self, file_id: str) -> tuple[Comment, ...] | Failed:
        return cast('tuple[Comment, ...] | Failed', self._decoded(Comments, self._get(f"/frameio/files/{_id(file_id)}/comments")))

    def approvals(self, file_id: str) -> tuple[Approval, ...] | Failed:
        return cast('tuple[Approval, ...] | Failed', self._decoded(Approvals, self._get(f"/frameio/files/{_id(file_id)}/approvals")))

    def briefing(self, since: datetime) -> tuple[BriefingShow, ...] | Failed:
        if since.tzinfo is None:
            return Failed("since must carry a time zone")
        return cast('tuple[BriefingShow, ...] | Failed', self._decoded(BriefingFeed, self._get("/frameio/briefing?since=" + urllib.parse.quote(since.isoformat(), safe=""))))

    @staticmethod
    def _decoded(request: Any, value: JsonValue | Failed) -> Any:
        if isinstance(value, Failed):
            return value
        try:
            return decode(request, value)
        except ValueError as error:
            return Failed(str(error))

    def _get(self, path: str) -> JsonValue | Failed:
        try:
            status, answer = self._transport.send("GET", path, self._headers, b"")
        except Exception as error:  # noqa: BLE001 - a caller gets Failed, never an exception
            return Failed(f"Frame.io read API unavailable: {type(error).__name__}: {error}")
        if status == 200 and "value" in answer:
            value: JsonValue = answer["value"]
            return value
        detail = str(answer.get("error", answer))
        if status == 503 and detail == SIGN_IN_REQUIRED:
            return SignInNeeded()
        return Failed(f"Frame.io read API answered {status}: {detail}")
