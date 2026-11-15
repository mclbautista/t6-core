"""The one-off read of the Notion External Links database for `t6 admin media migrate`.

External Links is retired: no reader in t6-core uses it any more (Media
locations replace it). The migration still has to read its rows once to turn
them into Media locations or into the review list, so this module holds that
read and nothing else. It never writes to Notion and it never builds a domain
object from a row; it hands the raw fields to `MediaStore.migrate`. Delete this
module when the migration has run and the database is archived.
"""

from __future__ import annotations

from typing import Any, Final

from t6.notion import codec
from t6.notion.sync import NotionClient

JSON = dict[str, Any]


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


_P = ExternalLinksProperties


def read_external_links_once_for_migration(client: NotionClient, data_source_id: str) -> list[JSON]:
    """Read every live External Links row, including incomplete rows for the review list. Migration only."""
    from t6.notion.reads import is_live

    rows = []
    for page in client.query_data_source(data_source_id):
        if not is_live(page):
            continue
        props = page.get("properties") or {}
        rows.append({
            "page_id": str(page["id"]),
            "system": codec.select(props.get(_P.SYSTEM)),
            "kind": codec.select(props.get(_P.KIND)),
            "active": codec.checkbox(props.get(_P.ACTIVE)) is True,
            "ref": codec.rich_text(props.get(_P.EXTERNAL_ID)),
            "projects": list(codec.relation(props.get(_P.PROJECTS))),
            "deliverables": list(codec.relation(props.get(_P.DELIVERABLES))),
        })
    return rows
