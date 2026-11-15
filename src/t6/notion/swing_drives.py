"""Read the Notion Swing Drives database for `t6 admin drive import --swing`.

Swing Drives live once, in t6.drives; this database is imported and then
retired. The reader only reads, and it hands raw fields to the importer
(`t6.swing_import`). Properties are read by name. The committed schema snapshot
has no Serial or Projects property on this database: a database without them
gives rows with no serial and no Project, and the importer sends those to the
exception queue instead of guessing. Delete this module when the import has run
and the database is archived.
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
    SERIAL: Final = "Serial"  # rich text: the only identity the import accepts
    PROJECTS: Final = "Projects"  # relation to the Projects database
    IN_USE: Final = "In Use"  # checkbox
    LAST_MOVED: Final = "Last Moved"  # date


_P = SwingDrivesProperties


def read_swing_drives(client: NotionClient, data_source_id: str) -> list[JSON]:
    """Every live Swing Drives row as plain facts: page ID, name, serial text, use, date, Project pages."""
    rows = []
    for page in client.query_data_source(data_source_id):
        if not is_live(page):
            continue
        props = page.get("properties") or {}
        moved = codec.date_start(props.get(_P.LAST_MOVED))
        rows.append({
            "page_id": str(page["id"]),
            "name": codec.title(props.get(_P.NAME)),
            "serial": codec.rich_text(props.get(_P.SERIAL)),
            "in_use": codec.checkbox(props.get(_P.IN_USE)) is True,
            "last_moved": (moved.date() if isinstance(moved, datetime) else moved).isoformat() if moved else None,
            "projects": list(codec.relation(props.get(_P.PROJECTS))),
        })
    return rows
