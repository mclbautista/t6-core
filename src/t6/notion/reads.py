"""Read Projects, Contacts and Deliverables as `t6.domain` entities, with no Notion property names.

`ProjectReader`, `ContactReader` and `DeliverableReader` each read one data
source. `list` skips pages in the trash or archived, and a Project or Contact
carries the page's `last_edited` time. Fields are addressed by property ID through the generated models.
Relations come back as page IDs.

Project Code identity and the registry are separate: this mapping fills the
page fields only (`t6_id` and `rotation` stay empty; see `t6.registry`).
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from t6.domain import Contact, Deliverable, Project
from t6.notion.generated import ContactsRow, DeliverablesRow, ProjectsRow
from t6.notion.model import NotionModel
from t6.notion.sync import NotionClient

JSON = dict[str, Any]

_SHIFT = re.compile(r"^\s*(\d{1,2})(?::(\d{2}))?\s*([AaPp])\.?[Mm]\.?\s*$|^\s*(\d{1,2}):(\d{2})\s*$")


def parse_end_of_shift(text: str | None) -> dt.time | None:
    """A time of day from "10:00 PM", "10 PM" or "22:00". Blank, "Flexible / Variable" or anything else is None."""
    match = _SHIFT.match(text or "")
    if not match:
        return None
    if match[4] is not None:
        hour, minute = int(match[4]), int(match[5])
    else:
        hour, minute = int(match[1]), int(match[2] or 0)
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if match[3].lower() == "p" else 0)
    return dt.time(hour, minute) if hour < 24 and minute < 60 else None


def _names(text: str | None) -> tuple[str, ...]:
    return tuple(part.strip() for part in re.split(r"[,;\n]", text or "") if part.strip())


def page_edited(page: JSON) -> dt.datetime | None:
    """When Notion last saved the page, from the page's own `last_edited_time`."""
    value = page.get("last_edited_time")
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) and value else None


def is_live(page: JSON) -> bool:
    """False for a page that is in the trash or archived."""
    return not (page.get("in_trash") or page.get("archived"))


def _key(model: type[NotionModel], attribute: str) -> str:
    return model.notion_id(attribute) or model.notion_name(attribute)


def project_from_row(row: ProjectsRow, last_edited: dt.datetime | None = None) -> Project:
    return Project(
        page_id=row.page_id,
        name=row.name or "",
        project_code=row.project_code,
        project_type=row.project_type,
        eps=None if row.eps is None or row.eps != int(row.eps) else int(row.eps),
        status=row.status,
        studio_id=row.studio[0] if row.studio else None,
        stage=row.stage,
        health=row.health,
        playdate=row.playdate,
        submission_deadline=row.submission_deadline,
        archival_deadline=row.archival_deadline,
        pm_notes=row.pm_notes,
        tech_notes=row.tech_notes,
        has_post_supe=row.has_post_supe,
        editor_ids=row.editors,
        assistant_editor_ids=row.assistant_editor,
        online_editor_ids=row.online_editor,
        colorist_ids=row.colorist,
        sound_tech_ids=row.sound_tech,
        post_coordinator_ids=row.t6_post_coordinator,
        director_ids=row.director,
        cinematographer_ids=row.cinematographer,
        last_edited=last_edited,
    )


def contact_from_row(row: ContactsRow, last_edited: dt.datetime | None = None) -> Contact:
    user_id = (row.telegram_user_id or "").strip()
    return Contact(
        page_id=row.page_id,
        name=row.name or "",
        email=row.email,
        telegram_handle=row.telegram_handle,
        telegram_user_id=int(user_id) if user_id.isdigit() else None,
        aliases=_names(row.aliases),
        end_of_shift=parse_end_of_shift(row.end_of_shift),
        last_edited=last_edited,
    )


def deliverable_from_row(row: DeliverablesRow) -> Deliverable:
    due = row.deadline.date() if isinstance(row.deadline, dt.datetime) else row.deadline
    return Deliverable(
        page_id=row.page_id,
        name=row.name or "",
        project_id=row.projects[0] if row.projects else None,
        due=due,
        status=row.status,
        deliverable_class=row.deliverable_class or row.class_,
        playdate=row.playdate,
    )


class ProjectReader:
    def __init__(self, client: NotionClient, data_source_id: str) -> None:
        self._client, self._source = client, data_source_id

    def get(self, page_id: str) -> Project:
        page = self._client.retrieve_page(page_id)
        return project_from_row(ProjectsRow.from_page(page), page_edited(page))

    def list(self) -> tuple[Project, ...]:
        pages = self._client.query_data_source(self._source)
        return tuple(project_from_row(ProjectsRow.from_page(page), page_edited(page)) for page in pages if is_live(page))


class ContactReader:
    def __init__(self, client: NotionClient, data_source_id: str) -> None:
        self._client, self._source = client, data_source_id

    def get(self, page_id: str) -> Contact:
        page = self._client.retrieve_page(page_id)
        return contact_from_row(ContactsRow.from_page(page), page_edited(page))

    def list(self) -> tuple[Contact, ...]:
        pages = self._client.query_data_source(self._source)
        return tuple(contact_from_row(ContactsRow.from_page(page), page_edited(page)) for page in pages if is_live(page))


class DeliverableReader:
    def __init__(self, client: NotionClient, data_source_id: str) -> None:
        self._client, self._source = client, data_source_id

    def for_project(self, project_id: str) -> tuple[Deliverable, ...]:
        """The Deliverables related to the Project page `project_id`."""
        pages = self._client.query_data_source(
            self._source,
            filter={"property": _key(DeliverablesRow, "projects"), "relation": {"contains": project_id}},
        )
        return tuple(deliverable_from_row(DeliverablesRow.from_page(page)) for page in pages)
