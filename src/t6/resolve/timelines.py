"""Resolve timelines as a reader shows them: name, Resolve unique id and the first-frame marker stamp.

The stamp is the `t6t_` ID in the custom data of a marker at the timeline's first frame. Reading it needs
Resolve running or the bay scanner, so the reader is a seam. `ScriptingTimelines` reads and writes through
the Resolve scripting API. Its one write is the stamp marker (ADR 0002, rule 10 exception): add it, update
its custom data for a re-stamp, read it back. It never deletes a marker and saves only a project it changed. It
closes every project it loads (Resolve locks a loaded project until it is closed) and makes the database that
was current before current again.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Protocol

FIRST_FRAME = 0  # marker frame ids count from the timeline's start


@dataclass(frozen=True, slots=True, kw_only=True)
class ResolveTimeline:
    """One timeline in one Resolve project. `uuid` changes on duplicate and import; `stamp` is None when unstamped."""

    uuid: str
    name: str
    stamp: str | None


class TimelineReader(Protocol):
    def timelines(self, resolve_project: str) -> tuple[ResolveTimeline, ...] | None:
        """The timelines of the Resolve project `<database>/<folder>/<project>`, or None when it cannot be read."""
        ...


@dataclass(frozen=True, slots=True)
class StampFailure:
    """Why the timelines could not be read or the stamp was not written."""

    reason: str


def scripting_resolve() -> Any:
    """The running Resolve app from `DaVinciResolveScript` (shipped with Resolve Studio), or None when Resolve is not
    running. A module that cannot be imported raises ImportError: that is a broken install, not Resolve being closed."""
    return importlib.import_module("DaVinciResolveScript").scriptapp("Resolve")


class ScriptingTimelines:
    """The real `TimelineReader`, and the stamp writer. `resolve` returns the Resolve app object, or None
    when Resolve is not running; production passes `scripting_resolve`, tests a fake."""

    def __init__(self, resolve: Callable[[], Any] = scripting_resolve) -> None:
        self._resolve = resolve

    def timelines(self, resolve_project: str) -> tuple[ResolveTimeline, ...] | None:
        found = self.read(resolve_project)
        return None if isinstance(found, StampFailure) else found

    def read(self, resolve_project: str) -> tuple[ResolveTimeline, ...] | StampFailure:
        with self._opened(resolve_project) as loaded:
            if isinstance(loaded, StampFailure):
                return loaded
            return tuple(_timeline(t) for t in _scripted(loaded[1]))

    def stamp(self, resolve_project: str, timeline: str, stamp: str, *, replaces: str | None = None,
              canonical: str | None = None) -> ResolveTimeline | StampFailure:
        """Write `stamp` on the first frame of the timeline named `timeline` (or with that unique id), read it
        back and save. A timeline already carrying `stamp` is left as it is. `replaces` re-stamps a duplicate:
        the timeline's current stamp, which must also be on another timeline of the project. `canonical` is the
        unique id of the canonical Timeline, which is never the target."""
        with self._opened(resolve_project) as loaded:
            if isinstance(loaded, StampFailure):
                return loaded
            return self._stamp(*loaded, timeline, stamp, replaces, canonical)

    def _stamp(self, manager: Any, project: Any, timeline: str, stamp: str, replaces: str | None, canonical: str | None) -> ResolveTimeline | StampFailure:
        scripted = [(t, _timeline(t)) for t in _scripted(project)]
        matches = [(t, seen) for t, seen in scripted if timeline in (seen.name, seen.uuid)]
        if not matches:
            return StampFailure("No timeline of that name or Resolve ID in the Resolve project")
        if len(matches) > 1:
            return StampFailure("More than one timeline of that name; give its Resolve ID")
        target, seen = matches[0]
        if canonical is not None and seen.uuid == canonical:
            return StampFailure("That is the canonical Timeline; T6 does not re-stamp it")
        others = [s for _, s in scripted if s.uuid != seen.uuid]
        if any(s.stamp == stamp for s in others):
            return StampFailure("The stamp is already on another timeline of the Resolve project")
        if seen.stamp == stamp:
            return seen
        if seen.stamp is None:
            if FIRST_FRAME in (target.GetMarkers() or {}):
                return StampFailure("The first frame has a marker without a stamp; T6 does not change it")
            written = target.AddMarker(FIRST_FRAME, "Blue", "T6", "T6 Timeline stamp", 1, stamp)
        elif replaces is None or seen.stamp != replaces:
            return StampFailure("The timeline carries a different stamp")
        elif not any(s.stamp == replaces for s in others):
            return StampFailure("Only a duplicate is re-stamped; no other timeline carries that stamp")
        else:
            written = target.UpdateMarkerCustomData(FIRST_FRAME, stamp)
        if not written:
            return StampFailure("Resolve refused the marker write")
        back = target.GetMarkerCustomData(FIRST_FRAME)
        if back != stamp:
            return StampFailure(f"Read-back mismatch: the first-frame marker holds {back!r}")
        if not manager.SaveProject():
            return StampFailure("The marker was written but Resolve did not save the project")
        return _timeline(target)

    @contextmanager
    def _opened(self, resolve_project: str) -> Iterator[tuple[Any, Any] | StampFailure]:
        """Load `<database>/<folder>/.../<project>` for the block: the project manager and the project, or why not.
        After the block the project is closed, unless it was already the current project, and the database that was
        current before is made current again."""
        parts = resolve_project.split("/")
        if len(parts) < 2 or not all(parts):
            yield StampFailure("The Resolve project ref must be <database>/<folder>/<project>")
            return
        resolve = self._resolve()
        if resolve is None:
            yield StampFailure("Resolve is not running")
            return
        manager = resolve.GetProjectManager()
        database, name = parts[0], parts[-1]
        before = manager.GetCurrentDatabase() or {}
        current = manager.GetCurrentProject()
        was_current = before.get("DbName") == database and bool(current) and current.GetName() == name
        loaded = self._load(manager, database, parts[1:-1], name)
        try:
            yield loaded if isinstance(loaded, StampFailure) else (manager, loaded)
        finally:
            if not isinstance(loaded, StampFailure) and not was_current:
                manager.CloseProject(loaded)
            if before and (manager.GetCurrentDatabase() or {}).get("DbName") != before.get("DbName"):
                manager.SetCurrentDatabase(before)

    @staticmethod
    def _load(manager: Any, database: str, folders: list[str], name: str) -> Any:
        """The project `name` in `database`/`folders`, loaded, or why not."""
        if (manager.GetCurrentDatabase() or {}).get("DbName") != database:
            wanted = [d for d in manager.GetDatabaseList() or [] if d.get("DbName") == database]
            if not wanted or not manager.SetCurrentDatabase(wanted[0]):
                return StampFailure(f"The Resolve database {database!r} is missing or cannot be opened")
        if not manager.GotoRootFolder() or not all(manager.OpenFolder(f) for f in folders):
            return StampFailure("The Resolve project folder is missing")
        if name not in (manager.GetProjectListInCurrentFolder() or []):
            return StampFailure("The Resolve project is missing")
        project = manager.LoadProject(name)
        if not project:
            return StampFailure("The Resolve project cannot be loaded (locked or open elsewhere)")
        return project


def _scripted(project: Any) -> list[Any]:
    return [project.GetTimelineByIndex(i) for i in range(1, int(project.GetTimelineCount()) + 1)]


def _timeline(scripted: Any) -> ResolveTimeline:
    return ResolveTimeline(uuid=scripted.GetUniqueId(), name=scripted.GetName(), stamp=scripted.GetMarkerCustomData(FIRST_FRAME) or None)
