"""The one-off read of the Notion External Links database for `t6 admin media migrate`.

External Links is retired: no reader in t6-core uses it any more (Media
locations replace it). The migration still has to read its rows once to turn
them into Media locations or into the review list, so this module holds that
read and nothing else. It never writes to Notion and it never builds a domain
object from a row; it hands the raw fields to `MediaStore.migrate`. Delete this
module when the migration has run and the database is archived. The property
names it reads (`ExternalLinksProperties`) stay in `t6.notion.models`, which the
registry migration and the Notion test fakes use too.
"""

from __future__ import annotations

from typing import Any

from t6.notion import codec
from t6.notion.models import ExternalLinksProperties
from t6.notion.source import NotionSource, as_source
from t6.notion.sync import NotionClient

JSON = dict[str, Any]


_P = ExternalLinksProperties


def read_external_links_once_for_migration(source: NotionSource | NotionClient, data_source_id: str) -> list[JSON]:
    """Read every live External Links row, including incomplete rows for the review list. Migration only."""
    from t6.notion.reads import is_live

    notion = as_source(source)
    rows = []
    for page in notion.query_data_source(data_source_id, freshness="live"):
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
