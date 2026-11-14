"""Show registry-owned Project values in Notion, and reconcile Notion with the registry.

Two job handlers for the main worker (needs the `notion` extra):

- `registry.sync`: a Project changed in the registry. Write its *current*
  state to its active Notion page, whatever revision the job was queued for,
  so an old or repeated job can never put old values back.
- `notion.page_changed`: Notion says a page changed. Read the page as it is
  now and tell the registry what it shows. The registry decides what that
  means; this handler registers nothing and changes no Project.

Both read current state and are safe to run twice or out of order.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Final
from urllib.parse import unquote

from t6.domain.events import JobKind, JsonValue
from t6.notion import codec
from t6.notion.errors import NotionError
from t6.notion.editor import editor as notion_editor
from t6.notion.models import ProjectsProperties
from t6.notion.sync import NotionClient
from t6.registry import normalize_page_id
from t6.worker.client import WorkerJob
from t6.worker.registry import RegistryClient
from t6.worker.runner import Handler

JSON = dict[str, Any]

T6_ID: Final = "T6 ID"  # rich_text
ROTATION: Final = "Rotation"  # select
"""Projection properties on the Projects data source, beside `Project Code`.
The registry owns their values: an edit in Notion is put back."""

__all__ = ["ROTATION", "T6_ID", "ProjectProjection"]


class ProjectProjection:
    def __init__(
        self,
        notion: NotionClient,
        registry: RegistryClient,
        projects_data_source_id: str,
        offer: Callable[[str, str], None] | None = None,
    ) -> None:
        self._offer = offer
        self._notion = notion
        self._registry = registry
        self._projects = normalize_page_id(projects_data_source_id)

    def handlers(self) -> dict[str, Handler]:
        return {JobKind.REGISTRY_SYNC.value: self.sync, JobKind.NOTION_PAGE_CHANGED.value: self.reconcile}

    def sync(self, job: WorkerJob) -> JsonValue:
        project_id = str(job.payload["project_id"])
        project = self._registry.project(project_id)
        if project is None:
            return {"skipped": "no such project"}
        revision = int(project["revision"])
        page = next((b["page_id"] for b in project["bindings"] if b["status"] == "active"), None)
        if page is None:
            self._registry.report_sync(project_id, revision, ok=True)
            return {"synced": revision, "page": None}
        body = {
            "properties": {
                T6_ID: {"rich_text": [{"text": {"content": project["id"]}}]},
                ProjectsProperties.PROJECT_CODE: {"select": {"name": project["code"]} if project["code"] else None},
                ROTATION: {"select": {"name": project["rotation"]}},
            }
        }
        try:
            before = self._notion.retrieve_page(page)
            if _gone(before):
                raise NotionError(404, "object_not_found", "the page is in the trash")
            self._notion.update_page(page, body)
        except NotionError as error:
            if error.status == 404:
                self._registry.observed(page, available=False)
                self._registry.report_sync(project_id, revision, ok=False, detail="page unavailable", final=True)
                return {"unavailable": page}
            self._registry.report_sync(project_id, revision, ok=False, detail=error.code, attempt=job.attempt)
            raise
        self._registry.projection(page, {'t6_id': str(project['id']), 'code': project['code'], 'rotation': project['rotation']}, record=True)
        status = codec.select((before.get('properties') or {}).get('Status'))
        # Initialize the status baseline only if none has been observed yet.
        health = codec.select((before.get('properties') or {}).get('Health'))
        self._registry.projection(page, {'project_status': status, 'project_health': health}, initialize=True)
        self._registry.report_sync(project_id, revision, ok=True)
        return {"synced": revision, "page": page}

    def reconcile(self, job: WorkerJob) -> JsonValue:
        page_id = str(job.payload["page_id"])
        try:
            page = self._notion.retrieve_page(page_id)
        except NotionError as error:
            if error.status != 404:
                raise
            return {"outcome": self._registry.observed(page_id, available=False)}
        if _gone(page):
            return {"outcome": self._registry.observed(page_id, available=False)}
        parent = page.get("parent") or {}
        if normalize_page_id(str(parent.get("data_source_id", ""))) != self._projects:
            return {"outcome": "not_a_project_page"}
        properties: JSON = page.get("properties") or {}
        shown = {
            "t6_id": codec.rich_text(properties.get(T6_ID)),
            "code": codec.select(properties.get(ProjectsProperties.PROJECT_CODE)),
            "rotation": codec.select(properties.get(ROTATION)),
        }
        editor, editor_id = notion_editor(self._notion, job)
        updated = job.payload.get("updated_properties")
        changed = None
        if isinstance(updated, list) and updated:
            changed = [key for key, name in (("t6_id", T6_ID), ("code", ProjectsProperties.PROJECT_CODE), ("rotation", ROTATION))
                       if any(unquote(str(item)) in (name, unquote(str((properties.get(name) or {}).get("id", "")))) for item in updated)]
        outcome = self._registry.observed(page_id, available=True, shown=shown, editor=editor, changed=changed, editor_id=editor_id)
        for field, label in (('status', 'Status'), ('health', 'Health')):
            prop = properties.get(label) or {}
            if outcome != 'unbound' and isinstance(updated, list) and updated and any(unquote(str(item)) in (label, unquote(str(prop.get('id', '')))) for item in updated) and prop:
                self._registry.observe_project_status(page_id, codec.select(prop), editor, str(job.payload.get('event_id') or job.dedupe_key), editor_id, field)
        if outcome == "unbound" and self._offer is not None:
            heading = next((p for p in properties.values() if p.get("type") == "title"), None)
            self._offer(page_id, codec.title(heading) or "")
        return {"outcome": outcome}


def _gone(page: JSON) -> bool:
    return bool(page.get("in_trash") or page.get("archived"))
