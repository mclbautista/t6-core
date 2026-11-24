"""Keep the CRM Sheet's hidden `Projects` tab current: one "Name · t6p_..." line per open Lead and active Project (#712).

Any change that can alter the list queues one `crm.sheet_sync` job. The job rebuilds the whole column, so a retry or a
replay is harmless. A burst of changes shares one job (a five-minute window).
"""
from __future__ import annotations

import time
from collections.abc import Callable, Collection, Mapping
from datetime import datetime
from typing import Any, Protocol
from urllib.parse import unquote

from t6.domain.studio_time import MANILA
from t6.domain.events import JsonValue
from t6.domain.project_rules import StatusGroup, crm_stage, is_lead, status_gate
from t6.registry.sqlite import latest_stages
from t6.worker import WorkerJob
from t6.worker.internal_queue import InternalJobQueue
from t6.worker.runner import Handler

KIND = 'crm.sheet_sync'
TAB = 'Projects'
HISTORY_KIND = 'crm.history_sync'
HISTORY_TAB = 'History'
HISTORY_HEADER = ('Date', 'Project', 'What changed', 'From', 'To', 'By')
WINDOW_SECONDS = 300


class ColumnWriter(Protocol):
    def replace_column(self, tab: str, values: list[str]) -> None: ...


class RowWriter(Protocol):
    def replace_rows(self, tab: str, rows: list[list[str]]) -> None: ...


class ProjectDropdown:
    """`read_page` gives a Project page's (name, status), or None when it cannot be read.

    Lists each open Lead (a CRM stage other than Lost, see `t6.domain.project_rules.crm_stage`) and each active Project
    (In-Progress or Airing by its Notion Status, its CRM part ended).
    """

    def __init__(self, registry: Any, read_page: Callable[[str], tuple[str, str] | None], writer: ColumnWriter) -> None:
        self._registry, self._read_page, self._writer = registry, read_page, writer

    def lines(self) -> list[str]:
        with self._registry._transaction() as db:
            stages = latest_stages(db)
        found = []
        for project in self._registry.list_projects():
            page = project.notion_page
            shown = None if page is None else self._read_page(page)
            recorded = stages[project.id][0] if project.id in stages else None
            if shown is not None and (is_lead(crm_stage(recorded, shown[1])) or
                                      (recorded != 'Lost' and status_gate(shown[1]) is StatusGroup.IN_PROGRESS)):
                found.append(f'{shown[0].strip() or "(untitled)"} · {project.id}')
        return sorted(found, key=str.casefold)

    def handle(self, job: WorkerJob) -> JsonValue:
        lines = self.lines()
        self._writer.replace_column(TAB, lines)  # a failed write is raised, so the queue retries it
        return {'rows': len(lines)}


class EditHistory:
    """Rewrite the `History` tab from `crm_edit_history`, newest first. Names are stored; the Project is named from its page."""

    def __init__(self, registry: Any, read_page: Callable[[str], tuple[str, str] | None], writer: RowWriter) -> None:
        self._registry, self._read_page, self._writer = registry, read_page, writer

    def rows(self) -> list[list[str]]:
        with self._registry._transaction() as db:
            edits = db.execute('SELECT at, project_id, property, from_value, to_value, actor FROM crm_edit_history ORDER BY id DESC').fetchall()
        names: dict[str, str] = {}
        for project_id in {str(edit[1]) for edit in edits}:
            project = self._registry.get(project_id)
            page = None if project is None else project.notion_page
            shown = None if page is None else self._read_page(page)
            names[project_id] = (shown[0].strip() if shown else '') or project_id
        return [[datetime.fromtimestamp(at, MANILA).strftime('%Y-%m-%d %H:%M'), names[project_id], what, before, after, by]
                for at, project_id, what, before, after, by in edits]

    def handle(self, job: WorkerJob) -> JsonValue:
        rows = self.rows()
        self._writer.replace_rows(HISTORY_TAB, [list(HISTORY_HEADER), *rows])  # a failed write is raised, so the queue retries it
        return {'rows': len(rows)}


class SheetSyncTrigger:
    """Wrap a job handler so that, once it has succeeded, a change that can alter the list queues a rewrite."""

    def __init__(self, queue: InternalJobQueue, *, status_property: str, history_properties: Collection[str] = (),
                 clock: Callable[[], float] = time.time) -> None:
        self._queue, self._status, self._clock = queue, unquote(status_property), clock
        self._history = {unquote(item) for item in history_properties}

    def request(self, kind: str = KIND) -> None:
        bucket = int(self._clock() // WINDOW_SECONDS)
        self._queue.submit('main', kind, str(bucket), run_at=float((bucket + 1) * WINDOW_SECONDS))

    def _history_relevant(self, kind: str, job: WorkerJob, result: JsonValue) -> bool:
        """A Notion edit of Status or of a Lead relation; a page that is not a Project page never is."""
        edited = job.payload.get('updated_properties')
        if kind != 'notion.page_changed' or not isinstance(edited, list) or (isinstance(result, dict) and result.get('outcome') == 'not_a_project_page'):
            return False
        return any(unquote(str(item)) in self._history for item in edited)

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
            if self._history_relevant(kind, job, result):
                self.request(HISTORY_KIND)
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
    from t6.notion.generated import ProjectsRow
    from t6.notion.projection import CRM_RELATIONS
    history_properties = [str(ProjectsRow.notion_id(field)) for field in ('status', *(field for field, _ in CRM_RELATIONS))]
    trigger = SheetSyncTrigger(InternalJobQueue(registry), status_property=status_property, history_properties=history_properties)
    wrapped = {kind: trigger.wrap(handler, kind) for kind, handler in handlers.items()
               if kind in ('registry.sync', 'notion.page_changed')}
    return {**handlers, **wrapped, KIND: ProjectDropdown(registry, read_page, writer).handle,
            HISTORY_KIND: EditHistory(registry, read_page, writer).handle}
