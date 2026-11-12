"""Tell the review chat that a person has finished commenting on a Frame.io file.

One job handler for the main worker, `frameio.review_done`. The server queues
the job when one person has made no new comment on one file for the review
time (`T6_REVIEW_QUIET_SECONDS`). Its payload carries the file, the person, how
many comments they made in this burst (`count`) and the times of their first
and last comment (`first_at`, `last_at`: seconds since the epoch, from Frame.io).

The message goes to the review chat through the gateway's `/notify`, keyed by
the job, so a job that runs twice sends once. This worker holds no Frame.io
token and reads no Notion.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import html

from t6.domain.events import JobKind, JsonValue
from t6.worker.client import WorkerJob
from t6.worker.gateway import GatewayClient
from t6.worker.runner import Handler, Refused

__all__ = ["ReviewMessage"]

MANILA = timezone(timedelta(hours=8), "Asia/Manila")
"""Asia/Manila has no daylight saving time, so a fixed offset is exact."""


def manila(epoch_seconds: float) -> str:
    """`12 Nov 2026, 19:05` in Manila time."""
    return datetime.fromtimestamp(epoch_seconds, MANILA).strftime("%d %b %Y, %H:%M")


class ReviewMessage:
    def __init__(self, gateway: GatewayClient, chat: int, topic: int | None = None) -> None:
        self._gateway = gateway
        self._chat = chat
        self._topic = topic

    def handlers(self) -> dict[str, Handler]:
        return {JobKind.FRAMEIO_REVIEW_DONE.value: self.notify}

    def notify(self, job: WorkerJob) -> JsonValue:
        payload = job.payload
        count, first, last = payload.get("count"), payload.get("first_at"), payload.get("last_at")
        if not isinstance(count, int) or not isinstance(first, (int, float)) or not isinstance(last, (int, float)):
            raise Refused("review job without a comment count and first and last comment times")
        name = payload.get("user_name")
        who = html.escape(name if isinstance(name, str) and name.strip() else "Someone", quote=False)
        comments = "1 comment" if count == 1 else f"{count} comments"
        text = (
            f"<b>{who}</b> finished commenting on a Frame.io file.\n"
            f"Started: {manila(first)} (Manila)\n"
            f"Stopped: {manila(last)} (Manila)\n"
            f"{comments} from them"
        )
        ref = self._gateway.notify(self._chat, text, [], f"frameio-review:{job.dedupe_key}", topic=self._topic)
        return {"message_ref": ref, "count": count}
