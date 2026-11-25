"""Build `privacy_registry.json` from Notion: the one command and the worker job share this code (#534)."""
from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, time, timedelta
import json
import os
from pathlib import Path
from typing import Any

from t6.domain.events import JobKind, JsonValue
from t6.domain.studio_time import MANILA
from t6.notion.reads import ContactReader, ProjectReader, StudioReader
from t6.notion.source import NotionSource
from t6.server.queue import MAIN_QUEUE
from t6.worker.internal_queue import InternalJobQueue

DAILY_REBUILD_PREFIX = "privacy_registry.rebuild:daily:"
REBUILD_HOUR = 4


def _now_manila() -> datetime:
    return datetime.now(MANILA)


def next_rebuild_day(now: datetime) -> date:
    """The Manila date of the next 04:00 privacy registry rebuild at or after `now`."""
    manila = now.astimezone(MANILA)
    if manila.time() < time(REBUILD_HOUR, 0):
        return manila.date()
    return manila.date() + timedelta(days=1)


def schedule_daily_rebuild(
    queue: InternalJobQueue,
    on: date | None = None,
    *,
    now: Callable[[], datetime] | None = None,
) -> None:
    """Queue the 04:00 Asia/Manila `privacy_registry.rebuild` job for `on` (or the next 04:00 day)."""
    day = on if on is not None else next_rebuild_day((now or _now_manila)())
    run_at = datetime.combine(day, time(REBUILD_HOUR, 0), MANILA).timestamp()
    queue.submit(
        MAIN_QUEUE,
        JobKind.PRIVACY_REGISTRY_REBUILD.value,
        f"{DAILY_REBUILD_PREFIX}{day.isoformat()}",
        {},
        run_at=run_at,
    )


def build(notion: NotionSource, sources: dict[str, str]) -> dict[str, Any]:
    """`{people, projects, ids}` from the Studios, Contacts (when 'contacts' is a data source) and Projects."""
    studios = StudioReader(notion, sources["studios"]).names()
    projects = ProjectReader(notion, sources["projects"]).list()
    contacts = ContactReader(notion, sources["contacts"]).list() if sources.get("contacts") else ()

    def clean(name: str) -> str:
        return " ".join(name.split())

    people: dict[str, dict[str, Any]] = {clean(name): {"name": clean(name)} for name in studios if name.strip()}
    for contact in contacts:
        if contact.name.strip() and clean(contact.name) not in people:
            people[clean(contact.name)] = {"name": clean(contact.name), "aliases": [clean(a) for a in contact.aliases if a.strip()],
                                           "handles": [contact.telegram_handle] if contact.telegram_handle else []}
    shows: dict[str, dict[str, Any]] = {}
    for project in projects:
        if project.name.strip() and clean(project.name) not in shows:
            shows[clean(project.name)] = {"name": clean(project.name), **({"code": project.project_code} if project.project_code else {})}
    return {"people": list(people.values()), "projects": list(shows.values()), "ids": []}


def write(path: Path, payload: dict[str, Any]) -> None:
    """Replace the file in one step, so a reader never sees half of it."""
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # names inside: owner only from the first byte
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        file.write(json.dumps(payload, ensure_ascii=False) + "\n")
    os.chmod(temporary, 0o600)  # a leftover .tmp keeps its old mode through O_CREAT
    os.replace(temporary, path)


class Rebuild:
    """The main worker's `privacy_registry.rebuild` handler: read Notion, replace the file the API reloads from.

    It returns and logs counts only; a name never reaches the job result, the log or Audit.
    """

    def __init__(
        self,
        notion: NotionSource,
        sources: dict[str, str],
        output: Path,
        *,
        queue: InternalJobQueue | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._notion, self._sources, self._output = notion, sources, output
        self._queue, self._now = queue, now

    def __call__(self, job: object) -> dict[str, JsonValue]:
        if self._queue is not None:
            current_now = self._now or _now_manila
            next_day = next_rebuild_day(current_now())
            dedupe_key = getattr(job, "dedupe_key", None)
            if isinstance(dedupe_key, str) and dedupe_key.startswith(DAILY_REBUILD_PREFIX):
                try:
                    job_day = date.fromisoformat(dedupe_key[len(DAILY_REBUILD_PREFIX):])
                except ValueError:
                    job_day = None
                if job_day is not None:
                    next_day = max(next_day, job_day + timedelta(days=1))
            schedule_daily_rebuild(self._queue, next_day, now=current_now)
        payload = build(self._notion, self._sources)
        write(self._output, payload)
        return {"people": len(payload["people"]), "projects": len(payload["projects"])}
