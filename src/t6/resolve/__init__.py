"""DaVinci Resolve project server reader. Install with `pip install 't6[resolve]'`.

- `ProjectServer(db, database, schema)`: lists every project of one database.
  It refuses a role that can write.
- `ResolveProject`: database, folder path, name and internal ID. Its
  `external_id` is `<database>/<folder path>/<project name>`, and `ref` is the
  ExternalRef for External Links (system `resolve`, kind `project`, internal ID
  the server's project ID).
- `map_projects(projects, links)`: match projects to External Links rows and
  report the unmapped ones.
- `schema_for(version)`: table layout per `DaVinci Resolve Version`.
- `t6.resolve.postgres.PostgresDatabase`: the live connection.
"""

from t6.resolve.reader import (
    KIND,
    SYSTEM,
    Database,
    MappingReport,
    ProjectServer,
    ReadOnlyViolation,
    ResolveProject,
    map_projects,
)
from t6.resolve.schema import SCHEMAS, ProjectServerSchema, schema_for

__all__ = [
    "KIND",
    "SCHEMAS",
    "SYSTEM",
    "Database",
    "MappingReport",
    "ProjectServer",
    "ProjectServerSchema",
    "ReadOnlyViolation",
    "ResolveProject",
    "map_projects",
    "schema_for",
]
