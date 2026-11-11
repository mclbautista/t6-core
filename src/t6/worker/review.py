"""Tell a show's coordinator that a Frame.io review went quiet (needs the `notion` extra).

One job handler for the main worker, `frameio.review_done`. The server queues
the job when a Frame.io project has had no new comment for 15 minutes; its
payload carries the Frame.io project ID and the comment `count`.

The show is the Project that the Frame.io project's External Links row
(`frameio` / `project`) is related to. Its coordinators are the Contacts in
`T6 Post Coordinator`; each one with a `TelegramUserID` gets one DM through the
gateway's `/notify`, keyed by the job, so a job that runs twice sends once.

The link in the DM comes from the Frame.io worker through the `frameio`
queue. This worker holds no Frame.io token. When the Frame.io worker gives no
answer, the URL on the External Links row is used; with neither, the job is
tried again.
"""

from __future__ import annotations

from collections.abc import Callable
import html

from t6.domain import ExternalRef
from t6.domain.events import JobKind, JsonValue
from t6.frameio.model import decode
from t6.frameio.requests import FrameioProjects, FrameioRequest, FrameioResult, Ok
from t6.notion.external_links import ExternalLinks
from t6.notion.generated import ContactsRow, ProjectsRow
from t6.notion.sync import NotionClient
from t6.worker.client import WorkerJob
from t6.worker.gateway import GatewayClient
from t6.worker.runner import Handler, Refused

__all__ = ["ReviewDone"]


class ReviewDone:
    def __init__(
        self,
        notion: NotionClient,
        links: ExternalLinks,
        gateway: GatewayClient,
        ask_frameio: Callable[[FrameioRequest], FrameioResult],
    ) -> None:
        self._notion = notion
        self._links = links
        self._gateway = gateway
        self._ask = ask_frameio

    def handlers(self) -> dict[str, Handler]:
        return {JobKind.FRAMEIO_REVIEW_DONE.value: self.notify}

    def notify(self, job: WorkerJob) -> JsonValue:
        frameio_project_id, count = job.payload.get("frameio_project_id"), job.payload.get("count")
        if not isinstance(frameio_project_id, str) or not isinstance(count, int):
            raise Refused("review job without a Frame.io project and a comment count")
        row = self._links.find(ExternalRef(system="frameio", kind="project", external_id=frameio_project_id))
        if row is None or not row.project_ids:
            raise Refused(f"Frame.io project {frameio_project_id} has no External Links row related to a Project")
        show = ProjectsRow.from_page(self._notion.retrieve_page(row.project_ids[0]))
        chats: list[int] = []
        for contact_id in show.t6_post_coordinator:
            user_id = (ContactsRow.from_page(self._notion.retrieve_page(contact_id)).telegram_user_id or "").strip()
            if user_id.isdigit():
                chats.append(int(user_id))
        if not chats:
            raise Refused("the show has no T6 Post Coordinator with a TelegramUserID")
        link = self._link(frameio_project_id) or row.ref.url
        if link is None:
            raise RuntimeError("no link for the review yet: the Frame.io worker gave no answer")
        comments = "1 comment" if count == 1 else f"{count} comments"
        text = (
            f"Frame.io review done on <b>{html.escape(show.name or 'a show', quote=False)}</b>: {comments}.\n"
            f"{html.escape(link, quote=False)}"
        )
        for chat in chats:
            self._gateway.notify(chat, text, [], f"frameio-review-done:{job.dedupe_key}:{chat}")
        return {"notified": [*chats], "count": count}

    def _link(self, frameio_project_id: str) -> str | None:
        result = self._ask(FrameioProjects())
        if not isinstance(result, Ok):
            return None
        try:
            projects = decode(FrameioProjects, result.value)
        except ValueError:
            return None
        return next((project.url for project in projects if project.id == frameio_project_id), None)
