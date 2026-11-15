"""Names for event content: Notion keeps the Project's and the Person's names, t6 keeps the binding (#260)."""
from __future__ import annotations

from uuid import UUID

from t6.google.calendar import CalendarError
from t6.notion import NotionClient, NotionError
from t6.notion.reads import ContactReader, ProjectReader
from t6.registry.sqlite import SqliteProjectRegistry


class NotionNames:
    def __init__(self, registry: SqliteProjectRegistry, client: NotionClient) -> None:
        self._registry = registry
        self._projects, self._people = ProjectReader(client, ''), ContactReader(client, '')

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
