"""Read the Notion Swing Drives database for `t6 admin drive import --swing`.

Swing Drives live once, in t6.drives; this database is imported and then
retired. The reader only reads, and it hands raw fields to the importer
(`t6.swing_import`). Properties are read by name, as the live data source has
them: Name, In Use, Last Moved, Collaborator (a relation to Contacts), Format
(the name has a trailing space), HD Type and Short Notes. There is no Serial
and no Projects property. Person-In-Charge (a Notion user) and For Bawi are
not read. Delete this module when the import has run and the database is
archived.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final

from t6.notion import codec
from t6.notion.reads import is_live
from t6.notion.sync import NotionClient

JSON = dict[str, Any]


class SwingDrivesProperties:
    """Notion property names on the Swing Drives data source."""

    NAME: Final = "Name"  # title: the label on the sticker, never identity
    IN_USE: Final = "In Use"  # checkbox
    LAST_MOVED: Final = "Last Moved"  # date
    COLLABORATOR: Final = "Collaborator"  # relation to the Contacts database
    FORMAT: Final = "Format "  # select; the trailing space is part of the live name
    HD_TYPE: Final = "HD Type"  # multi-select
    SHORT_NOTES: Final = "Short Notes"  # rich text


_P = SwingDrivesProperties


def read_swing_drives(client: NotionClient, data_source_id: str) -> list[JSON]:
    """Every live Swing Drives row as plain facts, keyed by its page ID."""
    rows = []
    for page in client.query_data_source(data_source_id):
        if not is_live(page):
            continue
        props = page.get("properties") or {}
        moved = codec.date_start(props.get(_P.LAST_MOVED))
        rows.append({
            "page_id": str(page["id"]),
            "name": codec.title(props.get(_P.NAME)),
            "in_use": codec.checkbox(props.get(_P.IN_USE)) is True,
            "last_moved": (moved.date() if isinstance(moved, datetime) else moved).isoformat() if moved else None,
            "collaborators": list(codec.relation(props.get(_P.COLLABORATOR))),
            "format": codec.select(props.get(_P.FORMAT)),
            "hd_types": list(codec.multi_select(props.get(_P.HD_TYPE))),
            "notes": codec.rich_text(props.get(_P.SHORT_NOTES)),
        })
    return rows
