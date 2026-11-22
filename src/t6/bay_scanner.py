"""Read a bay's local DaVinci Resolve disk library, read-only (ADR 0008, #377).

This module is shipped to a bay as one file and run with Munki's own Python, so it uses the standard library only and
imports nothing from `t6`. It must stay runnable on Python 3.9 and later. A test enforces both.

The Resolve disk library is a folder of plain SQLite files, one `Project.db` per project (facts in
`docs/research/bay-resolve-disk-library.md`). The reader copies each `Project.db` to a temporary folder, opens only the
copy with `mode=ro&immutable=1`, and deletes the copy. It writes nothing inside the Resolve folders and never starts
Resolve. It does not read `ProjectMetadataCache/Metadata.db`, which is stale and incomplete.
"""
from __future__ import annotations

import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

LIBRARY_USERS = Path("Library/Application Support/Blackmagic Design/DaVinci Resolve/Resolve Project Library/Resolve Projects/Users")
"""Where the disk library sits below a login user's home. One Resolve user folder per library user."""

PROJECT_DB = "Project.db"


def project_roots(home: Path) -> list[Path]:
    """The `Projects` folder of every Resolve user in the library below `home`; none when there is no library."""
    users = home / LIBRARY_USERS
    if not users.is_dir():
        return []
    return sorted(path / "Projects" for path in users.iterdir() if (path / "Projects").is_dir())


def _read_project(db_copy: Path) -> dict[str, Any]:
    """One project's facts from its copied `Project.db`. Raises `ValueError` with the reason when it cannot be read."""
    try:
        connection = sqlite3.connect(f"{db_copy.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
    except sqlite3.Error as error:
        raise ValueError(f"cannot open: {error}") from error
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        missing = sorted({"SM_Project", "Sm2Timeline"} - tables)
        if missing:
            raise ValueError("unknown database layout: no table " + ", ".join(missing))
        rows = connection.execute("SELECT SM_Project_id, ProjectName, LastModTimeInSecs FROM SM_Project").fetchall()
        if len(rows) != 1:
            raise ValueError(f"unknown database layout: {len(rows)} rows in SM_Project, expected 1")
        uuid, name, modified = rows[0]
        if not isinstance(uuid, str) or not isinstance(name, str) or not isinstance(modified, (int, float)):
            raise ValueError("unknown database layout: SM_Project columns hold unexpected types")
        count = connection.execute("SELECT count(*) FROM Sm2Timeline").fetchone()[0]
        return {"project_uuid": uuid, "project_name": name, "last_modified": int(modified), "timeline_count": int(count)}
    except sqlite3.Error as error:
        raise ValueError(f"unreadable: {error}") from error
    finally:
        connection.close()


def scan_library(home: Path) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Every local copy below `home`, and the projects that could not be read, each with a reason.

    A project that fails never stops the others. Nothing under `home` is written: the `Project.db` files are copied to a
    temporary folder that is removed before this returns.
    """
    copies: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    scratch = Path(tempfile.mkdtemp(prefix="t6-bay-scan-"))
    try:
        for root in project_roots(home):
            for source in sorted(root.rglob(PROJECT_DB)):
                relative = source.parent.relative_to(root)
                folder = relative.parent.as_posix()
                folder_path = "" if folder == "." else folder
                try:
                    target = scratch / str(len(copies) + len(errors)) / PROJECT_DB
                    target.parent.mkdir()
                    shutil.copyfile(source, target)
                    copies.append({**_read_project(target), "folder_path": folder_path})
                except (OSError, ValueError) as error:
                    errors.append({"folder_path": folder_path, "project": relative.name, "reason": str(error)})
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    copies.sort(key=lambda copy: (copy["folder_path"], copy["project_name"], copy["project_uuid"]))
    return copies, errors
