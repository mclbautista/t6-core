"""The Resolve worker's side of `resolve.stamp` jobs: an API client and the one handler. It never opens the t6 database."""
from __future__ import annotations

import importlib.util
from collections.abc import Collection
from typing import Protocol

from t6.domain.events import JsonValue
from t6.resolve.timelines import ResolveTimeline, StampFailure
from t6.worker.client import QueueClient, Transport, WorkerJob
from t6.worker.runner import Handler, Refused


class StampWriter(Protocol):
    def stamp(self, resolve_project: str, timeline: str, stamp: str, *, replaces: str | None = None) -> ResolveTimeline | StampFailure: ...


class ResolveQueue:
    """Claims and finishes `resolve.stamp` jobs through `/v1/resolve/*`. Every failure is final: `retry_in` is ignored."""

    def __init__(self, transport: Transport, secret: str) -> None:
        self.client = QueueClient(transport, secret)

    def claim(self, queue: str, lease_seconds: float = 300.0, kinds: Collection[str] | None = None) -> WorkerJob | None:
        data = self.client._call('POST', '/v1/resolve/claim', {})['job']
        return None if data is None else WorkerJob(data['job_id'], queue, data['kind'], data['dedupe_key'], data['payload'], data['token'], data['attempt'])

    def ack(self, job: WorkerJob, result: JsonValue = None) -> None:
        self.client._call('POST', '/v1/resolve/report', {'job_id': job.job_id, 'token': job.token, 'data': result})

    def extend(self, job: WorkerJob, lease_seconds: float = 300.0) -> None:
        self.client._call('POST', '/v1/resolve/extend', {'job_id': job.job_id, 'token': job.token})

    def fail(self, job: WorkerJob, reason: str, retry_in: float | None = None) -> None:
        self.client._call('POST', '/v1/resolve/fail', {'job_id': job.job_id, 'token': job.token, 'reason': reason})


def stamp_handler(writer: StampWriter) -> Handler:
    """Write the job's stamp; the timeline as Resolve shows it after, or `Refused` with the adapter's reason."""
    def run(job: WorkerJob) -> JsonValue:
        payload = job.payload
        replaces = payload.get('replaces')
        written = writer.stamp(str(payload['resolve_project']), str(payload['timeline']), str(payload['stamp']), replaces=None if replaces is None else str(replaces))
        if isinstance(written, StampFailure):
            raise Refused(written.reason)
        return {'uuid': written.uuid, 'name': written.name, 'stamp': written.stamp}
    return run


def configured() -> dict[str, Handler]:
    """The Resolve worker's handlers; ValueError when `DaVinciResolveScript` cannot be imported (see the README)."""
    if importlib.util.find_spec('DaVinciResolveScript') is None:
        raise ValueError('DaVinciResolveScript is not importable; set PYTHONPATH, RESOLVE_SCRIPT_API and RESOLVE_SCRIPT_LIB')
    from t6.resolve.timelines import ScriptingTimelines
    return {'resolve.stamp': stamp_handler(ScriptingTimelines())}
