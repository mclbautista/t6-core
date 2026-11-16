"""Dismiss a dead internal job: an operator's audited way to clear the failed count (#414).

Covers the internal jobs in the t6 database (`internal_jobs`) only. The webhook jobs on the VPS edge live in the edge's
own queue file; `JobQueue.dismiss` and `t6 admin queue` cover those (see `docs/deployment.md`).

A job is never run again. Dismissing changes its state from `dead` to `dismissed`, keeps its row, its stored failure and
its dedupe key, and writes an audit entry with the operator and the reason in the same transaction. Like every write, it
is previewed and then confirmed with a token and an idempotency key.
"""
from __future__ import annotations

import json
from typing import Any

from t6 import verbflow
from t6.preview import PreviewTokens
from t6.registry.sqlite import SqliteProjectRegistry
from t6.verbs import Outcome, Refused
from t6.worker.internal_queue import InternalJobQueue, JobRecord


def _failure(job: JobRecord) -> Any:
    result = job.result
    return result.get('value') if isinstance(result, dict) else None


class JobDismissals:
    def __init__(self, registry: SqliteProjectRegistry, tokens: PreviewTokens) -> None:
        self.registry, self.tokens = registry, tokens
        self.queue = InternalJobQueue(registry, clock=registry._clock)

    def failed(self) -> list[dict[str, Any]]:
        """The dead jobs, oldest first: what an operator picks a job to dismiss from."""
        return [{'job': job.job_id, 'queue': job.queue, 'kind': job.kind, 'dedupe_key': job.dedupe_key, 'attempts': job.attempts, 'failure': _failure(job)}
                for job in self.queue.dead()]

    def write(self, values: dict[str, Any], *, actor: str, idempotency_key: str | None = None, confirm: str | None = None) -> Outcome:
        reason = values.get('reason')
        if type(values.get('job')) is not int or not isinstance(reason, str) or not reason.strip():
            return Refused(rule='reason', reason='A job and a reason are required')
        values = {'job': values['job'], 'reason': reason.strip()}
        request = verbflow.begin(self.tokens, 'queue dismiss', actor, values, idempotency_key, confirm)
        if isinstance(request, Refused):
            return request
        with self.registry._transaction() as db:
            settled = verbflow.settle(db, self.tokens, request)
            if settled:
                return settled
            # A dead transcribe job still has to fail its Transcript: do it now, so the dismissal never hides it from that sweep.
            from t6.transcript import TranscriptStore
            TranscriptStore(self.registry).fail_dead_runs(_db=db)
            job = self.queue.get(values['job'], db=db)
            if job is None:
                return Refused(rule='job', reason='Job not found')
            if job.state != 'dead':
                return Refused(rule='state', reason='Only a failed (dead) internal job can be dismissed')
            plan = {'job': job.job_id, 'queue': job.queue, 'kind': job.kind, 'dedupe_key': job.dedupe_key, 'failure': _failure(job),
                    'reason': values['reason'], 'state': 'dismissed', 'idempotency_key': request.key}
            if not request.nonce:
                return verbflow.issue(self.tokens, request, plan)[0]
            self.queue.dismiss(job.job_id, db=db)
            now = self.registry._clock()
            db.execute('INSERT INTO audit (project_id, action, actor, at, before, after) VALUES (?, ?, ?, ?, ?, ?)',
                       (f'internal-job:{job.job_id}', 'queue_dismiss', actor, now,
                        json.dumps({'state': 'dead', 'kind': job.kind, 'dedupe_key': job.dedupe_key, 'failure': _failure(job)}), json.dumps(plan)))
            return verbflow.finish(db, request, plan)
