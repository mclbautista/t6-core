"""Shared Notion client for T6. Install with `pip install 't6[notion]'`.

- `AsyncNotionClient`: async httpx client for pages and data sources.
- `NotionClient`: blocking wrapper with the same methods and results.
- `NotionSource`: reader layer with explicit `freshness` (`"mirror"` or `"live"`).
- `NotionError`: the only error either client raises.
- `RetryPolicy`: opt-in retry (see the README).
- `t6.notion.contacts`: read Contacts, set a Telegram user ID, create a Contact, in domain terms.
- `t6.notion.models`: pages to `t6.domain` entities.
- `t6.notion.generated`: one model per database, from the schema snapshot.
- `t6.notion.schema`: the snapshot and the `t6 admin schema-check` logic.
- `t6.notion.cameras`: read and update Cameras in domain terms (`t6 camera`).
- `t6.notion.testing`: an in-memory fake Notion for tests.
"""

from t6.notion.client import BASE_URL, NOTION_VERSION, AsyncNotionClient, RequestPacer, RetryPolicy
from t6.notion.errors import NotionError
from t6.notion.mirror import (
    DEFAULT_MAX_AGE_SECONDS,
    MIN_REQUEST_INTERVAL_SECONDS,
    MirrorReader,
    MirrorSourceState,
    MirrorSyncer,
    MirrorWriter,
    mirror_db_path,
    mirror_enabled,
)
from t6.notion.source import Freshness, NotionSource, as_source, last_read_from_mirror
from t6.notion.sync import NotionClient

__all__ = [
    "BASE_URL",
    "DEFAULT_MAX_AGE_SECONDS",
    "MIN_REQUEST_INTERVAL_SECONDS",
    "NOTION_VERSION",
    "AsyncNotionClient",
    "Freshness",
    "MirrorReader",
    "MirrorSourceState",
    "MirrorSyncer",
    "MirrorWriter",
    "NotionClient",
    "NotionError",
    "NotionSource",
    "RequestPacer",
    "RetryPolicy",
    "as_source",
    "last_read_from_mirror",
    "mirror_db_path",
    "mirror_enabled",
]
