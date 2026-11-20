"""Reader layer over NotionClient with explicit `freshness` semantics."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from t6.notion.mirror import MirrorReader
    from t6.notion.sync import NotionClient

JSON = dict[str, Any]
Freshness = Literal["mirror", "live"]

__all__ = ["Freshness", "NotionSource", "as_source", "last_read_from_mirror"]

_last_read_source: ContextVar[str | None] = ContextVar("notion_last_read_source", default=None)


def last_read_from_mirror() -> bool:
    """Whether the most recent `NotionSource` read in this context was answered from the local mirror."""
    return _last_read_source.get() == "mirror"


def _check_freshness(freshness: Freshness) -> None:
    if freshness not in ("mirror", "live"):
        raise ValueError(f"unsupported freshness: {freshness!r}")


class NotionSource:
    """Reads Notion pages, data sources, blocks and users with explicit `freshness`.

    Wraps either an already-open `NotionClient` (called directly without closing it)
    or a zero-arg factory `Callable[[], NotionClient]` (opened and closed per call).
    When `mirror` is supplied, `"mirror"` reads hit the local SQLite mirror first and
    fall back silently to live Notion when unavailable, not ready, stale, or missing.
    """

    def __init__(
        self,
        client: NotionClient | Callable[[], NotionClient | AbstractContextManager[NotionClient]],
        *,
        mirror: MirrorReader | None = None,
    ) -> None:
        if callable(client):
            self._client: NotionClient | None = None
            self._open_client: Callable[[], NotionClient | AbstractContextManager[NotionClient]] | None = client
        else:
            self._client = client
            self._open_client = None
        self._mirror: MirrorReader | None = mirror

    @classmethod
    def from_client(cls, client: NotionClient, *, mirror: MirrorReader | None = None) -> NotionSource:
        """Wrap an already-open `NotionClient` directly without closing it."""
        instance = cls.__new__(cls)
        instance._client = client
        instance._open_client = None
        instance._mirror = mirror
        return instance

    @contextmanager
    def _session(self) -> Iterator[NotionClient]:
        if self._client is not None:
            yield self._client
            return
        assert self._open_client is not None
        opened = self._open_client()
        if isinstance(opened, AbstractContextManager):
            with opened as client:
                yield client
            return
        try:
            yield opened
        finally:
            close = getattr(opened, "close", None)
            if callable(close):
                close()

    @contextmanager
    def open_write_client(self) -> Iterator[NotionClient]:
        """Yield a live `NotionClient` for writes, closing any per-call client on exit."""
        with self._session() as client:
            yield client

    def query_data_source(
        self,
        data_source_id: str,
        *,
        filter: JSON | None = None,
        sorts: list[JSON] | None = None,
        page_size: int = 100,
        freshness: Freshness = "mirror",
    ) -> list[JSON]:
        """Query a data source with `freshness` (`"mirror"` or `"live"`)."""
        _check_freshness(freshness)
        mirror: MirrorReader | None = getattr(self, "_mirror", None)
        if freshness == "mirror" and mirror is not None and filter is None and sorts is None:
            cached = mirror.query_data_source(data_source_id)
            if cached is not None:
                _last_read_source.set("mirror")
                return cached
        _last_read_source.set("live")
        with self._session() as client:
            return client.query_data_source(
                data_source_id, filter=filter, sorts=sorts, page_size=page_size
            )

    def retrieve_page(self, page_id: str, *, freshness: Freshness = "mirror") -> JSON:
        """Retrieve a page with `freshness` (`"mirror"` or `"live"`)."""
        _check_freshness(freshness)
        mirror: MirrorReader | None = getattr(self, "_mirror", None)
        if freshness == "mirror" and mirror is not None:
            cached = mirror.retrieve_page(page_id)
            if cached is not None:
                _last_read_source.set("mirror")
                return cached
        _last_read_source.set("live")
        with self._session() as client:
            return client.retrieve_page(page_id)

    def retrieve_data_source(
        self, data_source_id: str, *, freshness: Freshness = "mirror"
    ) -> JSON:
        """Retrieve a data source schema/metadata with `freshness` (`"mirror"` or `"live"`)."""
        _check_freshness(freshness)
        _last_read_source.set("live")
        with self._session() as client:
            return client.retrieve_data_source(data_source_id)

    def list_block_children(
        self, block_id: str, *, page_size: int = 100, freshness: Freshness = "live"
    ) -> list[JSON]:
        """List direct child blocks of `block_id` (`"live"` by default)."""
        _check_freshness(freshness)
        _last_read_source.set("live")
        with self._session() as client:
            return client.list_block_children(block_id, page_size=page_size)

    def retrieve_user(self, user_id: str, *, freshness: Freshness = "live") -> JSON:
        """Retrieve a Notion user (`"live"` by default)."""
        _check_freshness(freshness)
        _last_read_source.set("live")
        with self._session() as client:
            return client.retrieve_user(user_id)

    def create_page(self, body: JSON) -> JSON:
        """Create a page via a live `NotionClient`."""
        with self.open_write_client() as client:
            return client.create_page(body)

    def update_page(self, page_id: str, body: JSON) -> JSON:
        """Update a page via a live `NotionClient`."""
        with self.open_write_client() as client:
            return client.update_page(page_id, body)

    def append_block_children(self, block_id: str, children: list[JSON]) -> list[JSON]:
        """Append child blocks via a live `NotionClient`."""
        with self.open_write_client() as client:
            return client.append_block_children(block_id, children)


def as_source(source: NotionSource | NotionClient) -> NotionSource:
    """Return `source` as a `NotionSource`, wrapping an already-open `NotionClient` directly."""
    if isinstance(source, NotionSource):
        return source
    return NotionSource.from_client(source)
