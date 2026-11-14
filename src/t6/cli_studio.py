"""Studio commands of the `t6` CLI: `departments`, `contacts find`, `projects resolve`, `standing-cc`.

Each prints one JSON document with Notion page and option IDs. Errors are JSON
on stderr with exit status 2 (see `t6.cli`). Nothing here writes to Notion.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from t6.cli import DEFAULT_REGISTRY_DB, REGISTRY_DB_VARIABLE, TOKEN_VARIABLE, _fail, _open_registry, _read_data_sources

if TYPE_CHECKING:
    from t6.domain import Contact, Project
    from t6.lookup import Candidate, Resolution
    from t6.notion.sync import NotionClient

JSON = dict[str, Any]


def add_parsers(commands: Any) -> None:
    def data_sources(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--data-sources", type=Path, required=True, metavar="FILE", help="JSON of database key to data source ID")

    departments = commands.add_parser(
        "departments",
        help="Print the studio Departments, read live, in display order, with option IDs",
        description=f"Reads ${TOKEN_VARIABLE}. Needs a 'job_orders' entry in --data-sources.",
    )
    data_sources(departments)
    contacts = commands.add_parser("contacts", help="Contact commands")
    contacts_commands = contacts.add_subparsers(dest="contacts_command", required=True)
    find = contacts_commands.add_parser(
        "find",
        help="Find a Contact by name, @handle, alias or Telegram user ID",
        description=(
            f"Reads ${TOKEN_VARIABLE}. Needs a 'contacts' entry in --data-sources. Prints the match, or "
            "the candidates for a person to choose. Fuzzy matching needs t6[lookup]."
        ),
    )
    find.add_argument("mention", help="a name, @handle, alias or Telegram user ID")
    data_sources(find)
    projects = commands.add_parser("projects", help="Project commands")
    projects_commands = projects.add_subparsers(dest="projects_command", required=True)
    resolve = projects_commands.add_parser(
        "resolve",
        help="Resolve a Project Code (or Project name) to a Project",
        description=(
            f"Reads ${TOKEN_VARIABLE}. Needs a 'projects' entry in --data-sources. A reused code is resolved "
            "by rotation when the registry file at --registry-db knows the Projects."
        ),
    )
    resolve.add_argument("text", help="a Project Code, or part of a Project name")
    data_sources(resolve)
    resolve.add_argument(
        "--registry-db",
        type=Path,
        default=Path(os.environ.get(REGISTRY_DB_VARIABLE, DEFAULT_REGISTRY_DB)),
        metavar="FILE",
        help="the Project registry file; a missing file means every Project counts as in rotation",
    )
    commands.add_parser(
        "standing-cc",
        help="Print the standing cc Contact page ID ($T6_STANDING_CC)",
        description="Prints {\"standing_cc\": <Contact page ID or null>}. Needs no Notion access.",
    )


def run(args: argparse.Namespace, open_notion: Callable[[str], NotionClient]) -> int:
    if args.command == "standing-cc":
        from t6.domain.studio_people import standing_cc

        return _print({"standing_cc": standing_cc()})
    try:
        from t6.notion import NotionError
    except ImportError:
        return _fail(f"{args.command} needs the notion extra: pip install 't6[notion]'")
    token = os.environ.get(TOKEN_VARIABLE)
    if not token:
        return _fail(f"set {TOKEN_VARIABLE} to a Notion integration token")
    try:
        sources = _read_data_sources(args.data_sources)
        key = {"departments": "job_orders", "contacts": "contacts", "projects": "projects"}[args.command]
        if key not in sources:
            return _fail(f"--data-sources has no '{key}' entry")
        with open_notion(token) as client:
            if args.command == "departments":
                result = _departments(client, sources[key])
            elif args.command == "contacts":
                result = _contacts_find(client, sources[key], args.mention)
            else:
                result = _projects_resolve(client, sources[key], args.text, args.registry_db)
    except (OSError, ValueError, ImportError, NotionError) as error:
        return _fail(str(error))
    return _print(result)


def _print(result: JSON) -> int:
    import json

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _departments(client: NotionClient, data_source_id: str) -> JSON:
    from t6.notion.departments import get_departments

    return {"departments": [{"id": d.option_id, "name": d.name} for d in get_departments(client, data_source_id)]}


def _contact(contact: Contact) -> JSON:
    return {
        "id": contact.page_id,
        "name": contact.name,
        "telegram_handle": contact.telegram_handle,
        "telegram_user_id": contact.telegram_user_id,
        "email": contact.email,
        "aliases": list(contact.aliases),
        "end_of_shift": contact.end_of_shift.strftime("%H:%M") if contact.end_of_shift else None,
    }


def _project(project: Project) -> JSON:
    return {
        "id": project.page_id,
        "t6_id": project.t6_id,
        "name": project.name,
        "project_code": project.project_code,
        "status": project.status,
        "rotation": project.rotation,
    }


def _resolution(found: Resolution[Any], describe: Callable[[Any], JSON]) -> JSON:
    def candidate(item: Candidate[Any]) -> JSON:
        return {**describe(item.entity), "score": round(item.score, 3)}

    return {
        "match": describe(found.entity) if found.entity is not None else None,
        "method": found.method,
        "score": round(found.score, 3),
        "needs_review": found.needs_review,
        "reason": found.reason,
        "candidates": [candidate(item) for item in found.candidates],
    }


def _contacts_find(client: NotionClient, data_source_id: str, mention: str) -> JSON:
    from t6.lookup import Index, Lookup
    from t6.notion.reads import ContactReader

    lookup = Lookup(Index(contacts=ContactReader(client, data_source_id).list()))
    return _resolution(lookup.contact(mention), _contact)


def _projects_resolve(client: NotionClient, data_source_id: str, text: str, registry_db: Path) -> JSON:
    from t6.lookup import Index, Lookup
    from t6.notion.reads import ProjectReader

    projects = ProjectReader(client, data_source_id).list()
    registry = _open_registry(registry_db)
    if registry is not None:
        from t6.registry import ProjectRefs

        refs = ProjectRefs(registry)
        projects = tuple(refs.identified(project) for project in projects)
    lookup = Lookup(Index(projects=projects))
    found = lookup.project(text)
    if found.entity is None and not found.candidates:
        found = lookup.project(None, name=text)
    return _resolution(found, _project)
