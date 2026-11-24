"""Move the `main` jobs the API started from its queue file to the internal job table (#358).

Before this change the full-mode API put the sync jobs it starts in its queue file (`T6_QUEUE_DB`),
and no worker claimed from there. ADR 0006 puts work that starts inside T6 in the job table in the t6
database, which the main worker polls. On start the API moves what is still waiting.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable

from t6.server.queue import MAIN_QUEUE, JobQueue
from t6.worker.internal_queue import InternalJobQueue

SYNC_KINDS = frozenset({"registry.sync", "deliverable.sync", "jo.sync", "project.status_sync"})
"""The kinds of `main` job the API starts for the Notion projections."""


def hand_over_inside_jobs(queue: JobQueue, inside: InternalJobQueue, log: Callable[[str], None]) -> dict[str, int]:
    """Move waiting `main` jobs of the four sync kinds into `inside`; return how many moved, by kind.

    The job keeps its kind, dedupe key and payload. The internal table dedupes by kind and key, so
    running this twice, or after a crash between the two steps, moves nothing twice. Waiting jobs of
    other kinds stay where they are and are counted in the log by kind: the live count decides what to
    do with them.
    """
    moved: Counter[str] = Counter()
    left: Counter[str] = Counter()
    for job in queue.waiting(MAIN_QUEUE):
        if job.kind not in SYNC_KINDS:
            left[job.kind] += 1
            continue
        inside.submit(MAIN_QUEUE, job.kind, job.dedupe_key, job.payload)
        if queue.hand_over(job.job_id, "internal_jobs"):
            moved[job.kind] += 1
    if moved:
        log("t6.server: moved waiting jobs to the internal job table: " + ", ".join(f"{k}: {n}" for k, n in sorted(moved.items())))
    if left:
        log("t6.server: waiting jobs left in the queue file: " + ", ".join(f"{k}: {n}" for k, n in sorted(left.items())))
    return dict(moved)
