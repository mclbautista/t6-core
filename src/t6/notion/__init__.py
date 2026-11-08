"""Shared Notion client for T6. Install with `pip install 't6[notion]'`.

- `AsyncNotionClient`: async httpx client for pages and data sources.
- `NotionClient`: blocking wrapper with the same methods and results.
- `NotionError`: the only error either client raises.
- `RetryPolicy`: opt-in retry (see the README).
- `t6.notion.models`: pages to `t6.domain` entities.
- `t6.notion.testing`: an in-memory fake Notion for tests.
"""

from t6.notion.client import BASE_URL, NOTION_VERSION, AsyncNotionClient, RetryPolicy
from t6.notion.errors import NotionError
from t6.notion.sync import NotionClient

__all__ = [
    "BASE_URL",
    "NOTION_VERSION",
    "AsyncNotionClient",
    "NotionClient",
    "NotionError",
    "RetryPolicy",
]
