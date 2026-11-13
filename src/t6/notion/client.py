"""Generic async Notion client over httpx, pinned to one Notion-Version.

The client knows endpoints, pagination, errors and retry. It knows nothing
about T6 databases; that lives in `t6.notion.models`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any

import httpx

from t6.notion.errors import NotionError

NOTION_VERSION = "2026-03-11"
"""The Notion API version every client sends unless told otherwise."""

BASE_URL = "https://api.notion.com/v1"

BLOCKS_PER_APPEND = 100
"""The most blocks Notion accepts in one append request."""

JSON = dict[str, Any]


@dataclass(frozen=True, kw_only=True)
class RetryPolicy:
    """Opt-in retry. See the README for the policy in full.

    A 429 retries on any call. A 5xx or a timeout retries on reads only.
    Writes (create and update page) raise on a 5xx or a timeout, because a
    repeated create can make a duplicate page.
    """

    attempts: int = 3
    """Total tries, the first one included."""
    base_delay: float = 0.5
    """Wait before the first retry when Notion sends no Retry-After."""
    max_delay: float = 8.0
    """Cap on the doubling backoff. A Retry-After from Notion is not capped."""
    sleep: Callable[[float], Awaitable[None]] = field(default=asyncio.sleep, compare=False)

    def __post_init__(self) -> None:
        if self.attempts < 1:
            raise ValueError("attempts must be 1 or more")

    def delay(self, retry: int, retry_after: float | None) -> float:
        """Seconds to wait before retry number `retry` (1 for the first)."""
        if retry_after is not None:
            return retry_after
        return float(min(self.max_delay, self.base_delay * 2 ** (retry - 1)))


class AsyncNotionClient:
    """Async client for pages and data sources."""

    def __init__(
        self,
        token: str,
        *,
        notion_version: str = NOTION_VERSION,
        retry: RetryPolicy | None = None,
        base_url: str = BASE_URL,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.notion_version = notion_version
        self.retry = retry
        self._http = httpx.AsyncClient(
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Notion-Version": notion_version,
                "Content-Type": "application/json",
            },
            timeout=timeout,
            transport=transport,
        )

    async def __aenter__(self) -> AsyncNotionClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    # Pages

    async def retrieve_page(self, page_id: str) -> JSON:
        return await self._call("GET", f"/pages/{page_id}", read=True)

    async def create_page(self, body: JSON) -> JSON:
        """Create a page. A write: never retried on a 5xx or a timeout."""
        return await self._call("POST", "/pages", body, read=False)

    async def update_page(self, page_id: str, body: JSON) -> JSON:
        """Update a page. A write: never retried on a 5xx or a timeout."""
        return await self._call("PATCH", f"/pages/{page_id}", body, read=False)

    # Blocks

    async def iter_block_children(self, block_id: str, *, page_size: int = 100) -> AsyncIterator[JSON]:
        """Yield every child block of a page or block, following `next_cursor` page by page.

        Only direct children are returned; a child with `has_children` set is listed by calling this again with its ID.
        """
        cursor: str | None = None
        while True:
            query = f"?page_size={page_size}" + (f"&start_cursor={cursor}" if cursor else "")
            data = await self._call("GET", f"/blocks/{block_id}/children{query}", read=True)
            for block in data.get("results", []):
                yield block
            cursor = data.get("next_cursor")
            if not data.get("has_more") or not cursor:
                return

    async def list_block_children(self, block_id: str, *, page_size: int = 100) -> list[JSON]:
        """Every child block of a page or block, across all result pages."""
        return [block async for block in self.iter_block_children(block_id, page_size=page_size)]

    async def append_block_children(self, block_id: str, children: list[JSON]) -> list[JSON]:
        """Append blocks after the last child of a page or block and return the created blocks.

        A write: never retried on a 5xx or a timeout. Notion takes at most 100 blocks per request, so a longer
        list is sent as several requests in order. If a later request fails, the earlier ones have already
        been saved and the error is raised; read the children back to see how far it got.
        """
        created: list[JSON] = []
        for start in range(0, len(children), BLOCKS_PER_APPEND):
            data = await self._call(
                "PATCH",
                f"/blocks/{block_id}/children",
                {"children": children[start : start + BLOCKS_PER_APPEND]},
                read=False,
            )
            created.extend(data.get("results", []))
        return created

    # Data sources

    async def retrieve_data_source(self, data_source_id: str) -> JSON:
        return await self._call("GET", f"/data_sources/{data_source_id}", read=True)

    async def iter_data_source(
        self,
        data_source_id: str,
        *,
        filter: JSON | None = None,
        sorts: list[JSON] | None = None,
        page_size: int = 100,
    ) -> AsyncIterator[JSON]:
        """Yield every row of a query, following `next_cursor` page by page."""
        body: JSON = {"page_size": page_size}
        if filter is not None:
            body["filter"] = filter
        if sorts is not None:
            body["sorts"] = sorts
        while True:
            data = await self._call(
                "POST", f"/data_sources/{data_source_id}/query", body, read=True
            )
            for row in data.get("results", []):
                yield row
            cursor = data.get("next_cursor")
            if not data.get("has_more") or not cursor:
                return
            body = {**body, "start_cursor": cursor}

    async def query_data_source(
        self,
        data_source_id: str,
        *,
        filter: JSON | None = None,
        sorts: list[JSON] | None = None,
        page_size: int = 100,
    ) -> list[JSON]:
        """Every row of a query, across all result pages."""
        return [
            row
            async for row in self.iter_data_source(
                data_source_id, filter=filter, sorts=sorts, page_size=page_size
            )
        ]

    # Transport

    async def _call(self, method: str, path: str, body: JSON | None = None, *, read: bool) -> JSON:
        retry = 0
        while True:
            try:
                return await self._send(method, path, body)
            except NotionError as error:
                retry += 1
                if not self._should_retry(error, read=read, retry=retry):
                    raise
                assert self.retry is not None
                await self.retry.sleep(self.retry.delay(retry, error.retry_after))

    def _should_retry(self, error: NotionError, *, read: bool, retry: int) -> bool:
        if self.retry is None or retry >= self.retry.attempts:
            return False
        if error.status == 429:
            return True
        transient = error.code == "timeout" or (error.status is not None and error.status >= 500)
        return read and transient

    async def _send(self, method: str, path: str, body: JSON | None) -> JSON:
        try:
            response = await self._http.request(method, path, json=body)
        except httpx.TimeoutException as error:
            raise NotionError(None, "timeout", str(error) or type(error).__name__) from error
        except httpx.TransportError as error:
            raise NotionError(None, "network_error", str(error) or type(error).__name__) from error
        if response.status_code >= 400:
            try:
                data = response.json()
            except ValueError:
                data = {}
            if not isinstance(data, dict):
                data = {}
            raise NotionError(
                response.status_code,
                str(data.get("code", "unknown")),
                str(data.get("message", response.text)),
                retry_after=_retry_after(response.headers.get("Retry-After")),
            )
        result = response.json()
        if not isinstance(result, dict):
            raise NotionError(response.status_code, "invalid_json", "expected a JSON object")
        return result


def _retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None
