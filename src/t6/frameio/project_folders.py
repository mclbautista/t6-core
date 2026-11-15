"""The caller's side of Project Start's Frame.io step (#275): ask the Frame.io worker for the project and folders.

The caller holds no Frame.io credentials. It submits a `CreateProjectFolders` request to the `frameio` queue and
waits for the Frame.io worker's answer (`t6.frameio.ask`). The request is safe to repeat: the worker finds what
exists by the stored project id or by exact name, so a timeout, a crash or a second confirm creates nothing new.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import time
from typing import Protocol

from t6.domain.events import JsonValue
from t6.frameio.requests import CreateProjectFolders, Failed, Ok, RequestQueue, ask
from t6.server.queue import JobQueue

DEFAULT_TIMEOUT_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class ProjectFolders:
    """What the Frame.io worker answered: the project, and the folder id of each Deliverable code."""

    frameio_project_id: str
    folders: Mapping[str, str]
    created_project: bool
    created_folders: tuple[str, ...]

    @classmethod
    def from_json(cls, raw: Mapping[str, JsonValue]) -> ProjectFolders:
        project, folders, created = raw.get("frameio_project_id"), raw.get("folders"), raw.get("created_folders")
        if (
            not isinstance(project, str)
            or not project
            or not isinstance(folders, dict)
            or not all(isinstance(k, str) and isinstance(v, str) and v for k, v in folders.items())
            or not isinstance(created, list)
        ):
            raise ValueError("the Frame.io worker's answer is not a project with folders")
        return cls(project, {str(k): str(v) for k, v in folders.items()}, raw.get("created_project") is True, tuple(str(c) for c in created))


class FrameioProjectWriter(Protocol):
    def create_project_folders(
        self, project_id: str, name: str, folders: tuple[str, ...], *, known_project_id: str | None = None, taken: tuple[str, ...] = ()
    ) -> ProjectFolders | Failed:
        """Make the Frame.io project and one folder per name when missing; `Failed` carries the reason and creates no duplicate."""
        ...


class LocalFrameioQueue:
    """The server's own job queue, for a process (the API) that holds it."""

    def __init__(self, queue: JobQueue) -> None:
        self._queue = queue

    def submit(self, queue: str, kind: str, dedupe_key: str, payload: Mapping[str, JsonValue] | None = None) -> int:
        return self._queue.submit(queue, kind, dedupe_key, payload).job_id

    def result(self, queue: str, job_id: int) -> tuple[str, JsonValue] | None:
        found = self._queue.result(job_id, queue)
        return None if found is None else (found.status, found.value)


class QueueFrameioWriter:
    def __init__(
        self,
        queue: RequestQueue,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._queue, self._timeout, self._clock, self._sleep = queue, timeout, clock, sleep

    def create_project_folders(
        self, project_id: str, name: str, folders: tuple[str, ...], *, known_project_id: str | None = None, taken: tuple[str, ...] = ()
    ) -> ProjectFolders | Failed:
        try:
            request = CreateProjectFolders(project_id=project_id, name=name, folders=folders, known_project_id=known_project_id, taken=taken)
        except ValueError as error:
            return Failed(f"bad request: {error}")
        answer = ask(self._queue, request, timeout=self._timeout, clock=self._clock, sleep=self._sleep)
        if not isinstance(answer, Ok):
            return answer
        try:
            return ProjectFolders.from_json(answer.value) if isinstance(answer.value, dict) else Failed("the Frame.io worker's answer is not an object")
        except ValueError as error:
            return Failed(str(error))
