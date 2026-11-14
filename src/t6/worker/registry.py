"""Client for the server's Project registry routes a worker uses, over a `Transport`."""

from __future__ import annotations

from collections.abc import Mapping
import json
from typing import Any

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

    def _call(self, method: str, path: str, body: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
        status, answer = self._transport.send(
            method, path, self._headers, json.dumps(body).encode() if body is not None else b""
        )
        if status == 404:
            return None
        if status != 200:
            raise RegistryRejected(status, str(answer.get("error", answer)))
        return answer

    def project(self, project_id: str) -> dict[str, Any] | None:
        """The Project as the registry has it now, or None when there is none."""
        answer = self._call("GET", f"/registry/projects/{project_id}")
        return None if answer is None else answer["project"]

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

    def observed(self, page_id: str, *, available: bool, shown: Mapping[str, str | None] | None = None) -> str:
        """Report a page's current Notion state. Returns the registry's outcome."""
        answer = self._call(
            "POST",
            f"/registry/pages/{page_id}/observed",
            {"available": available, "shown": None if shown is None else dict(shown)},
        )
        return "" if answer is None else str(answer["outcome"])

    def jo_sync_state(self, intent_id: int) -> dict[str, Any] | None:
        answer = self._call("GET", f"/registry/jo-sync/{intent_id}")
        return None if answer is None else answer["state"]

    def begin_jo_create(self, intent_id: int) -> bool:
        answer = self._call("POST", f"/registry/jo-sync/{intent_id}", {"begin": True})
        return bool(answer and answer["started"])

    def report_jo_sync(self, intent_id: int, *, page_id: str | None = None, error: str | None = None) -> None:
        self._call("POST", f"/registry/jo-sync/{intent_id}", {"page_id": page_id, "error": error})
