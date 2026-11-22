"""Build `privacy_registry.json` from Notion: the one command and the worker job share this code (#534)."""
import json
import os
from pathlib import Path
from typing import Any

from t6.domain.events import JsonValue
from t6.notion.reads import ContactReader, ProjectReader, StudioReader
from t6.notion.source import NotionSource


def build(notion: NotionSource, sources: dict[str, str]) -> dict[str, Any]:
    """`{people, projects, ids}` from the Studios, Contacts (when 'contacts' is a data source) and Projects."""
    studios = StudioReader(notion, sources["studios"]).names()
    projects = ProjectReader(notion, sources["projects"]).list()
    contacts = ContactReader(notion, sources["contacts"]).list() if sources.get("contacts") else ()

    def clean(name: str) -> str:
        return " ".join(name.split())

    people: dict[str, dict[str, Any]] = {clean(name): {"name": clean(name)} for name in studios if name.strip()}
    for contact in contacts:
        if contact.name.strip() and clean(contact.name) not in people:
            people[clean(contact.name)] = {"name": clean(contact.name), "aliases": [clean(a) for a in contact.aliases if a.strip()],
                                           "handles": [contact.telegram_handle] if contact.telegram_handle else []}
    shows: dict[str, dict[str, Any]] = {}
    for project in projects:
        if project.name.strip() and clean(project.name) not in shows:
            shows[clean(project.name)] = {"name": clean(project.name), **({"code": project.project_code} if project.project_code else {})}
    return {"people": list(people.values()), "projects": list(shows.values()), "ids": []}


def write(path: Path, payload: dict[str, Any]) -> None:
    """Replace the file in one step, so a reader never sees half of it."""
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


class Rebuild:
    """The main worker's `privacy_registry.rebuild` handler: read Notion, replace the file the API reloads from.

    It returns and logs counts only; a name never reaches the job result, the log or Audit.
    """

    def __init__(self, notion: NotionSource, sources: dict[str, str], output: Path) -> None:
        self._notion, self._sources, self._output = notion, sources, output

    def __call__(self, job: object) -> dict[str, JsonValue]:
        payload = build(self._notion, self._sources)
        write(self._output, payload)
        return {"people": len(payload["people"]), "projects": len(payload["projects"])}
