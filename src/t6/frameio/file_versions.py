"""Record a Version when a file lands in a Deliverable's T6-made Frame.io folder (#265).

The edge queues the raw Frame.io webhook body on the `main` queue as a `frameio.file_versioned` job.
The main worker runs it, because it can open the t6 database; the Frame.io worker cannot, and holds
the only Frame.io token. The webhook names the file but not its folder, so a `FileReader` finds it. For `file.versioned` it names the version stack (#410).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from t6.domain.events import JsonValue
from t6.frameio.model import FileLocation
from t6.frameio.requests import Failed, FileNotFound
from t6.preview import PreviewTokens
from t6.registry.sqlite import SqliteProjectRegistry
from t6.version import VersionStore
from t6.worker.client import WorkerJob

__all__ = ['EVENT_TYPES', 'FileLocation', 'FileNotFound', 'FileReader', 'FrameioFileVersions']

EVENT_TYPES = frozenset({'file.versioned', 'file.upload.completed'})


class FileReader(Protocol):
    def file_location(self, file_id: str) -> FileLocation | Failed:
        """Where the file is, or the stack's folder and files when the ID names a version stack. `FileNotFound`: Frame.io does not know it (the job ends). Any other `Failed` is worth a retry."""
        ...


def _text(body: Mapping[str, JsonValue], *path: str) -> str | None:
    value: Mapping[str, JsonValue] | JsonValue = body
    for step in path:
        value = value.get(step) if isinstance(value, dict) else None
    return value if isinstance(value, str) and value.strip() else None


class FrameioFileVersions:
    def __init__(self, registry: SqliteProjectRegistry, reader: FileReader) -> None:
        self._versions = VersionStore(registry, PreviewTokens('frameio-events'))  # records events; mints no preview
        self._reader = reader

    def handle(self, job: WorkerJob) -> JsonValue:
        """Idempotent by the job's dedupe key. Raises RuntimeError when the file cannot be read (the job is retried)."""
        file_id = _text(job.payload, 'resource', 'id')
        if _text(job.payload, 'type') not in EVENT_TYPES or file_id is None:
            return {'outcome': 'ignored', 'reason': 'not a file version event'}
        key = job.dedupe_key
        if self._versions.event_seen(key):
            return {'outcome': 'known'}
        found = self._reader.file_location(file_id)
        if isinstance(found, FileNotFound):
            return {'outcome': 'ignored', 'reason': 'the file is gone from Frame.io'}
        if isinstance(found, Failed):
            raise RuntimeError(found.reason)
        if found.folder_id is None:
            return {'outcome': 'ignored', 'reason': 'not a T6 folder'}
        if found.file_ids is not None and found.stack_id is not None:  # the resource is a version stack (#410)
            outcome = self._versions.record_stack_from_frameio(key, found.folder_id, found.stack_id, found.file_ids)
            return {'outcome': 'ignored', 'reason': outcome} if outcome == 'not a T6 folder' else {'outcome': outcome}
        outcome = self._versions.record_from_frameio(key, found.folder_id, file_id, found.stack_id)
        return {'outcome': 'ignored', 'reason': outcome} if outcome == 'not a T6 folder' else {'outcome': outcome}
