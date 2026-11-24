"""Keep the CRM Sheet's hidden `Projects` tab current: one "Name · t6p_..." line per open Lead and active Project (#712).

Any change that can alter the list queues one `crm.sheet_sync` job. The job rebuilds the whole column, so a retry or a
replay is harmless. A burst of changes shares one job (a five-minute window).
"""
from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any, Protocol
from urllib.parse import unquote

from t6.domain.events import JsonValue
from t6.domain.project_rules import StatusGroup, status_gate
from t6.worker import WorkerJob
from t6.worker.internal_queue import InternalJobQueue
from t6.worker.runner import Handler

KIND = 'crm.sheet_sync'
TAB = 'Projects'
WINDOW_SECONDS = 300
_OPEN = {StatusGroup.INCOMING, StatusGroup.IN_PROGRESS}
"""Open Leads are the incoming group (Leads, Negotiations, Proposal Made); active Projects are In-Progress and Airing."""


class ColumnWriter(Protocol):
    def replace_column(self, tab: str, values: list[str]) -> None: ...


class ProjectDropdown:
    """`read_page` gives a Project page's (name, status), or None when it cannot be read."""

    def __init__(self, registry: Any, read_page: Callable[[str], tuple[str, str] | None], writer: ColumnWriter) -> None:
        self._registry, self._read_page, self._writer = registry, read_page, writer

    def lines(self) -> list[str]:
        found = []
        for project in self._registry.list_projects():
            page = next((b.page_id for b in project.bindings if b.system == 'notion' and b.active and b.available), None)
            shown = None if page is None else self._read_page(page)
            if shown is not None and status_gate(shown[1]) in _OPEN:
                found.append(f'{shown[0].strip() or "(untitled)"} · {project.id}')
        return sorted(found, key=str.casefold)

    def handle(self, job: WorkerJob) -> JsonValue:
        lines = self.lines()
        self._writer.replace_column(TAB, lines)  # a failed write is raised, so the queue retries it
        return {'rows': len(lines)}


class SheetSyncTrigger:
    """Wrap a job handler so that, once it has succeeded, a change that can alter the list queues a rewrite."""

    def __init__(self, queue: InternalJobQueue, *, status_property: str, clock: Callable[[], float] = time.time) -> None:
        self._queue, self._status, self._clock = queue, unquote(status_property), clock

    def request(self) -> None:
        bucket = int(self._clock() // WINDOW_SECONDS)
        self._queue.submit('main', KIND, str(bucket), run_at=float((bucket + 1) * WINDOW_SECONDS))

    def _relevant(self, kind: str, job: WorkerJob, result: JsonValue) -> bool:
        if kind != 'notion.page_changed':
            return True
        if isinstance(result, dict) and result.get('outcome') == 'not_a_project_page':
            return False
        edited = job.payload.get('updated_properties')
        if not isinstance(edited, list) or not edited:
            return True  # a new page, or no list of what changed
        return any(unquote(str(item)) in ('title', self._status) for item in edited)

    def wrap(self, handler: Handler, kind: str) -> Handler:
        def run(job: WorkerJob) -> JsonValue:
            result = handler(job)
            if self._relevant(kind, job, result):
                self.request()
            return result
        return run


def install(handlers: Mapping[str, Handler], environ: Mapping[str, str],
            read_page: Callable[[str], tuple[str, str] | None], *, status_property: str = '') -> dict[str, Handler]:
    """The handlers with the Sheet sync added; unchanged when the CRM Sheet is not configured."""
    from t6.crm.sheet_writer import configured_writer
    writer = configured_writer(environ)
    if writer is None or not environ.get('T6_DATABASE'):
        return dict(handlers)
    from t6.registry.sqlite import SqliteProjectRegistry
    registry = SqliteProjectRegistry(environ['T6_DATABASE'])
    trigger = SheetSyncTrigger(InternalJobQueue(registry), status_property=status_property)
    wrapped = {kind: trigger.wrap(handler, kind) for kind, handler in handlers.items()
               if kind in ('registry.sync', 'notion.page_changed')}
    return {**handlers, **wrapped, KIND: ProjectDropdown(registry, read_page, writer).handle}
