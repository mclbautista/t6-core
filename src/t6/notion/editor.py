"""Resolve the author supplied by a Notion webhook to its display name."""
from __future__ import annotations

from typing import Any

from t6.notion.errors import NotionError
from t6.notion.sync import NotionClient
from t6.worker.client import WorkerJob


def editor(notion: NotionClient, job: WorkerJob) -> tuple[str, str | None]:
    authors = job.payload.get('authors')
    if not isinstance(authors, list) or len(authors) != 1 or not isinstance(authors[0], dict):
        return 'Unknown', None
    author: dict[str, Any] = authors[0]
    user_id = str(author['id']) if author.get('id') else None
    name = author.get('name')
    if not name and user_id:
        try:
            name = notion.retrieve_user(user_id).get('name')
        except NotionError as error:
            if error.status != 404:
                raise
    return str(name or user_id or 'Unknown'), user_id
