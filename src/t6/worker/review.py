"""Tell the review chat that a person has finished commenting on a Frame.io file.

One job handler for the main worker, `frameio.review_done`. The server queues
the job when one person has made no new comment on one file for the review
time (`T6_REVIEW_QUIET_SECONDS`). Its payload carries the file, the person, how
many comments they made in this burst (`count`) and the times of their first
and last comment (`first_at`, `last_at`: seconds since the epoch, from Frame.io).

The message goes to the review chat through the gateway's `/notify`, keyed by
the job, so a job that runs twice sends once. This worker holds no Frame.io
token and reads no Notion: the file's title and link, the file's comment count
and the project's shares come from the Frame.io worker through the `frameio`
queue. When it cannot answer, the job is tried again; on the third try the
message is sent anyway with "unknown" where the answer is missing.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from datetime import datetime, timezone
import html

from t6.domain.dateformats import long_date
from t6.domain.events import JobKind, JsonValue
from t6.frameio.model import FrameioFile, ShareLink, decode
from t6.frameio.requests import Comments, FileInfo, FrameioRequest, FrameioResult, Ok, ShareLinks
from t6.worker.client import WorkerJob
from t6.worker.gateway import GatewayClient
from t6.worker.runner import Handler, Refused

__all__ = ["ReviewMessage"]

SEND_ANYWAY_AT_ATTEMPT = 3
"""The attempt (counting from 1) that sends even when Frame.io could not be read."""

UNKNOWN = "unknown"

def link(url: str, label: str) -> str:
    """A Telegram HTML hyperlink. Both parts are escaped, the URL for use inside `href`."""
    return f'<a href="{html.escape(url, quote=True)}">{html.escape(label, quote=False)}</a>'


class ReviewMessage:
    def __init__(
        self,
        gateway: GatewayClient,
        chat: int,
        topic: int | None,
        ask_frameio: Callable[[FrameioRequest], FrameioResult],
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        target: Callable[[str], tuple[int, int | None] | None] | None = None,
        version: Callable[[str], dict[str, Any] | None] | None = None,
        track_send: Callable[[int, int, int | None], None] | None = None,
    ) -> None:
        self._gateway = gateway
        self._chat = chat
        self._topic = topic
        self._ask = ask_frameio
        self._now = now
        self._target = target
        self._version = version
        self._track_send = track_send

    def handlers(self) -> dict[str, Handler]:
        return {JobKind.FRAMEIO_REVIEW_DONE.value: self.notify}

    def notify(self, job: WorkerJob) -> JsonValue:
        payload = job.payload
        count, first, last = payload.get("count"), payload.get("first_at"), payload.get("last_at")
        file_id, project_id = payload.get("file_id"), payload.get("frameio_project_id")
        if not isinstance(count, int) or not isinstance(first, (int, float)) or not isinstance(last, (int, float)):
            raise Refused("review job without a comment count and first and last comment times")
        if not isinstance(file_id, str) or not isinstance(project_id, str):
            raise Refused("review job without a Frame.io file and project")
        name = payload.get("user_name")
        who = html.escape(name if isinstance(name, str) and name.strip() else "Someone", quote=False)

        file, total, shares = self._read_file(file_id), self._read_total(file_id), self._read_shares(project_id)
        if (file is None or total is None or shares is None) and job.attempt < SEND_ANYWAY_AT_ATTEMPT:
            raise RuntimeError("Frame.io did not answer; the message is sent after the last try")

        title = file.name if file is not None and file.name else UNKNOWN
        asset = link(file.url, "open in Frame.io (sign in)") if file is not None and file.url else UNKNOWN
        comments = "1 comment" if count == 1 else f"{count} comments"
        text = (
            f"<b>{who}</b> finished commenting on <b>{html.escape(title, quote=False)}</b>\n"
            f"{self._version_line(file_id, job.attempt)}"
            f"Share link: {self._share_text(shares, {file_id, *([file.parent_id] if file is not None and file.parent_id else [])})}\n"
            f"Frame.io link: {asset}\n"
            f"Started: {long_date(first)}\n"
            f"Stopped: {long_date(last)}\n"
            f"{comments} from them, {UNKNOWN if total is None else total} on the file in total"
        )
        target = self._target(project_id) if self._target is not None else None
        chat, topic = target if target is not None else (self._chat, self._topic)
        ref = self._gateway.notify(chat, text, [], f"frameio-review:{job.dedupe_key}", topic=topic)
        if target is not None and self._track_send is not None:
            self._track_send(ref, chat, topic)
        return {"message_ref": ref, "count": count}

    def _version_line(self, file_id: str, attempt: int) -> str:
        """The file's Version as a line of the message, or nothing when t6 has no Version for it.

        A lookup that fails is tried again with the job; the last try sends without the line.
        """
        if self._version is None:
            return ""
        try:
            version = self._version(file_id)
        except Exception as error:
            if attempt < SEND_ANYWAY_AT_ATTEMPT:
                raise RuntimeError("t6 did not answer for the Version; the message is sent after the last try") from error
            return ""
        if version is None:
            return ""
        parts = [str(version["deliverable"]), *([str(version["note"])] if version.get("note") else []), str(version["id"])]
        locked = " (locked)" if version.get("locked") else ""
        return f"Version: {html.escape(', '.join(parts), quote=False)}{locked}\n"

    def _read_file(self, file_id: str) -> FrameioFile | None:
        result = self._ask(FileInfo(file_id=file_id))
        try:
            return FrameioFile.from_json(result.value) if isinstance(result, Ok) and isinstance(result.value, dict) else None
        except (TypeError, KeyError):
            return None

    def _read_total(self, file_id: str) -> int | None:
        result = self._ask(Comments(file_id=file_id))
        try:
            return len(decode(Comments, result.value)) if isinstance(result, Ok) else None
        except ValueError:
            return None

    def _read_shares(self, project_id: str) -> tuple[ShareLink, ...] | None:
        result = self._ask(ShareLinks(frameio_project_id=project_id))
        try:
            return decode(ShareLinks, result.value) if isinstance(result, Ok) else None
        except ValueError:
            return None

    def _share_text(self, shares: tuple[ShareLink, ...] | None, held: set[str]) -> str:
        """The newest live share that holds the file (or its folder or version stack), as a link.

        "no share link" when every live share was checked and none holds it;
        "unknown" when some live shares were not checked (past the scan limit).
        """
        if shares is None:
            return UNKNOWN
        now = self._now()
        found = [s for s in shares if s.url and s.live(now) and held.intersection(s.file_ids)]
        if not found:
            return UNKNOWN if any(s.live(now) and not s.files_known for s in shares) else "no share link"
        newest = max(found, key=lambda s: s.created_at or datetime.min.replace(tzinfo=timezone.utc))
        assert newest.url is not None
        return link(newest.url, newest.url)
