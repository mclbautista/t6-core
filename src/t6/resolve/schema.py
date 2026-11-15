"""The project server tables the reader needs, per DaVinci Resolve version.

A `ProjectServerSchema` names the tables and columns the reader selects from.
It is the only place that knows Resolve's table layout, so a version that
renames a column needs one new entry here and one new fixture, not a code change.

The layouts below are assumptions and have not been checked against a live
server. Their synthetic SQLite fixtures test the reader, not real schema
compatibility. Validate the schema and inventory against the server before
marking an entry verified.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True, kw_only=True)
class ProjectServerSchema:
    """Table and column names for one Resolve version. Names are identifiers, never user input.

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


# Keys match the Notion Projects property `DaVinci Resolve Version`.
SCHEMAS: dict[str, ProjectServerSchema] = {
    version: ProjectServerSchema(
        version=version,
        project_table="Sm2Project",
        project_id="SM_ProjectID",
        project_name="ProjectName",
        project_folder_id="FolderId",
        folder_table="Sm2Folder",
        folder_id="SM_FolderID",
        folder_name="FolderName",
        folder_parent_id="ParentFolderId",
    )
    for version in ("18", "19")
}


def schema_for(version: str) -> ProjectServerSchema:
    """The schema for a Resolve version, or KeyError naming the versions that exist."""
    try:
        return SCHEMAS[version]
    except KeyError:
        known = ", ".join(sorted(SCHEMAS))
        raise KeyError(f"no project server schema for Resolve {version!r} (known: {known})") from None
