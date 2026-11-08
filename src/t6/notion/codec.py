"""Decode Notion property values into plain Python values.

Each function takes one property object from a page's `properties` (or None
when the property is missing) and returns a Python value. Missing, empty and
wrong-typed properties decode to None (or an empty tuple), so a model tolerates
schema drift instead of crashing.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

JSON = dict[str, Any]


def _value(prop: JSON | None, kind: str) -> Any:
    if not prop or prop.get("type") != kind:
        return None
    return prop.get(kind)


def plain_text(rich_text: list[JSON] | None) -> str:
    """Join a rich-text array into one string."""
    return "".join(str(part.get("plain_text", "")) for part in rich_text or [])


def _text(prop: JSON | None, kind: str) -> str | None:
    text = plain_text(_value(prop, kind))
    return text or None


def title(prop: JSON | None) -> str | None:
    return _text(prop, "title")


def rich_text(prop: JSON | None) -> str | None:
    return _text(prop, "rich_text")


def number(prop: JSON | None) -> float | int | None:
    value = _value(prop, "number")
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def integer(prop: JSON | None) -> int | None:
    """A number property that must hold a whole number; anything else is None."""
    value = number(prop)
    if value is None or value != int(value):
        return None
    return int(value)


def _option_name(option: Any) -> str | None:
    if isinstance(option, dict) and option.get("name"):
        return str(option["name"])
    return None


def select(prop: JSON | None) -> str | None:
    return _option_name(_value(prop, "select"))


def status(prop: JSON | None) -> str | None:
    return _option_name(_value(prop, "status"))


def multi_select(prop: JSON | None) -> tuple[str, ...]:
    options = _value(prop, "multi_select") or []
    return tuple(name for name in map(_option_name, options) if name)


def relation(prop: JSON | None) -> tuple[str, ...]:
    """Related page IDs, in Notion's order."""
    return tuple(str(item["id"]) for item in _value(prop, "relation") or [] if item.get("id"))


def checkbox(prop: JSON | None) -> bool | None:
    value = _value(prop, "checkbox")
    return value if isinstance(value, bool) else None


def url(prop: JSON | None) -> str | None:
    return _value(prop, "url") or None


def email(prop: JSON | None) -> str | None:
    return _value(prop, "email") or None


def date_start(prop: JSON | None) -> date | datetime | None:
    """The start of a date property: a date, or a datetime when it has a time."""
    value = _value(prop, "date")
    start = value.get("start") if isinstance(value, dict) else None
    if not start:
        return None
    if "T" in start:
        return datetime.fromisoformat(start)
    return date.fromisoformat(start)
