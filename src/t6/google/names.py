"""Names for event content: Notion keeps the Project's and the Person's names, t6 keeps the binding (#260)."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol
from uuid import UUID

from t6.google.calendar import CalendarError
from t6.notion import NotionClient, NotionError, NotionSource, as_source
from t6.notion.reads import ContactReader, ProjectReader
from t6.registry.sqlite import SqliteProjectRegistry


class Identities(Protocol):
    """The Google email a Person holds in Notion. A lookup that fails raises `CalendarError`, so the job retries."""

    def email(self, person: str) -> str | None: ...


class NotionNames:
    def __init__(self, registry: SqliteProjectRegistry, source: NotionSource | NotionClient) -> None:
        self._registry = registry
        notion = as_source(source)
        self._projects, self._people = ProjectReader(notion, ''), ContactReader(notion, '')

    def _page(self, entity: str) -> str:
        with self._registry._transaction() as db:
            row = db.execute("SELECT page_id FROM bindings WHERE system = 'notion' AND project_id = ? AND active = 1", (entity,)).fetchone()
        if row is None:
            raise CalendarError(f'{entity} has no Notion page')
        page: str = row[0]
        if len(page) == 32:
            try:
                return str(UUID(page))
            except ValueError:
                pass
        return page

    def project(self, project: str) -> str:
        try:
            return self._projects.get(self._page(project)).name
        except (NotionError, OSError) as error:
            raise CalendarError('Project name lookup failed') from error

    def person(self, person: str) -> str:
        try:
            return self._people.get(self._page(person)).name
        except (NotionError, OSError) as error:
            raise CalendarError('Person name lookup failed') from error

    def email(self, person: str) -> str | None:
        """The Person's email from the Notion Contact page (Notion owns typed fields), for mapping a Google editor to a Person."""
        try:
            return self._people.get(self._page(person)).email
        except (NotionError, OSError) as error:
            raise CalendarError('Person email lookup failed') from error


def notion_names(environ: Mapping[str, str], registry: SqliteProjectRegistry, reason: str) -> NotionNames:
    """The Notion-backed names and emails for a Google handler. A missing `T6_NOTION_TOKEN` is a `ValueError` that says `reason`."""
    token = environ.get('T6_NOTION_TOKEN')
    if not token:
        raise ValueError(f'missing environment variable: T6_NOTION_TOKEN ({reason})')
    return NotionNames(registry, NotionSource(lambda: NotionClient(token)))
