"""Client for the server's Project registry routes a worker uses, over a `Transport`."""

from __future__ import annotations

from collections.abc import Mapping
import json
from typing import Any

from t6.media_links import MediaLink
from t6.worker.client import Transport


class RegistryRejected(Exception):
    """The server refused a registry call."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"registry answered {status}: {detail}")
        self.status = status


class RegistryClient:
    def __init__(self, transport: Transport, secret: str) -> None:
        self._transport = transport
        self._headers = {"Authorization": f"Bearer {secret}", "Content-Type": "application/json"}

    def _call(self, method: str, path: str, body: Mapping[str, Any] | None = None, *, missing_is_none: bool = True) -> dict[str, Any] | None:
        status, answer = self._transport.send(
            method, path, self._headers, json.dumps(body).encode() if body is not None else b""
        )
        if status == 404 and missing_is_none:
            return None
        if status != 200:
            raise RegistryRejected(status, str(answer.get("error", answer)))
        return answer

    def project(self, project_id: str) -> dict[str, Any] | None:
        """The Project as the registry has it now, or None when there is none."""
        answer = self._call("GET", f"/registry/projects/{project_id}")
        return None if answer is None else answer["project"]

    def review_chat_links(self, page_id: str) -> list[dict[str, Any]]:
        from urllib.parse import quote
        answer = self._call("GET", f"/registry/review-chat/{quote(page_id, safe='')}")
        return [] if answer is None else answer["links"]

    def media_links(self, kind: str, role: str | None = None) -> tuple[MediaLink, ...]:
        """The active Media locations of `kind` (and `role`), from the server. Needs a grant with registry read."""
        from urllib.parse import quote
        path = f"/registry/media-links/{quote(kind, safe='')}" + (f"/{quote(role, safe='')}" if role else "")
        answer = self._call("GET", path, missing_is_none=False)  # a 404 is a server without the route, never "no links"
        return tuple(
            MediaLink(id=item["id"], owner=item["owner"], project_id=item["project"], kind=item["kind"],
                      ref=item["ref"], role=item["role"], pages=tuple(item["pages"]))
            for item in (answer or {"links": []})["links"]
        )

    def review_version(self, frameio_file_id: str) -> dict[str, Any] | None:
        """The Version a Frame.io file belongs to, or None when t6 has none or this worker may not read it."""
        from urllib.parse import quote
        try:
            answer = self._call("GET", f"/registry/review-version/{quote(frameio_file_id, safe='')}")
        except RegistryRejected as refused:
            if refused.status == 403:
                return None
            raise
        return None if answer is None else answer["version"]

    def report_sync(
        self, project_id: str, revision: int, *, ok: bool, detail: str = "", attempt: int = 1, final: bool = False
    ) -> None:
        """Say that Notion now shows `revision`, or why this attempt did not get there.

        A failure becomes persistent when `final` is set or the job has used its last attempt.
        """
        self._call(
            "POST",
            f"/registry/projects/{project_id}/sync",
            {"revision": revision, "ok": ok, "detail": detail, "attempt": attempt, "final": final},
        )

    def observed(self, page_id: str, *, available: bool, shown: Mapping[str, str | None] | None = None,
                 editor: str | None = None, changed: list[str] | None = None, editor_id: str | None = None) -> str:
        """Report a page's current Notion state. Returns the registry's outcome."""
        answer = self._call(
            "POST",
            f"/registry/pages/{page_id}/observed",
            {"available": available, "shown": None if shown is None else dict(shown), "editor": editor, "changed": changed, "editor_id": editor_id},
        )
        return "" if answer is None else str(answer["outcome"])

    def register_created_page(self, page_id: str, event: str, status: str | None, option_id: str | None,
                              relations: dict[str, list[str]] | None = None) -> str:
        """Register the Projects page a `page.created` event named as a Lead. Returns the outcome."""
        answer = self._call("POST", f"/registry/pages/{page_id}/register",
                            {"event": event, "status": status, "option_id": option_id, "relations": relations or {}})
        return "" if answer is None else str(answer["outcome"])

    def deliverable_sync_state(self, intent_id: int) -> dict[str, Any] | None:
        answer = self._call('GET', f'/registry/deliverable-sync/{intent_id}')
        return answer['state'] if answer else None

    def begin_deliverable_create(self, intent_id: int) -> bool:
        answer = self._call('POST', f'/registry/deliverable-sync/{intent_id}', {'begin': True})
        return bool(answer and answer['started'])

    def report_deliverable_sync(self, intent_id: int, *, page_id: str | None = None, error: str | None = None) -> None:
        self._call('POST', f'/registry/deliverable-sync/{intent_id}', {'page_id': page_id, 'error': error})

    def jo_sync_state(self, intent_id: int) -> dict[str, Any] | None:
        answer = self._call("GET", f"/registry/jo-sync/{intent_id}")
        return None if answer is None else answer["state"]

    def status_sync_state(self, intent_id: int) -> dict[str, Any] | None:
        answer = self._call('GET', f'/registry/status-sync/{intent_id}')
        return None if answer is None else answer['state']

    def report_status_sync(self, intent_id: int) -> None:
        self._call('POST', f'/registry/status-sync/{intent_id}', {})

    def begin_jo_create(self, intent_id: int) -> bool:
        answer = self._call("POST", f"/registry/jo-sync/{intent_id}", {"begin": True})
        return bool(answer and answer["started"])

    def report_jo_sync(self, intent_id: int, *, page_id: str | None = None, error: str | None = None) -> None:
        self._call("POST", f"/registry/jo-sync/{intent_id}", {"page_id": page_id, "error": error})

    def observe_deliverable(self, page_id: str, shown: Mapping[str, str | None], changed: list[str], editor: str, event: str, *, editor_id: str | None = None) -> dict[str, Any]:
        return self._call('POST', f'/registry/notion-deliverable/{page_id}', {'shown': dict(shown), 'changed': changed, 'editor': editor, 'event': event, 'editor_id': editor_id}) or {}

    def observe_job_order(self, page_id: str, shown: Mapping[str, str | None], changed: list[str], editor: str, event: str, *, editor_id: str | None = None) -> dict[str, Any]:
        return self._call("POST", f"/registry/notion-jo/{page_id}", {"shown": dict(shown), "changed": changed, "editor": editor, "event": event, "editor_id": editor_id}) or {}

    def projection(self, page_id: str, values: Mapping[str, str | None], *, record: bool = False, initialize: bool = False) -> dict[str, Any]:
        return self._call("POST", f"/registry/notion-projection/{page_id}", {"values": dict(values), "record": record, "initialize": initialize}) or {}

    def observe_project_status(self, page_id: str, status: str | None, editor: str, event: str, editor_id: str | None, field: str = "status", option_id: str | None = None) -> dict[str, Any]:
        return self._call("POST", f"/registry/notion-project-status/{page_id}", {"status": status, "editor": editor, "event": event, "editor_id": editor_id, "field": field, "option_id": option_id}) or {}

    def observe_crm_edit(self, page_id: str, label: str, names: list[str], editor: str, event: str) -> dict[str, Any]:
        return self._call('POST', f'/registry/notion-crm-edit/{page_id}', {'property': label, 'names': names, 'editor': editor, 'event': event}) or {}

    def observe_project_dates(self, page_id: str, dates: Mapping[str, str | None], coordinators: list[str]) -> dict[str, Any]:
        return self._call('POST', f'/registry/notion-project-dates/{page_id}', {'dates': dict(dates), 'coordinators': coordinators}) or {}
