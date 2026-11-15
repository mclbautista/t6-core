"""A project server stand-in for tests: SQLite loaded from a schema fixture.

`ServerFixture(sql)` builds a database file from the fixture's DDL and rows.
`readonly()` opens it the way the read-only role does: any write fails.
`writable()` opens it with write rights, to test that the reader refuses it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from t6.resolve.timelines import ResolveTimeline


class SqliteDatabase:
    def __init__(self, path: Path, *, read_only: bool) -> None:
        mode = "ro" if read_only else "rw"
        self._conn = sqlite3.connect(f"file:{path}?mode={mode}", uri=True)
        self._read_only = read_only

    def can_write(self) -> bool:
        return not self._read_only

    def query(self, sql: str) -> Sequence[Sequence[Any]]:
        return self._conn.execute(sql).fetchall()

    def execute(self, sql: str) -> None:
        """Run a statement. Present so tests can prove a read-only connection cannot write."""
        self._conn.execute(sql)

    def close(self) -> None:
        self._conn.close()


class ServerFixture:
    def __init__(self, sql: str) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self._path = Path(self._dir.name) / "resolve.db"
        conn = sqlite3.connect(self._path)
        conn.executescript(sql)
        conn.commit()
        conn.close()

    def readonly(self) -> SqliteDatabase:
        return SqliteDatabase(self._path, read_only=True)

    def writable(self) -> SqliteDatabase:
        return SqliteDatabase(self._path, read_only=False)

    def close(self) -> None:
        self._dir.cleanup()


class FakeTimelineReader:
    """A `TimelineReader` for tests: timelines held in memory per Resolve project, renamable like Resolve."""

    def __init__(self, projects: dict[str, list[ResolveTimeline]] | None = None) -> None:
        self._projects = {ref: list(timelines) for ref, timelines in (projects or {}).items()}

    def add(self, resolve_project: str, timeline: ResolveTimeline) -> None:
        self._projects.setdefault(resolve_project, []).append(timeline)

    def rename(self, resolve_project: str, uuid: str, name: str) -> None:
        timelines = self._projects[resolve_project]
        timelines[:] = [replace(t, name=name) if t.uuid == uuid else t for t in timelines]

    def timelines(self, resolve_project: str) -> tuple[ResolveTimeline, ...] | None:
        found = self._projects.get(resolve_project)
        return None if found is None else tuple(found)
