"""An in-memory fake Notion for tests.

FakeNotion serves the endpoints the client uses through an httpx MockTransport,
so tests run the real client code with no network:

    fake = FakeNotion()
    fake.add_data_source("ds-projects")
    page = fake.add_page("ds-projects", project_properties(name="Example Show"))
    with fake.client() as client:
        client.retrieve_page(page["id"])

`fail_next` and `time_out_next` inject errors for the next requests, and
`requests` records every request the fake received.

A data source added with properties works like Notion's: a write that names a
property the data source does not have fails with 400, and a select or
multi-select value that is not yet an option is added as an option. Write
bodies need no `type` keys; the fake adds them so pages read back like
Notion's. `add_snapshot_data_source` builds such a data source from the
committed schema snapshot.
"""

from __future__ import annotations

import copy
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from t6.notion.client import NOTION_VERSION, AsyncNotionClient, RetryPolicy
from t6.notion.external_links import ExternalLinksProperties as xl
from t6.notion.models import ProjectsProperties as p
from t6.notion.schema import load_snapshot
from t6.notion.sync import NotionClient

JSON = dict[str, Any]

_PAGE = re.compile(r"^/v1/pages/(?P<id>[^/]+)$")
_DATA_SOURCE = re.compile(r"^/v1/data_sources/(?P<id>[^/]+)$")
_QUERY = re.compile(r"^/v1/data_sources/(?P<id>[^/]+)/query$")


@dataclass
class _Failure:
    status: int | None
    code: str
    retry_after: float | None
    after: int = 0


class FakeNotion:
    """In-memory pages and data sources behind a mock HTTP transport."""

    def __init__(self) -> None:
        self._pages: dict[str, JSON] = {}
        self._data_sources: dict[str, JSON] = {}
        self._failures: list[_Failure] = []
        self.requests: list[httpx.Request] = []
        self.transport = httpx.MockTransport(self._handle)

    # Seeding and inspection

    def add_data_source(self, data_source_id: str, properties: JSON | None = None) -> JSON:
        source = {"object": "data_source", "id": data_source_id, "properties": properties or {}}
        self._data_sources[data_source_id] = source
        return copy.deepcopy(source)

    def add_snapshot_data_source(self, data_source_id: str, database: str) -> JSON:
        """Add a data source whose schema is a snapshot database, with no options yet."""
        return self.add_data_source(data_source_id, snapshot_properties(database))

    def add_property(self, data_source_id: str, name: str, notion_type: str) -> None:
        """Add a property to a data source, as if someone edited it in Notion."""
        self._data_sources[data_source_id]["properties"][name] = _property(name, notion_type)

    def remove_property(self, data_source_id: str, name: str) -> None:
        del self._data_sources[data_source_id]["properties"][name]

    def change_property_type(self, data_source_id: str, name: str, notion_type: str) -> None:
        self.add_property(data_source_id, name, notion_type)

    def select_options(self, data_source_id: str, name: str) -> list[str]:
        """The option names of a select or multi-select property."""
        prop = self._data_sources[data_source_id]["properties"][name]
        return [str(option["name"]) for option in prop[prop["type"]]["options"]]

    def add_page(self, data_source_id: str, properties: JSON, page_id: str | None = None) -> JSON:
        if data_source_id not in self._data_sources:
            raise KeyError(f"unknown data source {data_source_id!r}; call add_data_source first")
        page_id = page_id or str(uuid.uuid4())
        page = {
            "object": "page",
            "id": page_id,
            "parent": {"type": "data_source_id", "data_source_id": data_source_id},
            "url": f"https://www.notion.so/{page_id.replace('-', '')}",
            "archived": False,
            "in_trash": False,
            "properties": copy.deepcopy(properties),
        }
        self._pages[page_id] = page
        return copy.deepcopy(page)

    def page(self, page_id: str) -> JSON:
        return copy.deepcopy(self._pages[page_id])

    def page_count(self) -> int:
        return len(self._pages)

    # Error injection

    def fail_next(
        self,
        status: int,
        *,
        code: str = "error",
        times: int = 1,
        retry_after: float | None = None,
        after: int = 0,
    ) -> None:
        """Answer the next `times` requests with this HTTP error.

        `after` lets that many requests through first, so a test can fail the
        second call of a flow (for example the create after a lookup).
        """
        failures = [_Failure(status, code, retry_after) for _ in range(times)]
        if failures:
            failures[0].after = after
        self._failures.extend(failures)

    def time_out_next(self, *, times: int = 1) -> None:
        """Make the next `times` requests time out before Notion answers."""
        self._failures.extend(_Failure(None, "timeout", None) for _ in range(times))

    # Clients wired to this fake

    def async_client(
        self, *, notion_version: str = NOTION_VERSION, retry: RetryPolicy | None = None
    ) -> AsyncNotionClient:
        return AsyncNotionClient(
            "fake-token", notion_version=notion_version, retry=retry, transport=self.transport
        )

    def client(
        self, *, notion_version: str = NOTION_VERSION, retry: RetryPolicy | None = None
    ) -> NotionClient:
        return NotionClient(
            "fake-token", notion_version=notion_version, retry=retry, transport=self.transport
        )

    # HTTP

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._failures and self._failures[0].after > 0:
            self._failures[0].after -= 1
        elif self._failures:
            failure = self._failures.pop(0)
            if failure.status is None:
                raise httpx.ReadTimeout("fake timeout", request=request)
            headers = {}
            if failure.retry_after is not None:
                headers["Retry-After"] = f"{failure.retry_after:g}"
            return _error(failure.status, failure.code, "injected failure", headers)

        path = request.url.path
        body: JSON = json.loads(request.content) if request.content else {}
        if request.method == "GET" and (match := _PAGE.match(path)):
            return self._get(self._pages, match["id"])
        if request.method == "PATCH" and (match := _PAGE.match(path)):
            return self._update_page(match["id"], body)
        if request.method == "POST" and path == "/v1/pages":
            return self._create_page(body)
        if request.method == "GET" and (match := _DATA_SOURCE.match(path)):
            return self._get(self._data_sources, match["id"])
        if request.method == "POST" and (match := _QUERY.match(path)):
            return self._query(match["id"], body)
        return _error(400, "invalid_request_url", f"fake has no route for {request.method} {path}")

    @staticmethod
    def _get(store: dict[str, JSON], object_id: str) -> httpx.Response:
        if object_id not in store:
            return _error(404, "object_not_found", f"Could not find object {object_id}.")
        return httpx.Response(200, json=store[object_id])

    def _create_page(self, body: JSON) -> httpx.Response:
        parent = body.get("parent") or {}
        data_source_id = parent.get("data_source_id")
        if data_source_id not in self._data_sources:
            return _error(404, "object_not_found", f"Could not find data source {data_source_id}.")
        properties = self._prepare(data_source_id, body.get("properties") or {})
        if isinstance(properties, httpx.Response):
            return properties
        page = self.add_page(data_source_id, properties)
        return httpx.Response(200, json=page)

    def _update_page(self, page_id: str, body: JSON) -> httpx.Response:
        if page_id not in self._pages:
            return _error(404, "object_not_found", f"Could not find page {page_id}.")
        page = self._pages[page_id]
        properties = self._prepare(page["parent"]["data_source_id"], body.get("properties") or {})
        if isinstance(properties, httpx.Response):
            return properties
        page["properties"].update(properties)
        for flag in ("archived", "in_trash"):
            if flag in body:
                page[flag] = bool(body[flag])
        return httpx.Response(200, json=page)

    def _prepare(self, data_source_id: str, properties: JSON) -> JSON | httpx.Response:
        """Validate and complete a write body; add select options it introduces."""
        schema: JSON = self._data_sources[data_source_id]["properties"]
        prepared: JSON = {}
        for name, value in properties.items():
            if schema and name not in schema:
                return _error(
                    400, "validation_error", f"{name} is not a property that exists."
                )
            value = _as_read({name: copy.deepcopy(value)})[name]
            kind = value.get("type")
            if kind is None:
                return _error(400, "validation_error", f"{name} has no value.")
            if schema and kind in ("select", "multi_select"):
                options = schema[name][kind]["options"]
                chosen = value.get(kind)
                for option in chosen if isinstance(chosen, list) else [chosen]:
                    known = {existing["name"] for existing in options}
                    if option and option["name"] not in known:
                        options.append({"id": uuid.uuid4().hex[:8], "name": option["name"]})
            prepared[name] = value
        return prepared

    def _query(self, data_source_id: str, body: JSON) -> httpx.Response:
        if data_source_id not in self._data_sources:
            return _error(404, "object_not_found", f"Could not find data source {data_source_id}.")
        rows = [
            page
            for page in self._pages.values()
            if page["parent"]["data_source_id"] == data_source_id
            and not page["in_trash"]
            and _matches(page, body.get("filter"))
        ]
        size = max(1, min(100, int(body.get("page_size", 100))))
        start = int(body.get("start_cursor") or 0)
        chunk = rows[start : start + size]
        more = start + size < len(rows)
        return httpx.Response(
            200,
            json={
                "object": "list",
                "results": chunk,
                "has_more": more,
                "next_cursor": str(start + size) if more else None,
            },
        )


def _matches(page: JSON, query_filter: JSON | None) -> bool:
    """Support `and` of single-property filters: `equals` on text, select, number and
    checkbox, and `contains` on relation. Anything else matches every row."""
    if not query_filter:
        return True
    if "and" in query_filter:
        return all(_matches(page, part) for part in query_filter["and"])
    if "property" not in query_filter:
        return True
    prop = page["properties"].get(query_filter["property"]) or {}
    if "relation" in query_filter and "contains" in query_filter["relation"]:
        wanted_id = query_filter["relation"]["contains"]
        return any(item.get("id") == wanted_id for item in prop.get("relation") or [])
    for kind in ("select", "status", "rich_text", "title", "number", "checkbox"):
        if kind in query_filter and "equals" in query_filter[kind]:
            wanted = query_filter[kind]["equals"]
            value = prop.get(kind)
            if kind in ("select", "status"):
                value = (value or {}).get("name")
            elif kind in ("rich_text", "title"):
                value = "".join(part.get("plain_text", "") for part in value or [])
            return bool(value == wanted)
    return True


def _as_read(properties: JSON) -> JSON:
    """Turn write-shaped properties into the typed shape Notion returns when reading.

    A write sends `{"select": {...}}`; a read returns `{"type": "select", "select": {...}}`,
    and rich text parts carry `plain_text`.
    """
    result: JSON = {}
    for name, value in properties.items():
        prop = dict(value)
        if "type" not in prop and len(prop) == 1:
            prop["type"] = next(iter(prop))
        for kind in ("title", "rich_text"):
            if prop.get("type") == kind:
                prop[kind] = [
                    {**part, "plain_text": part.get("plain_text", part["text"]["content"])}
                    for part in prop[kind]
                ]
        result[name] = prop
    return result


def _error(status: int, code: str, message: str, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(
        status,
        json={"object": "error", "status": status, "code": code, "message": message},
        headers=headers,
    )


# Schema helpers


def _property(name: str, notion_type: str) -> JSON:
    prop: JSON = {"id": uuid.uuid4().hex[:4], "name": name, "type": notion_type}
    if notion_type in ("select", "multi_select", "status"):
        prop[notion_type] = {"options": []}
    else:
        prop[notion_type] = {}
    return prop


def snapshot_properties(database: str) -> JSON:
    """Live-shaped data source properties for one database of the schema snapshot."""
    return {
        prop["name"]: _property(prop["name"], prop["type"])
        for prop in load_snapshot()["databases"][database]["properties"]
    }


# Property builders for seeding the fake


def title(text: str) -> JSON:
    return {"type": "title", "title": [_text(text)]} if text else {"type": "title", "title": []}


def rich_text(text: str | None) -> JSON:
    return {"type": "rich_text", "rich_text": [_text(text)] if text else []}


def select(name: str | None) -> JSON:
    return {"type": "select", "select": {"name": name} if name else None}


def number(value: float | int | None) -> JSON:
    return {"type": "number", "number": value}


def relation(*page_ids: str) -> JSON:
    return {"type": "relation", "relation": [{"id": page_id} for page_id in page_ids], "has_more": False}


def checkbox(value: bool) -> JSON:
    return {"type": "checkbox", "checkbox": value}


def url_value(value: str | None) -> JSON:
    return {"type": "url", "url": value}


def date(start: str | None) -> JSON:
    return {"type": "date", "date": {"start": start, "end": None} if start else None}


def _text(text: str) -> JSON:
    return {"type": "text", "text": {"content": text}, "plain_text": text}


def project_properties(
    *,
    name: str,
    project_code: str | None = None,
    project_type: str | None = None,
    eps: int | None = None,
    status: str | None = None,
    studio_id: str | None = None,
) -> JSON:
    """Properties for a Projects page, with the exact Notion property names."""
    return {
        p.NAME: title(name),
        p.PROJECT_CODE: select(project_code),
        p.PROJECT_TYPE: select(project_type),
        p.EPS: number(eps),
        p.STATUS: select(status),
        p.STUDIO: relation(*([studio_id] if studio_id else [])),
    }


def external_link_properties(
    *,
    system: str | None = None,
    kind: str | None = None,
    external_id: str | None = None,
    internal_id: str | None = None,
    url: str | None = None,
    active: bool = False,
    last_activity: str | None = None,
    projects: list[str] | None = None,
    deliverables: list[str] | None = None,
    contacts: list[str] | None = None,
    bays: list[str] | None = None,
    storage: list[str] | None = None,
    cameras: list[str] | None = None,
) -> JSON:
    """Properties for an External Links page, with the exact Notion property names."""
    return {
        xl.NAME: title(" / ".join(part for part in (system, kind, external_id) if part)),
        xl.SYSTEM: select(system),
        xl.KIND: select(kind),
        xl.EXTERNAL_ID: rich_text(external_id),
        xl.INTERNAL_ID: rich_text(internal_id),
        xl.URL: url_value(url),
        xl.ACTIVE: checkbox(active),
        xl.LAST_ACTIVITY: date(last_activity),
        xl.PROJECTS: relation(*(projects or [])),
        xl.DELIVERABLES: relation(*(deliverables or [])),
        xl.CONTACTS: relation(*(contacts or [])),
        xl.BAYS: relation(*(bays or [])),
        xl.STORAGE: relation(*(storage or [])),
        xl.CAMERAS: relation(*(cameras or [])),
    }
