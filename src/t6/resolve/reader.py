"""Read the Resolve project server and list every project it holds.

The reader only selects. It also refuses to start when the database role could
write (`ReadOnlyViolation`), so a mis-granted role is found before any read.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from t6.domain import ExternalRef
from t6.notion.external_links import ExternalLink, ExternalLinks
from t6.resolve.schema import ProjectServerSchema

SYSTEM = "resolve"
KIND = "project"


class ReadOnlyViolation(Exception):
    """The database role can write, so the reader will not use it."""


class Database(Protocol):
    """One connection to one project server database, as the reader needs it."""

    def can_write(self) -> bool:
        """True when the connected role could change data or the schema."""
        ...

    def query(self, sql: str) -> Sequence[Sequence[Any]]:
        """Run one SELECT and return its rows."""
        ...


@dataclass(frozen=True, slots=True, kw_only=True)
class ResolveProject:
    """One project on the server."""

    database: str
    folder_path: tuple[str, ...]
    name: str
    internal_id: str

    @property
    def external_id(self) -> str:
        """`<database>/<folder path>/<project name>`. A project in the root folder has no folder segment."""
        return "/".join((self.database, *self.folder_path, self.name))

    @property
    def ref(self) -> ExternalRef:
        return ExternalRef(
            system=SYSTEM, kind=KIND, external_id=self.external_id, internal_id=self.internal_id
        )


class ProjectServer:
    """The project inventory of one database on the Resolve project server."""

    def __init__(
        self, db: Database, database: str, schema: ProjectServerSchema, *,
        allow_unverified_schema: bool = False,
    ) -> None:
        if not schema.verified and not allow_unverified_schema:
            raise ValueError(
                f"Resolve {schema.version!r} schema is unverified; "
                "only use allow_unverified_schema=True for controlled investigation"
            )
        if db.can_write():
            raise ReadOnlyViolation(
                f"the role for database {database!r} can write; use a read-only role"
            )
        self._db = db
        self._database = database
        self._schema = schema

    def projects(self) -> tuple[ResolveProject, ...]:
        """Every project, sorted by folder path then name."""
        s = self._schema
        folders = {
            str(row[0]): (str(row[1]), None if row[2] is None else str(row[2]))
            for row in self._db.query(
                f'SELECT "{s.folder_id}", "{s.folder_name}", "{s.folder_parent_id}" '
                f'FROM "{s.folder_table}"'
            )
        }
        rows = self._db.query(
            f'SELECT "{s.project_id}", "{s.project_name}", "{s.project_folder_id}" '
            f'FROM "{s.project_table}"'
        )
        projects = [
            ResolveProject(
                database=self._database,
                folder_path=self._path(folders, None if row[2] is None else str(row[2])),
                name=str(row[1]),
                internal_id=str(row[0]),
            )
            for row in rows
        ]
        return tuple(sorted(projects, key=lambda p: (p.folder_path, p.name, p.internal_id)))

    @staticmethod
    def _path(
        folders: dict[str, tuple[str, str | None]], folder_id: str | None
    ) -> tuple[str, ...]:
        names: list[str] = []
        seen: set[str] = set()
        while folder_id is not None and folder_id in folders and folder_id not in seen:
            seen.add(folder_id)
            name, parent = folders[folder_id]
            if parent is not None:  # the root folder has no parent and no name in the path
                names.append(name)
            folder_id = parent
        return tuple(reversed(names))


@dataclass(frozen=True, slots=True, kw_only=True)
class MappingReport:
    """Server projects against External Links rows."""

    mapped: tuple[tuple[ResolveProject, ExternalLink], ...]
    unmapped: tuple[ResolveProject, ...]  # no row, or a row with no Notion Project
    orphan_links: tuple[ExternalLink, ...]  # rows whose project is not on the server


def map_projects(
    projects: Sequence[ResolveProject], links: ExternalLinks
) -> MappingReport:
    """Match each project to its External Links row and Notion Project. Reads only."""
    by_id = {link.ref.external_id: link for link in links.links_for_system(SYSTEM, KIND)}
    mapped: list[tuple[ResolveProject, ExternalLink]] = []
    unmapped: list[ResolveProject] = []
    for project in projects:
        link = by_id.get(project.external_id)
        if link is not None and link.project_ids:
            mapped.append((project, link))
        else:
            unmapped.append(project)
    on_server = {p.external_id for p in projects}
    orphans = [link for key, link in by_id.items() if key not in on_server]
    return MappingReport(
        mapped=tuple(mapped), unmapped=tuple(unmapped), orphan_links=tuple(orphans)
    )
