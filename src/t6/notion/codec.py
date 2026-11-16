"""Decode Notion property values into plain Python values.

Each function takes one property object from a page's `properties` (or None
when the property is missing) and returns a Python value. Missing, empty and
wrong-typed properties decode to None (or an empty tuple), so a model tolerates
schema drift instead of crashing.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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


def _moment(text: str | None, zone: Any = None) -> date | datetime | None:
    """A date, or a datetime when `text` has a time. A time with no offset takes `zone` (a name such as "America/New_York") when it names one."""
    if not text:
        return None
    if "T" not in text:
        return date.fromisoformat(text)
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None and isinstance(zone, str) and zone:
        try:
            return moment.replace(tzinfo=ZoneInfo(zone))
        except (ZoneInfoNotFoundError, ValueError):
            return moment
    return moment


def date_start(prop: JSON | None) -> date | datetime | None:
    """The start of a date property: a date, or a datetime when it has a time.

    A time with no offset takes the value's `time_zone` when Notion holds one;
    with neither it stays naive (the workspace time).
    """
    value = _value(prop, "date")
    return _moment(value.get("start"), value.get("time_zone")) if isinstance(value, dict) else None


def date_range(prop: JSON | None) -> tuple[date | datetime | None, date | datetime | None]:
    """The start and the end of a date property. The end is None when the date has none; it keeps its time as Notion holds it."""
    value = _value(prop, "date")
    if not isinstance(value, dict):
        return None, None
    zone = value.get("time_zone")
    return _moment(value.get("start"), zone), _moment(value.get("end"), zone)


def phone_number(prop: JSON | None) -> str | None:
    return _value(prop, "phone_number") or None


def people(prop: JSON | None) -> tuple[str, ...]:
    """User IDs of a people property, in Notion's order."""
    return tuple(str(item["id"]) for item in _value(prop, "people") or [] if item.get("id"))


def timestamp(prop: JSON | None) -> datetime | None:
    """A created_time or last_edited_time property."""
    if not prop or prop.get("type") not in ("created_time", "last_edited_time"):
        return None
    value = prop.get(str(prop["type"]))
    return datetime.fromisoformat(value) if isinstance(value, str) and value else None


def raw(prop: JSON | None) -> JSON | None:
    """The property object as Notion sent it, for read-only types such as formula."""
    return dict(prop) if prop else None
