"""Named values for select and status properties, keyed by Notion option ID.

T6 knows some option values by name, such as the Job Order statuses
(`t6.domain.JobOrderStatus`). The schema snapshot can record each one's Notion
option ID under `named_options`:

    "named_options": {"status": {"property": "<property ID or name>",
                                 "values": {"NEEDS_INTAKE_REVIEW": "<option ID>"}}}

The key is a domain attribute, not a Notion name. `t6 schema-snapshot` fills
the IDs by matching live option names to the value's label, once; after that
an option renamed in Notion is still found by its ID. A value with no ID
recorded is sent and read by its label. `schema-check` reports a named value
whose option is no longer live.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from t6.domain import Department, JobOrderStatus
from t6.notion.model import OptionId

JSON = dict[str, Any]

NAMED_FIELDS: dict[str, dict[str, type[StrEnum]]] = {
    "job_orders": {"status": JobOrderStatus, "department": Department},
}
"""Snapshot database key, then domain attribute, to the enum of known values."""

FIRST_FILL_PROPERTY: dict[tuple[str, str], str] = {
    ("job_orders", "status"): "Status",
    ("job_orders", "department"): "Department",
}
"""The property name to look for the first time a named block is filled."""


def named_block(snapshot: JSON, database: str, attribute: str) -> JSON | None:
    block = (snapshot["databases"].get(database) or {}).get("named_options", {}).get(attribute)
    return block if isinstance(block, dict) else None


def to_option(snapshot: JSON, database: str, attribute: str, value: str | None) -> OptionId | str | None:
    """What to send for a value: its `OptionId` when the snapshot knows it, else its label."""
    if not value:
        return None
    enum = NAMED_FIELDS[database][attribute]
    try:
        member = enum(value)
    except ValueError:
        return str(value)
    block = named_block(snapshot, database, attribute)
    option_id = ((block or {}).get("values") or {}).get(member.name)
    return OptionId(option_id) if option_id else member.value


def from_option(snapshot: JSON, database: str, attribute: str, option: JSON | None) -> StrEnum | str | None:
    """The named value an option stands for: found by ID, else by label, else its plain name."""
    if not option or not option.get("name"):
        return None
    enum = NAMED_FIELDS[database][attribute]
    values = (named_block(snapshot, database, attribute) or {}).get("values") or {}
    for key, option_id in values.items():
        if option_id == option.get("id") and key in enum.__members__:
            return enum[key]
    try:
        return enum(option["name"])
    except ValueError:
        return str(option["name"])


def live_options(prop: JSON) -> list[JSON]:
    """The options of a live select or status property, as `{id, name}` objects."""
    options = (prop.get(str(prop.get("type"))) or {}).get("options")
    return options if isinstance(options, list) else []


def find_live(properties: JSON, pointer: str) -> JSON | None:
    """The live property whose ID, or else whose name, is `pointer`."""
    props = [{**prop, "name": str(prop.get("name", key))} for key, prop in properties.items()]
    return next((p for p in props if str(p.get("id")) == pointer), None) or next(
        (p for p in props if p["name"] == pointer), None
    )


def missing_values(snapshot_database: JSON, database: str, properties: JSON) -> list[JSON]:
    """Named values whose option is not live: by ID when the snapshot has one, else by label."""
    missing: list[JSON] = []
    for attribute, enum in NAMED_FIELDS.get(database, {}).items():
        block = (snapshot_database.get("named_options") or {}).get(attribute)
        if not block:
            continue
        live = find_live(properties, str(block["property"]))
        if live is None:
            continue
        options = live_options(live)
        ids = {str(option.get("id")) for option in options}
        names = {str(option.get("name")) for option in options}
        for member in enum:
            option_id = (block.get("values") or {}).get(member.name)
            if (option_id not in ids) if option_id else (member.value not in names):
                missing.append({"attribute": attribute, "value": member.name})
    return missing


def fill(snapshot_database: JSON, database: str, properties: JSON) -> None:
    """Record the option ID of each named value found live, in `snapshot_database`'s `named_options`."""
    for attribute, enum in NAMED_FIELDS.get(database, {}).items():
        block = (snapshot_database.get("named_options") or {}).get(attribute) or {
            "property": FIRST_FILL_PROPERTY[(database, attribute)],
            "values": {},
        }
        live = find_live(properties, str(block["property"]))
        if live is None:
            continue
        block["property"] = str(live.get("id") or live["name"])
        by_name = {str(option.get("name")): option for option in live_options(live)}
        for member in enum:
            option = by_name.get(member.value)
            if member.name not in block["values"] and option is not None and "id" in option:
                block["values"][member.name] = str(option["id"])
        snapshot_database.setdefault("named_options", {})[attribute] = block
