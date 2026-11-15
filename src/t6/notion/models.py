"""Hand-checked Notion models that turn pages into `t6.domain` entities.

Property names are the exact Notion names. The Projects mapping was checked
against the live Projects data source schema on 2026-11-08. Generated models
for every database, built from the schema snapshot, are in `t6.notion.generated`.
"""

from __future__ import annotations

from typing import Any, Final

from t6.domain import Project
from t6.notion import codec

JSON = dict[str, Any]


class ProjectsProperties:
    """Notion property names on the Projects data source, and their types."""

    NAME: Final = "Name"  # title
    PROJECT_CODE: Final = "Project Code"  # select
    PROJECT_TYPE: Final = "Project Type"  # select
    EPS: Final = "Eps"  # number
    STATUS: Final = "Status"  # select
    STUDIO: Final = "Studio"  # relation to Studios


class ExternalLinksProperties:
    """Notion property names on the retired External Links data source."""

    NAME: Final = "Name"  # title
    SYSTEM: Final = "System"  # select
    KIND: Final = "Kind"  # select
    EXTERNAL_ID: Final = "External ID"  # rich text
    INTERNAL_ID: Final = "Internal ID"  # rich text
    URL: Final = "URL"  # url
    ACTIVE: Final = "Active"  # checkbox, set by people only
    LAST_ACTIVITY: Final = "Last Activity"  # date
    PROJECTS: Final = "Projects"  # relation
    DELIVERABLES: Final = "Deliverables"  # relation
    CONTACTS: Final = "Contacts"  # relation
    BAYS: Final = "Bays"  # relation
    STORAGE: Final = "Storage"  # relation
    CAMERAS: Final = "Cameras"  # relation


def project_from_page(page: JSON) -> Project:
    """Build a domain Project from a Projects page. Unknown properties are ignored."""
    props: JSON = page.get("properties") or {}
    p = ProjectsProperties
    studios = codec.relation(props.get(p.STUDIO))
    return Project(
        page_id=str(page["id"]),
        name=codec.title(props.get(p.NAME)) or "",
        project_code=codec.select(props.get(p.PROJECT_CODE)),
        project_type=codec.select(props.get(p.PROJECT_TYPE)),
        eps=codec.integer(props.get(p.EPS)),
        status=codec.select(props.get(p.STATUS)),
        studio_id=studios[0] if studios else None,
    )
