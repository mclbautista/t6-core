"""Thin blocking wrapper over AsyncNotionClient, for sync apps.

Each NotionClient owns one private event loop and runs every call on it. Do not
use it from inside a running event loop; use AsyncNotionClient there.
"""

from __future__ import annotations

import asyncio
from types import TracebackType

import httpx

from t6.notion.client import BASE_URL, JSON, NOTION_VERSION, AsyncNotionClient, RetryPolicy


class NotionClient:
    """Blocking client with the same methods and results as AsyncNotionClient."""

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
        self._runner = asyncio.Runner()
        self._async = AsyncNotionClient(
            token,
            notion_version=notion_version,
            retry=retry,
            base_url=base_url,
            timeout=timeout,
            transport=transport,
        )

    @property
    def notion_version(self) -> str:
        return self._async.notion_version

    def __enter__(self) -> NotionClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        try:
            self._runner.run(self._async.aclose())
        finally:
            self._runner.close()

    def retrieve_page(self, page_id: str) -> JSON:
        return self._runner.run(self._async.retrieve_page(page_id))

    def create_page(self, body: JSON) -> JSON:
        return self._runner.run(self._async.create_page(body))

    def update_page(self, page_id: str, body: JSON) -> JSON:
        return self._runner.run(self._async.update_page(page_id, body))

    def retrieve_data_source(self, data_source_id: str) -> JSON:
        return self._runner.run(self._async.retrieve_data_source(data_source_id))

    def query_data_source(
        self,
        data_source_id: str,
        *,
        filter: JSON | None = None,
        sorts: list[JSON] | None = None,
        page_size: int = 100,
    ) -> list[JSON]:
        return self._runner.run(
            self._async.query_data_source(
                data_source_id, filter=filter, sorts=sorts, page_size=page_size
            )
        )
