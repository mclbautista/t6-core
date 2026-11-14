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
multi-select value that is not yet an option is added as an option. A status
value that is not an option fails with 400, because Notion never creates a
status option on a write. Write
bodies need no `type` keys; the fake adds them so pages read back like
Notion's. `add_snapshot_data_source` builds such a data source from the
committed schema snapshot.

Properties and options have stable IDs, as in Notion. A write may key a
property by name or by ID and set an option by `{"id": ...}` or by name, and
pages read back with each property's `id`. `rename_property` and
`rename_option` change names and keep IDs, as a rename in Notion does.
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
from t6.notion import named
from t6.notion.external_links import ExternalLinksProperties as xl
from t6.notion.models import ProjectsProperties as p
from t6.notion.schema import load_snapshot
from t6.notion.sync import NotionClient

JSON = dict[str, Any]

_PAGE = re.compile(r"^/v1/pages/(?P<id>[^/]+)$")
_DATA_SOURCE = re.compile(r"^/v1/data_sources/(?P<id>[^/]+)$")
_CHILDREN = re.compile(r"^/v1/blocks/(?P<id>[^/]+)/children$")
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
        self._children: dict[str, list[JSON]] = {}
        self._failures: list[_Failure] = []
        self.requests: list[httpx.Request] = []
        self.transport = httpx.MockTransport(self._handle)

    # Seeding and inspection

    def add_data_source(self, data_source_id: str, properties: JSON | None = None) -> JSON:
        source = {"object": "data_source", "id": data_source_id, "properties": properties or {}}
        self._data_sources[data_source_id] = source
        return copy.deepcopy(source)

    def add_snapshot_data_source(self, data_source_id: str, database: str) -> JSON:
        """Add a data source whose schema is a snapshot database.

        Its only options are the snapshot's named values, under their recorded
        option IDs and labelled with their domain values.
        """
        return self.add_data_source(data_source_id, snapshot_properties(database))

    def add_property(self, data_source_id: str, name: str, notion_type: str) -> None:
        """Add a property to a data source, as if someone edited it in Notion."""
        self._data_sources[data_source_id]["properties"][name] = _property(name, notion_type)

    def rename_property(self, data_source_id: str, name: str, new_name: str) -> None:
        """Rename a property in the schema and on every page. Its ID stays."""
        schema = self._data_sources[data_source_id]["properties"]
        schema[new_name] = {**schema.pop(name), "name": new_name}
        for page in self._pages.values():
            if page["parent"]["data_source_id"] == data_source_id and name in page["properties"]:
                page["properties"][new_name] = page["properties"].pop(name)

    def add_option(self, data_source_id: str, name: str, option: str) -> str:
        """Add an option to a select, multi-select or status property and return its ID."""
        prop = self._data_sources[data_source_id]["properties"][name]
        options = prop[prop["type"]]["options"]
        existing = next((o for o in options if o["name"] == option), None)
        if existing is not None:
            return str(existing["id"])
        option_id = uuid.uuid4().hex[:8]
        options.append({"id": option_id, "name": option})
        return option_id

    def rename_option(self, data_source_id: str, name: str, option: str, new_option: str) -> None:
        """Rename a select, multi-select or status option, on every page too. Its ID stays."""
        prop = self._data_sources[data_source_id]["properties"][name]
        options = prop[prop["type"]]["options"]
        by_name = {existing["name"]: existing for existing in options}
        by_name[option]["name"] = new_option
        for page in self._pages.values():
            if page["parent"]["data_source_id"] != data_source_id:
                continue
            chosen = (page["properties"].get(name) or {}).get(prop["type"])
            for value in chosen if isinstance(chosen, list) else [chosen]:
                if value and value.get("name") == option:
                    value["name"] = new_option

    def remove_property(self, data_source_id: str, name: str) -> None:
        del self._data_sources[data_source_id]["properties"][name]

    def change_property_type(self, data_source_id: str, name: str, notion_type: str) -> None:
        """Change a property's type, as Notion does: its ID stays."""
        properties = self._data_sources[data_source_id]["properties"]
        properties[name] = _property(name, notion_type, properties[name]["id"])

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
            "properties": self._with_ids(data_source_id, copy.deepcopy(properties)),
        }
        self._pages[page_id] = page
        return copy.deepcopy(page)

    def _with_ids(self, data_source_id: str, properties: JSON) -> JSON:
        schema: JSON = self._data_sources[data_source_id]["properties"]
        for name, prop in properties.items():
            if name in schema and "id" not in prop:
                prop["id"] = schema[name]["id"]
        return properties

    def page(self, page_id: str) -> JSON:
        return copy.deepcopy(self._pages[self._page_key(page_id)])

    def _page_key(self, page_id: str) -> str:
        if page_id in self._pages:
            return page_id
        plain = _plain_id(page_id)
        if not re.fullmatch(r"[0-9a-f]{32}", plain):
            return page_id
        return next((key for key in self._pages if _plain_id(key) == plain), page_id)

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
            return self._get(self._pages, self._page_key(match["id"]))
        if request.method == "PATCH" and (match := _PAGE.match(path)):
            return self._update_page(self._page_key(match["id"]), body)
        if request.method == "POST" and path == "/v1/pages":
            return self._create_page(body)
        if request.method == "GET" and (match := _CHILDREN.match(path)):
            return self._list_children(self._page_key(match["id"]), request.url.params)
        if request.method == "PATCH" and (match := _CHILDREN.match(path)):
            return self._append_children(self._page_key(match["id"]), body)
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

    def block_children(self, block_id: str) -> list[JSON]:
        """The child blocks of a page or block, in order, as stored."""
        return self._children.get(block_id, [])

    def _is_block_parent(self, block_id: str) -> bool:
        return block_id in self._pages or any(
            block["id"] == block_id for blocks in self._children.values() for block in blocks
        )

    def _append_children(self, parent_id: str, body: JSON) -> httpx.Response:
        if not self._is_block_parent(parent_id):
            return _error(404, "object_not_found", f"Could not find block {parent_id}.")
        children = body.get("children")
        if not isinstance(children, list) or not 1 <= len(children) <= 100:
            return _error(400, "validation_error", "children must hold 1 to 100 blocks.")
        created = [self._store_block(parent_id, child) for child in children]
        return httpx.Response(200, json={"object": "list", "results": created, "has_more": False, "next_cursor": None})

    def _store_block(self, parent_id: str, block: JSON) -> JSON:
        nested = (block.get(block.get("type", "")) or {}).get("children", [])
        stored = {
            **{key: copy.deepcopy(value) for key, value in block.items()},
            "object": "block",
            "id": str(uuid.uuid4()),
            "parent": {"type": "page_id" if parent_id in self._pages else "block_id", **{
                "page_id" if parent_id in self._pages else "block_id": parent_id}},
            "has_children": bool(nested),
        }
        for part in (stored.get(stored.get("type", "")) or {}).get("rich_text") or []:
            if "plain_text" not in part and "text" in part:
                part["plain_text"] = part["text"]["content"]  # as Notion returns it
        if nested:
            del stored[stored["type"]]["children"]
        self._children.setdefault(parent_id, []).append(stored)
        for child in nested:
            self._store_block(stored["id"], child)
        return copy.deepcopy(stored)

    def _list_children(self, parent_id: str, params: httpx.QueryParams) -> httpx.Response:
        if not self._is_block_parent(parent_id):
            return _error(404, "object_not_found", f"Could not find block {parent_id}.")
        blocks = self._children.get(parent_id, [])
        size = max(1, min(100, int(params.get("page_size", 100))))
        start = int(params.get("start_cursor") or 0)
        more = start + size < len(blocks)
        return httpx.Response(
            200,
            json={
                "object": "list",
                "results": copy.deepcopy(blocks[start : start + size]),
                "has_more": more,
                "next_cursor": str(start + size) if more else None,
            },
        )

    def _prepare(self, data_source_id: str, properties: JSON) -> JSON | httpx.Response:
        """Validate and complete a write body; add select options it introduces."""
        schema: JSON = self._data_sources[data_source_id]["properties"]
        by_id = {str(prop["id"]): name for name, prop in schema.items()}
        prepared: JSON = {}
        for key, value in properties.items():
            name = key if key in schema else by_id.get(key, key)
            if schema and name not in schema:
                return _error(
                    400, "validation_error", f"{key} is not a property that exists."
                )
            value = _as_read({name: copy.deepcopy(value)})[name]
            kind = value.get("type")
            if kind is None:
                return _error(400, "validation_error", f"{name} has no value.")
            if schema and kind in ("select", "multi_select", "status"):
                options = schema[name][kind]["options"]
                chosen = value.get(kind)
                resolved = []
                for option in chosen if isinstance(chosen, list) else [chosen]:
                    if option:
                        known = next(
                            (o for o in options if o["id"] == option.get("id") or o["name"] == option.get("name")),
                            None,
                        )
                        if known is None and "id" in option:
                            return _error(400, "validation_error", f"{name} has no option {option['id']}.")
                        if known is None and kind == "status":
                            return _error(400, "validation_error", f"{name} has no status option {option['name']!r}; Notion never creates one on a write.")
                        if known is None:
                            known = {"id": uuid.uuid4().hex[:8], "name": option["name"]}
                            options.append(known)
                        resolved.append({"id": known["id"], "name": known["name"]})
                value[kind] = resolved if isinstance(chosen, list) else (resolved[0] if resolved else None)
            if schema:
                value["id"] = schema[name]["id"]
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
            and _matches(page, _by_name(self._data_sources[data_source_id], body.get("filter")))
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


def _by_name(source: JSON, query_filter: JSON | None) -> JSON | None:
    """A copy of the filter with property IDs turned into names, as the fake stores pages by name."""
    if not query_filter:
        return query_filter
    if "and" in query_filter:
        return {**query_filter, "and": [_by_name(source, part) for part in query_filter["and"]]}
    ids = {str(prop["id"]): name for name, prop in source["properties"].items() if "id" in prop}
    prop_key = query_filter.get("property")
    return {**query_filter, "property": ids.get(str(prop_key), prop_key)} if prop_key else query_filter


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
        wanted_id = _plain_id(query_filter["relation"]["contains"])  # Notion takes an ID with or without dashes
        return any(_plain_id(item.get("id")) == wanted_id for item in prop.get("relation") or [])
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


def _plain_id(page_id: object) -> str:
    return str(page_id).replace("-", "").lower()


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


def _property(name: str, notion_type: str, property_id: str | None = None) -> JSON:
    prop: JSON = {"id": property_id or uuid.uuid4().hex[:4], "name": name, "type": notion_type}
    if notion_type in ("select", "multi_select", "status"):
        prop[notion_type] = {"options": []}
    else:
        prop[notion_type] = {}
    return prop


def snapshot_properties(database: str) -> JSON:
    """Live-shaped data source properties for one database of the schema snapshot.

    The snapshot holds no option names, so the only options are its named
    values, each under its recorded option ID and labelled with its domain value.
    """
    snapshot = load_snapshot()["databases"][database]
    properties = {
        prop["name"]: _property(prop["name"], prop["type"], prop.get("id"))
        for prop in snapshot["properties"]
    }
    for attribute, enum in named.NAMED_FIELDS.get(database, {}).items():
        block = (snapshot.get("named_options") or {}).get(attribute) or {}
        prop = named.find_live(properties, str(block.get("property", "")))
        if prop is None:
            continue
        options = properties[prop["name"]][prop["type"]]["options"]
        for member in enum:
            option_id = (block.get("values") or {}).get(member.name)
            if option_id:
                options.append({"id": option_id, "name": member.value})
    return properties


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
