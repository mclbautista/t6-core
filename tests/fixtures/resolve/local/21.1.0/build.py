"""Build a synthetic subset of the Resolve 21.1.0 local library for #374.

Run: python3 build.py /path/to/new/library
The destination must not exist. The result is a scanner fixture, not a library
that Resolve can open. No file from the inspected Bay is an input.
"""

import argparse
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import UUID


SCHEMA = """
CREATE TABLE SM_Project (
    SM_Project_id uuid NOT NULL PRIMARY KEY,
    ProjectName character varying NOT NULL,
    Folder uuid,
    LastModTimeInSecs bigint,
    FieldsBlob BLOB
);
CREATE TABLE SM_ProjectFolder (
    SM_ProjectFolder_id uuid PRIMARY KEY,
    ParentFolder uuid,
    Name character varying,
    ModDateTime character varying
);
CREATE TABLE Sm2Timeline (
    Sm2Timeline_id uuid NOT NULL PRIMARY KEY,
    Name character varying NOT NULL,
    SM_Project_id uuid,
    ModTimeInSecs bigint,
    FieldsBlob BLOB
);
"""


def build(library: Path) -> None:
    library.mkdir(mode=0o700)
    projects = library / "Resolve Projects" / "Users" / "fixture-user" / "Projects"
    entries = (((), "Synthetic root"), (("Folder A",), "Synthetic duplicate"),
               (("Folder B",), "Synthetic duplicate"))
    for i, (folders, name) in enumerate(entries, start=1):
        project = projects.joinpath(*folders, name)
        project.mkdir(parents=True)
        with closing(sqlite3.connect(project / "Project.db")) as con, con:
            con.executescript(SCHEMA)
            con.execute(
                "INSERT INTO SM_Project VALUES (?, ?, ?, ?, NULL)",
                (str(UUID(int=i)), name, str(UUID(int=i + 10)) if folders else None, 100 + i),
            )
            con.execute(
                "INSERT INTO Sm2Timeline VALUES (?, ?, NULL, ?, NULL)",
                (str(UUID(int=i + 100)), "Synthetic timeline", 200 + i),
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("library", type=Path)
    build(parser.parse_args().library)
