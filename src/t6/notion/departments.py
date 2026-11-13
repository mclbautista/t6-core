"""Studio Departments, read live from the Job Orders `Department` options.

Notion owns which Departments exist: this module reads the options of the
`Department` select (by property ID, so a rename of the property or an option
needs no code change) and hands them back in the studio's display order, each
with its option ID. t6-core owns only the display order and the alias `TO` for
`TOC`. MISC is not a Department.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from t6.domain.job_order_rules import DEPARTMENT_ALIASES
from t6.notion.generated import JobOrdersRow
from t6.notion.sync import NotionClient

JSON = dict[str, Any]

DISPLAY_ORDER = ("TOC", "AE", "OE", "COOR", "COLOR", "VFX", "SOUND", "ADMIN")
"""Departments in report order. Any other Department follows, then none."""

ALIASES = DEPARTMENT_ALIASES
"""Short forms people type, mapped to the Department option name."""

NOT_DEPARTMENTS = frozenset({"MISC"})


@dataclass(frozen=True, slots=True)
class Department:
    """One Department option: its Notion option ID and current name."""

    option_id: str
    name: str


@dataclass(frozen=True, slots=True)
class UnknownDepartment:
    """A token that matches no Department. Never a guess."""

    token: str


def get_departments(client: NotionClient, data_source_id: str) -> tuple[Department, ...]:
    """The live Department options, in display order. MISC is left out."""
    live = client.retrieve_data_source(data_source_id).get("properties") or {}
    prop = _department_property(live)
    options = ((prop or {}).get("select") or {}).get("options") or []
    found = [
        Department(str(option["id"]), str(option["name"]))
        for option in options
        if str(option["name"]).strip().upper() not in NOT_DEPARTMENTS
    ]
    return tuple(sorted(found, key=lambda department: display_rank(department.name)))


def display_rank(name: str | None) -> tuple[int, str]:
    """A sort key: listed Departments first, other names next (A to Z), no Department last."""
    if name is None or not name.strip():
        return (len(DISPLAY_ORDER) + 1, "")
    key = name.strip().upper()
    if key in DISPLAY_ORDER:
        return (DISPLAY_ORDER.index(key), "")
    return (len(DISPLAY_ORDER), key)


def resolve_department(
    departments: tuple[Department, ...], token: str
) -> Department | UnknownDepartment:
    """The Department a token names, by name or alias, ignoring case and spaces."""
    key = token.strip().upper()
    key = ALIASES.get(key, key)
    for department in departments:
        if department.name.strip().upper() == key:
            return department
    return UnknownDepartment(token)


def _department_property(live: JSON) -> JSON | None:
    property_id = JobOrdersRow.notion_id("department")
    props = [{**prop, "name": str(prop.get("name", key))} for key, prop in live.items()]
    if property_id:
        for prop in props:
            if str(prop.get("id")) == property_id:
                return prop
    name = JobOrdersRow.notion_name("department")
    return next((prop for prop in props if prop["name"] == name), None)
