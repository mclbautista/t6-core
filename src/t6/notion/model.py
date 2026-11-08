"""Base class for the generated Notion models.

A model is a frozen dataclass with one field per Notion property. Each field
carries the exact Notion property name (trailing spaces and emoji included)
and the Notion property type in its metadata. `from_page` reads a page by
those names, so a live property that the model does not know is ignored and a
missing one reads as empty. `to_properties` builds the `properties` body of a
create or update call from Python names.

The subclasses are generated from `schema_snapshot.json` by
`scripts/generate_models.py` and live in `t6.notion.generated`.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, ClassVar, Self

from t6.notion import codec

JSON = dict[str, Any]

NAME_KEY = "notion_name"
TYPE_KEY = "notion_type"

_TEXT_TYPES = {"title", "rich_text"}
_STRING_TYPES = {"url", "email", "phone_number"}
_TUPLE_TYPES = {"multi_select", "relation", "people"}
_DECODERS = {
    "title": codec.title,
    "rich_text": codec.rich_text,
    "select": codec.select,
    "status": codec.status,
    "multi_select": codec.multi_select,
    "relation": codec.relation,
    "people": codec.people,
    "checkbox": codec.checkbox,
    "number": codec.number,
    "date": codec.date_start,
    "url": codec.url,
    "email": codec.email,
    "phone_number": codec.phone_number,
    "created_time": codec.timestamp,
    "last_edited_time": codec.timestamp,
}
READ_ONLY_TYPES = frozenset(
    {
        "created_time",
        "last_edited_time",
        "last_edited_by",
        "formula",
        "rollup",
        "unique_id",
        "place",
    }
)
"""Notion computes these. A model reads them and never writes them."""


def notion(name: str, notion_type: str, *, default: Any = None) -> Any:
    """Declare a model field that maps to the Notion property `name`."""
    return field(default=default, metadata={NAME_KEY: name, TYPE_KEY: notion_type})


def decode(notion_type: str, prop: JSON | None) -> Any:
    """Decode one property object. Types without a decoder come back as raw JSON."""
    return _DECODERS.get(notion_type, codec.raw)(prop)


def _text(value: str | None) -> list[JSON]:
    return [{"type": "text", "text": {"content": value}}] if value else []


def _named(value: str | None) -> JSON | None:
    return {"name": value} if value else None


def encode(notion_type: str, value: Any) -> JSON:
    """Build one property value for a create or update body.

    Select and multi-select values are sent by name. Notion adds an option it
    does not know to the property, so writing a new value needs no schema step.
    """
    if notion_type in READ_ONLY_TYPES:
        raise ValueError(f"{notion_type} properties are read-only")
    if notion_type in _TEXT_TYPES:
        return {notion_type: _text(value)}
    if notion_type in ("select", "status"):
        return {notion_type: _named(value)}
    if notion_type == "multi_select":
        return {notion_type: [{"name": name} for name in value or ()]}
    if notion_type in ("relation", "people"):
        return {notion_type: [{"id": item} for item in value or ()]}
    if notion_type == "checkbox":
        return {notion_type: bool(value)}
    if notion_type == "date":
        return {notion_type: _date(value)}
    if notion_type in _STRING_TYPES or notion_type == "number":
        return {notion_type: value}
    raise ValueError(f"cannot write a {notion_type} property")


def _date(value: date | datetime | None) -> JSON | None:
    return {"start": value.isoformat()} if value is not None else None


@dataclass(frozen=True, slots=True, kw_only=True)
class NotionModel:
    """One page of one Notion database, read by exact property name."""

    page_id: str
    DATABASE: ClassVar[str]
    """The snapshot key of the database this model describes."""

    @classmethod
    def notion_fields(cls) -> dict[str, dataclasses.Field[Any]]:
        """Python attribute name to dataclass field, for the Notion-mapped fields."""
        return {f.name: f for f in dataclasses.fields(cls) if NAME_KEY in f.metadata}

    @classmethod
    def notion_name(cls, attribute: str) -> str:
        """The exact Notion property name behind a Python attribute."""
        return str(cls.notion_fields()[attribute].metadata[NAME_KEY])

    @classmethod
    def from_page(cls, page: JSON) -> Self:
        """Build a model from a page. Unknown live properties are ignored."""
        props: JSON = page.get("properties") or {}
        values = {
            attribute: decode(str(f.metadata[TYPE_KEY]), props.get(str(f.metadata[NAME_KEY])))
            for attribute, f in cls.notion_fields().items()
        }
        return cls(page_id=str(page["id"]), **values)

    @classmethod
    def to_properties(cls, **values: Any) -> JSON:
        """The `properties` body for the given attributes, under exact Notion names.

        A value of None clears the property. An attribute that is unknown or
        read-only raises TypeError.
        """
        fields = cls.notion_fields()
        body: JSON = {}
        for attribute, value in values.items():
            if attribute not in fields:
                raise TypeError(f"{cls.__name__} has no property {attribute!r}")
            f = fields[attribute]
            notion_type = str(f.metadata[TYPE_KEY])
            if notion_type in READ_ONLY_TYPES:
                raise TypeError(f"{cls.__name__}.{attribute} is a read-only {notion_type}")
            body[str(f.metadata[NAME_KEY])] = encode(notion_type, value)
        return body
