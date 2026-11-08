"""Read and update Cameras rows in domain terms.

Rows go out and come in as JSON objects keyed by the generated model's Python
names (`name`, `tags`, ...), never by Notion property names. Writes go through
`CamerasRow.to_properties`, so Notion naming stays inside the generated model.
"""

from __future__ import annotations

from typing import Any

from t6.notion.generated import CamerasRow
from t6.notion.model import READ_ONLY_TYPES
from t6.notion.sync import NotionClient

JSON = dict[str, Any]


class CameraInputError(ValueError):
    """The update JSON has an unknown field or an invalid value."""


def camera_to_json(row: CamerasRow) -> JSON:
    """One row as JSON: `id` plus every Notion-mapped field by domain name."""
    out: JSON = {"id": row.page_id}
    for attribute in CamerasRow.notion_fields():
        value = getattr(row, attribute)
        out[attribute] = list(value) if isinstance(value, tuple) else value
    return out


def _writable() -> dict[str, str]:
    return {
        attribute: str(f.metadata["notion_type"])
        for attribute, f in CamerasRow.notion_fields().items()
        if f.metadata["notion_type"] not in READ_ONLY_TYPES
    }


def validate_update(payload: Any) -> JSON:
    """Check update JSON against the model. Returns the values, or raises CameraInputError."""
    if not isinstance(payload, dict):
        raise CameraInputError("update JSON must be an object of field to value")
    writable = _writable()
    unknown = sorted(set(payload) - set(writable))
    if unknown:
        raise CameraInputError(
            f"unknown fields: {', '.join(unknown)}; allowed: {', '.join(sorted(writable))}"
        )
    if not payload:
        raise CameraInputError("update JSON has no fields")
    values: JSON = {}
    for attribute, value in payload.items():
        kind = writable[attribute]
        if kind == "title":
            if value is not None and not isinstance(value, str):
                raise CameraInputError(f"{attribute} must be a string or null")
        elif kind in ("multi_select", "relation", "people"):
            if not isinstance(value, list) or not all(
                isinstance(item, str) and item.strip() for item in value
            ):
                raise CameraInputError(f"{attribute} must be a list of non-empty strings")
        else:  # pragma: no cover - guards a future Cameras property type
            raise CameraInputError(f"{attribute} ({kind}) cannot be written from the CLI")
        values[attribute] = value
    return values


def get_cameras(
    client: NotionClient, data_source_id: str, *, page_id: str | None = None
) -> list[JSON]:
    """All Cameras, or just the one page when `page_id` is given."""
    if page_id is not None:
        return [camera_to_json(CamerasRow.from_page(_camera_page(client, data_source_id, page_id)))]
    pages = client.query_data_source(data_source_id)
    return [camera_to_json(CamerasRow.from_page(page)) for page in pages]


def update_camera(
    client: NotionClient, data_source_id: str, page_id: str, payload: Any
) -> JSON:
    """Validate and write an update, then return the updated row."""
    values = validate_update(payload)
    _camera_page(client, data_source_id, page_id)
    page = client.update_page(page_id, {"properties": CamerasRow.to_properties(**values)})
    return camera_to_json(CamerasRow.from_page(page))


def _camera_page(client: NotionClient, data_source_id: str, page_id: str) -> JSON:
    page = client.retrieve_page(page_id)
    parent = page.get("parent") or {}
    if parent.get("data_source_id") != data_source_id:
        raise CameraInputError(f"page {page_id} is not in the Cameras data source")
    return page
