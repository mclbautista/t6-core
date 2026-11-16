"""The project server tables the reader needs.

A `ProjectServerSchema` names the tables and columns the reader selects from.
It is the only place that knows Resolve's table layout, so a version that
renames a column needs one new entry here and one new fixture, not a code change.

The layout below was checked against the live server on 2026-11-16
(PostgreSQL 13.4, server app 20). One server holds projects of every Resolve
version, so the layout is the server's, not one Project's. The synthetic SQLite
fixture tests the reader, not real schema compatibility.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True, kw_only=True)
class ProjectServerSchema:
    """Table and column names of the project server. Names are identifiers, never user input.

    `verified` is an explicit claim by the schema author, not a runtime check.
    """

    version: str
    project_table: str
    project_id: str
    project_name: str
    project_folder_id: str
    folder_table: str
    folder_id: str
    folder_name: str
    folder_parent_id: str
    verified: bool = False


# Keys are the major Resolve versions that share the verified layout.
SCHEMAS: dict[str, ProjectServerSchema] = {
    version: ProjectServerSchema(
        version=version,
        project_table="SM_Project",
        project_id="SM_Project_id",
        project_name="ProjectName",
        project_folder_id="Folder",
        folder_table="SM_ProjectFolder",
        folder_id="SM_ProjectFolder_id",
        folder_name="Name",
        folder_parent_id="ParentFolder",
        verified=True,
    )
    for version in ("17", "18", "19", "20")
}


def schema_for(version: str) -> ProjectServerSchema:
    """The schema for a Resolve version, matched on its major, or KeyError naming the majors that exist."""
    major = version.split(".")[0]
    try:
        return SCHEMAS[major]
    except KeyError:
        known = ", ".join(sorted(SCHEMAS))
        raise KeyError(f"no project server schema for Resolve {version!r} (known: {known})") from None
