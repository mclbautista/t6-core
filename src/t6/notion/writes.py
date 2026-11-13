"""Typed Notion writes: create or update a page, read it back, check it.

`create_row` and `update_row` write by property ID through a generated model,
read the page back and compare what Notion holds with what was asked. They
return the saved row (or what `to_domain` makes of it), or one of two typed
failures, so a caller knows whether to retry:

- `Rejected`: Notion refused the write (a 4xx, including a 429). Nothing was
  written.
- `Ambiguous`: the write may or may not have happened: a timeout or network
  error after sending, a 5xx, a readback that failed, or a readback that does
  not match what was written. The caller decides what to do, using its own
  idempotency key: repeating a create can make a duplicate page.

The functions never retry and keep no state. Apps keep idempotency keys and
retry decisions in their own ledgers. Dates compare by instant to the minute,
because Notion drops seconds; a date with a time needs a timezone to match a
readback that has one.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Literal, TypeVar, overload

from t6.notion.errors import NotionError
from t6.notion.model import ID_KEY, NAME_KEY, TYPE_KEY, NotionModel, OptionId
from t6.notion.sync import NotionClient

JSON = dict[str, Any]
M = TypeVar("M", bound=NotionModel)
T = TypeVar("T")

__all__ = ["Ambiguous", "Rejected", "create_row", "update_row"]


@dataclass(frozen=True, slots=True)
class Rejected:
    """Notion refused the write. Nothing was written."""

    status: int | None
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class Ambiguous:
    """The write may have happened. `page_id` is None when a create got no answer."""

    reason: Literal["timeout", "network_error", "server_error", "readback_failed", "mismatch"]
    detail: str
    page_id: str | None = None
    mismatched: tuple[str, ...] = ()
    """The attributes whose saved value differs from what was written (for `mismatch`)."""


@overload
def create_row(client: NotionClient, model: type[M], data_source_id: str, values: JSON) -> M | Rejected | Ambiguous: ...
@overload
def create_row(
    client: NotionClient, model: type[M], data_source_id: str, values: JSON, *, to_domain: Callable[[M], T]
) -> T | Rejected | Ambiguous: ...
def create_row(
    client: NotionClient,
    model: type[M],
    data_source_id: str,
    values: JSON,
    *,
    to_domain: Callable[[M], Any] | None = None,
) -> Any:
    """Create a page in `data_source_id` from `values` (Python attribute names) and check it."""
    body = {"parent": {"data_source_id": data_source_id}, "properties": model.to_properties(**values)}
    return _write(client, model, values, lambda: client.create_page(body), None, to_domain)


@overload
def update_row(client: NotionClient, model: type[M], page_id: str, values: JSON) -> M | Rejected | Ambiguous: ...
@overload
def update_row(
    client: NotionClient, model: type[M], page_id: str, values: JSON, *, to_domain: Callable[[M], T]
) -> T | Rejected | Ambiguous: ...
def update_row(
    client: NotionClient,
    model: type[M],
    page_id: str,
    values: JSON,
    *,
    to_domain: Callable[[M], Any] | None = None,
) -> Any:
    """Update the page `page_id` with `values` (Python attribute names) and check it."""
    body = {"properties": model.to_properties(**values)}
    return _write(client, model, values, lambda: client.update_page(page_id, body), page_id, to_domain)


def _write(
    client: NotionClient,
    model: type[M],
    values: JSON,
    send: Callable[[], JSON],
    page_id: str | None,
    to_domain: Callable[[M], Any] | None,
) -> Any:
    try:
        page_id = str(send()["id"])
    except NotionError as error:
        return _failure(error, page_id)
    try:
        page = client.retrieve_page(page_id)
    except NotionError as error:
        return Ambiguous("readback_failed", str(error), page_id)
    mismatched = tuple(
        attribute for attribute, value in values.items() if not _saved(model, attribute, value, page)
    )
    if mismatched:
        return Ambiguous("mismatch", f"saved values differ for: {', '.join(mismatched)}", page_id, mismatched)
    row = model.from_page(page)
    return to_domain(row) if to_domain else row


def _failure(error: NotionError, page_id: str | None) -> Rejected | Ambiguous:
    if error.code == "timeout":
        return Ambiguous("timeout", str(error), page_id)
    if error.code == "network_error":
        return Ambiguous("network_error", str(error), page_id)
    if error.status is not None and error.status >= 500:
        return Ambiguous("server_error", str(error), page_id)
    return Rejected(error.status, error.code, error.message)


def _saved(model: type[NotionModel], attribute: str, value: Any, page: JSON) -> bool:
    f = model.notion_fields()[attribute]
    kind = str(f.metadata[TYPE_KEY])
    props: JSON = page.get("properties") or {}
    by_id = {str(p["id"]): p for p in props.values() if isinstance(p, dict) and "id" in p}
    prop = by_id.get(str(f.metadata.get(ID_KEY))) or props.get(str(f.metadata[NAME_KEY]))
    held = (prop or {}).get(kind) if (prop or {}).get("type") == kind else None
    if kind in ("select", "status"):
        return _same_options([held] if held else [], [value] if value else [])
    if kind == "multi_select":
        return _same_options(held or [], list(value or ()))
    return bool(_held(kind, held) == _asked(kind, value))


def _same_options(held: list[JSON], asked: list[Any]) -> bool:
    """Every asked option, by ID if it is an `OptionId` and by name otherwise, is held, and no other."""

    def found(option: Any) -> bool:
        key = "id" if isinstance(option, OptionId) else "name"
        return any(str(h.get(key)) == str(option) for h in held)

    return len(held) == len(asked) and all(found(option) for option in asked)


def _id(page_id: str) -> str:
    return page_id.replace("-", "").lower()


def _instant(value: date | datetime | str | None) -> tuple[str, Any] | None:
    if isinstance(value, str):
        value = datetime.fromisoformat(value) if "T" in value else date.fromisoformat(value)
    if value is None:
        return None
    if isinstance(value, datetime):
        value = value.astimezone(UTC) if value.tzinfo else value
        return ("instant", value.replace(second=0, microsecond=0))
    return ("day", value)


def _asked(kind: str, value: Any) -> Any:
    """What a write meant to save, in a form `_held` can produce."""
    if kind in ("relation", "people"):
        return frozenset(_id(str(item)) for item in value or ())
    if kind == "date":
        return _instant(value)
    if kind == "checkbox":
        return bool(value)
    if kind in ("title", "rich_text", "url", "email", "phone_number"):
        return value or None
    return value


def _held(kind: str, raw: Any) -> Any:
    """What the page holds for a property value, in the form `_asked` produces."""
    if kind in ("title", "rich_text"):
        return "".join(str(part.get("plain_text", "")) for part in raw or []) or None
    if kind in ("relation", "people"):
        return frozenset(_id(str(item["id"])) for item in raw or ())
    if kind == "date":
        return _instant(raw.get("start")) if raw else None
    if kind == "checkbox":
        return bool(raw)
    if kind in ("url", "email", "phone_number"):
        return raw or None
    return raw
