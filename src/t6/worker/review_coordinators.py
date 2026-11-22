"""The Telegram handles of the coordinators of the Project a Frame.io project is linked to, for the review message (#547)."""
from __future__ import annotations

from t6.media_links import MediaLinks
from t6.notion.reads import ContactReader, ProjectReader


class ReviewCoordinators:
    """`Prod Coor/Supe` of the linked Project, as Telegram handles, read from the local Notion mirror.

    Reads one Project page and one Contact page for each coordinator (`freshness="mirror"`, never a data source scan).
    Anything missing (no link, no coordinator, no handle, a page that cannot be read) is left out: the caller sends the
    message without a tag.
    """

    def __init__(self, links: MediaLinks, projects: ProjectReader, contacts: ContactReader) -> None:
        self._links, self._projects, self._contacts = links, projects, contacts

    def __call__(self, frameio_project_id: str) -> tuple[str, ...]:
        page = next((link.pages[0] for link in self._links.media_links("frameio-project")
                     if link.ref == frameio_project_id and link.pages), None)
        if page is None:
            return ()
        handles: list[str] = []
        for contact_id in self._projects.get(page, freshness="mirror").prod_coor_supe_ids:
            try:
                handle = (self._contacts.get(contact_id, freshness="mirror").telegram_handle or "").strip().lstrip("@").strip()
            except Exception:
                continue  # one unreadable Contact does not hide the others
            if handle and handle not in handles:
                handles.append(handle)
        return tuple(handles)
