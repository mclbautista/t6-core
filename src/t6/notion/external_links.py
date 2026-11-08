"""External Links: tie a Notion row to an object in another tool.

Code reads rows as domain `ExternalRef`s and creates them idempotently. Code
never sets Active, because people do: a created row leaves it untouched, and
`touch` writes Last Activity only.

Property names are the exact Notion names from the External Links design. They
have not been checked against the live data source yet, because the database
did not exist when this was written. System and Kind are assumed to be select
properties and External ID and Internal ID rich text; check them against the
schema when the database exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from t6.domain import ExternalRef
from t6.notion import codec
from t6.notion.sync import NotionClient

JSON = dict[str, Any]


class ExternalLinksProperties:
    """Notion property names on the External Links data source, and their types."""

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


_P = ExternalLinksProperties


@dataclass(frozen=True, slots=True, kw_only=True)
class ExternalLink:
    """One External Links row. Equal when it is the same Notion page."""

    page_id: str
    ref: ExternalRef
    active: bool
    last_activity: datetime | None
    project_ids: tuple[str, ...] = ()

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ExternalLink) and self.page_id == other.page_id

    def __hash__(self) -> int:
        return hash(self.page_id)


def external_link_from_page(page: JSON) -> ExternalLink | None:
    """Build an ExternalLink from a page, or None when it lacks System, Kind or External ID."""
    props: JSON = page.get("properties") or {}
    system = codec.select(props.get(_P.SYSTEM))
    kind = codec.select(props.get(_P.KIND))
    external_id = codec.rich_text(props.get(_P.EXTERNAL_ID))
    if not (system and kind and external_id) or not (
        system.strip() and kind.strip() and external_id.strip()
    ):
        return None
    last = codec.date_start(props.get(_P.LAST_ACTIVITY))
    return ExternalLink(
        page_id=str(page["id"]),
        ref=ExternalRef(
            system=system,
            kind=kind,
            external_id=external_id,
            url=codec.url(props.get(_P.URL)),
            internal_id=codec.rich_text(props.get(_P.INTERNAL_ID)),
        ),
        active=codec.checkbox(props.get(_P.ACTIVE)) is True,
        last_activity=last if isinstance(last, datetime) else None,
        project_ids=codec.relation(props.get(_P.PROJECTS)),
    )


def _text(content: str) -> JSON:
    return {"rich_text": [{"type": "text", "text": {"content": content}}]}


def _relation(page_ids: list[str] | tuple[str, ...]) -> JSON:
    return {"relation": [{"id": page_id} for page_id in page_ids]}


class ExternalLinks:
    """Reads and creates External Links rows in one data source."""

    def __init__(self, client: NotionClient, data_source_id: str) -> None:
        self._client = client
        self._data_source_id = data_source_id

    def links_for_project(self, project_id: str) -> tuple[ExternalLink, ...]:
        """Every row related to this Project, in the order Notion returns them."""
        rows = self._client.query_data_source(
            self._data_source_id,
            filter={"property": _P.PROJECTS, "relation": {"contains": project_id}},
        )
        links = (external_link_from_page(row) for row in rows)
        return tuple(link for link in links if link is not None)

    def links_for_system(self, system: str, kind: str) -> tuple[ExternalLink, ...]:
        """Every row with this System and Kind."""
        rows = self._client.query_data_source(
            self._data_source_id,
            filter={
                "and": [
                    {"property": _P.SYSTEM, "select": {"equals": system}},
                    {"property": _P.KIND, "select": {"equals": kind}},
                ]
            },
        )
        links = (external_link_from_page(row) for row in rows)
        return tuple(link for link in links if link is not None)

    def refs_for_project(self, project_id: str) -> tuple[ExternalRef, ...]:
        """The domain ExternalRefs of this Project's rows."""
        return tuple(link.ref for link in self.links_for_project(project_id))

    def find(self, ref: ExternalRef) -> ExternalLink | None:
        """The existing row for this System, Kind and External ID, if any."""
        rows = self._client.query_data_source(
            self._data_source_id,
            filter={
                "and": [
                    {"property": _P.SYSTEM, "select": {"equals": ref.system}},
                    {"property": _P.KIND, "select": {"equals": ref.kind}},
                    {"property": _P.EXTERNAL_ID, "rich_text": {"equals": ref.external_id}},
                ]
            },
        )
        for row in rows:
            link = external_link_from_page(row)
            if link is not None and link.ref == ref:
                return link
        return None

    def ensure(
        self,
        ref: ExternalRef,
        *,
        project_id: str | None = None,
        deliverable_ids: list[str] | None = None,
        contact_ids: list[str] | None = None,
        bay_ids: list[str] | None = None,
        storage_ids: list[str] | None = None,
        camera_ids: list[str] | None = None,
    ) -> ExternalLink:
        """Return the row for `ref`, creating it only if none exists.

        An existing row is returned as it is: nothing is written to it and no
        duplicate is made. A new row never sets Active. A create that fails on
        a 5xx or a timeout raises, because retrying it could make a duplicate.
        """
        existing = self.find(ref)
        if existing is not None:
            return existing
        properties: JSON = {
            _P.NAME: {"title": [{"type": "text", "text": {"content": _name(ref)}}]},
            _P.SYSTEM: {"select": {"name": ref.system}},
            _P.KIND: {"select": {"name": ref.kind}},
            _P.EXTERNAL_ID: _text(ref.external_id),
        }
        if ref.internal_id is not None:
            properties[_P.INTERNAL_ID] = _text(ref.internal_id)
        if ref.url is not None:
            properties[_P.URL] = {"url": ref.url}
        for name, ids in (
            (_P.PROJECTS, [project_id] if project_id else []),
            (_P.DELIVERABLES, deliverable_ids),
            (_P.CONTACTS, contact_ids),
            (_P.BAYS, bay_ids),
            (_P.STORAGE, storage_ids),
            (_P.CAMERAS, camera_ids),
        ):
            if ids:
                properties[name] = _relation(ids)
        page = self._client.create_page(
            {
                "parent": {"type": "data_source_id", "data_source_id": self._data_source_id},
                "properties": properties,
            }
        )
        link = external_link_from_page(page)
        assert link is not None  # the row was just built with all three identity fields
        return link

    def touch(self, page_id: str, when: datetime) -> ExternalLink:
        """Set Last Activity. Writes that property and nothing else, so Active stays as people left it."""
        if when.tzinfo is None:
            raise ValueError("when must carry a timezone")
        page = self._client.update_page(
            page_id, {"properties": {_P.LAST_ACTIVITY: {"date": {"start": when.isoformat()}}}}
        )
        link = external_link_from_page(page)
        if link is None:
            raise ValueError(f"page {page_id} is not an External Links row")
        return link


def _name(ref: ExternalRef) -> str:
    return f"{ref.system} / {ref.kind} / {ref.external_id}"
