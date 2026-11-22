"""Synthetic Resolve disk libraries for the bay scanner tests. No real project, folder, client or person name appears here."""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from t6.bay_scanner import LIBRARY_USERS


def library(home: Path, user: str = "user-a") -> Path:
    """The `Projects` folder of one Resolve library user below `home`."""
    root = home / LIBRARY_USERS / user / "Projects"
    root.mkdir(parents=True, exist_ok=True)
    return root


def project(root: Path, folders: tuple[str, ...], name: str, uuid: str, modified: int, timelines: int) -> Path:
    """One `Project.db` with the two tables the scanner reads, at `root/<folders>/<name>/Project.db`."""
    folder = root.joinpath(*folders, name)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "Project.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE SM_Project (SM_Project_id TEXT PRIMARY KEY, ProjectName TEXT, LastModTimeInSecs INTEGER)")
    connection.execute("CREATE TABLE Sm2Timeline (Sm2Timeline_id TEXT PRIMARY KEY, Name TEXT)")
    connection.execute("INSERT INTO SM_Project VALUES (?, ?, ?)", (uuid, name, modified))
    connection.executemany("INSERT INTO Sm2Timeline VALUES (?, ?)", [(f"{uuid}-t{n}", f"Timeline {n}") for n in range(timelines)])
    connection.commit()
    connection.close()
    return path


def snapshot(root: Path) -> dict[str, tuple[str, int]]:
    """Every file below `root` with its content hash and modified time, to prove a scan changed nothing."""
    return {str(path.relative_to(root)): (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
            for path in sorted(root.rglob("*")) if path.is_file()}
