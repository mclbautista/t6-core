"""Studio commands of the `t6` CLI: `contacts find`, `projects resolve`, `standing-cc`.

Each prints one JSON document with Notion page and option IDs. Errors are JSON
on stderr with exit status 2 (see `t6.cli`). Nothing here writes to Notion.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from t6.cli import DEFAULT_REGISTRY_DB, REGISTRY_DB_VARIABLE, TOKEN_VARIABLE, _fail, _read_data_sources

if TYPE_CHECKING:
    from t6.domain import Contact, Project
    from t6.lookup import Candidate, Resolution
    from t6.notion.source import NotionSource
    from t6.notion.sync import NotionClient

JSON = dict[str, Any]


def add_parsers(commands: Any) -> None:
    def data_sources(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--data-sources", type=Path, metavar="FILE", help="JSON of database key to data source ID")

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
    find.add_argument("mention", nargs="?", help="a name, @handle, alias or Telegram user ID")
    find.add_argument("--mention", dest="mentions", action="append", metavar="NAME", help="a name to find; repeat it to find several names in one call (instead of the positional mention)")
    find.add_argument("--suggest", action="store_true", help="candidates are up to 3 suggestions, even after an exact match")
    find.add_argument("--prefer-telegram", action="store_true", help="search the Contacts that have a Telegram ID first")
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
        "--in-text",
        action="store_true",
        help="find one known Project Code inside longer text (whole token, longest first); never falls back to a name match",
    )
    resolve.add_argument(
        "--registry-db",
        type=Path,
        default=Path(os.environ.get(REGISTRY_DB_VARIABLE, DEFAULT_REGISTRY_DB)),
        metavar="FILE",
        help="the Project registry file; with no such file every Project counts as in rotation; an unreadable file is an error",
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
        source_key = {"contacts": "contacts", "projects": "projects"}[args.command]
        if source_key not in sources:
            return _fail(f"--data-sources has no '{source_key}' entry")
        from t6.notion.source import NotionSource

        notion = NotionSource(lambda: open_notion(token))
        if args.command == "contacts":
            result = _contacts_find(notion, sources[source_key], args.mention, args.mentions, args.suggest, args.prefer_telegram)
            if getattr(args, "person_lookup", False):
                from t6.verbs import _people
                try:
                    people = _people()
                except ValueError:
                    people = None
                for found in result.get("results", [result]):
                    for item in ([found["match"]] if found["match"] else []) + found["candidates"]:
                        item["t6_id"] = people.registry.bound_entity("notion", item["id"]) if people else None
        else:
            result = _projects_resolve(notion, sources[source_key], args.text, args.registry_db, args.in_text)
    except (OSError, ValueError, ImportError, NotionError) as error:
        return _fail(str(error))
    return _print(result)


def _print(result: JSON) -> int:
    import json

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _contact(contact: Contact) -> JSON:
    return {
        "id": contact.page_id,
        "name": contact.name,
        "telegram_handle": contact.telegram_handle,
        "telegram_user_id": contact.telegram_user_id,
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


def _resolution(
    found: Resolution[Any], describe: Callable[[Any], JSON], candidates: Sequence[Candidate[Any]] | None = None
) -> JSON:
    def candidate(item: Candidate[Any]) -> JSON:
        return {**describe(item.entity), "score": round(item.score, 3)}

    return {
        "match": describe(found.entity) if found.entity is not None else None,
        "method": found.method,
        "score": round(found.score, 3),
        "needs_review": found.needs_review,
        "reason": found.reason,
        "candidates": [candidate(item) for item in (found.candidates if candidates is None else candidates)],
    }


MAX_MENTIONS = 50


def _find_contacts(
    contacts: Sequence[Contact],
    describe: Callable[[Any], JSON],
    mention: str | None,
    mentions: Sequence[str] | None,
    suggest: bool,
    prefer_telegram: bool,
) -> JSON:
    """One resolution for `mention`, or `{"results": [...]}`, one for each of `mentions` in order, over one `Index`."""
    from t6.lookup import Index, Lookup

    if (mention is None) == (mentions is None):
        raise ValueError("give exactly one of mention and mentions")
    if mentions is not None and len(mentions) > MAX_MENTIONS:
        raise ValueError(f"mentions holds at most {MAX_MENTIONS} names")
    lookup = Lookup(Index(contacts=tuple(contacts)))
    team = tuple(c.page_id for c in contacts if c.telegram_user_id is not None) if prefer_telegram else ()

    def find(name: str) -> JSON:
        return _resolution(lookup.contact(name, team=team), describe, lookup.suggest_contacts(name, team=team) if suggest else None)

    if mention is not None:
        return find(mention)
    return {"results": [find(name) for name in mentions or ()]}


def _contacts_find(
    client: NotionSource | NotionClient,
    data_source_id: str,
    mention: str | None,
    mentions: Sequence[str] | None = None,
    suggest: bool = False,
    prefer_telegram: bool = False,
) -> JSON:
    from t6.notion.reads import ContactReader

    return _find_contacts(ContactReader(client, data_source_id).list(), _contact, mention, mentions, suggest, prefer_telegram)


def _projects_resolve(
    client: NotionSource | NotionClient,
    data_source_id: str,
    text: str,
    registry_db: Path,
    in_text: bool = False,
) -> JSON:
    from t6.lookup import Index, Lookup
    from t6.notion.reads import ProjectReader

    projects = ProjectReader(client, data_source_id).list()
    if registry_db.is_file():
        from t6.registry import ProjectRefs, RegistryUnavailable, SqliteProjectRegistry

        try:
            refs = ProjectRefs(SqliteProjectRegistry(registry_db))
            projects = tuple(refs.identified(project) for project in projects)
        except (RegistryUnavailable, sqlite3.Error) as error:
            raise ValueError(f"the registry at {registry_db} cannot be read: {error}") from error
    lookup = Lookup(Index(projects=projects))
    if in_text:
        return _resolution(lookup.project_in_text(text), _project)
    found = lookup.project(text)
    if found.entity is None and not found.candidates:
        found = lookup.project(None, name=text)
    return _resolution(found, _project)
