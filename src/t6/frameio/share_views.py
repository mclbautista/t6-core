"""Record views of shares attached to Deliveries."""

import time
from collections.abc import Callable, Mapping

from t6.domain.events import JsonValue
from t6.person import FRAMEIO_ACTOR
from t6.registry.sqlite import SqliteProjectRegistry
from t6.worker.client import WorkerJob
from t6.worker.runner import Handler, Refused


class DeliveryShareViews:
    def __init__(self, registry: SqliteProjectRegistry, clock: Callable[[], float] = time.time) -> None:
        self._registry = registry
        self._clock = clock

    def record(self, event_id: str, share_id: str) -> bool:
        """Return whether the share is recorded, recording this event at most once."""
        with self._registry._transaction() as db:
            row = db.execute(
                "SELECT d.project_id, p.id FROM delivery_carriers c "
                "JOIN packages p ON p.id = c.package_id "
                "JOIN deliverables d ON d.id = p.deliverable_id "
                "WHERE c.kind = 'frameio-share' AND c.ref = ? AND p.kind = 'delivery' LIMIT 1",
                (share_id,),
            ).fetchone()
            if row is None:
                return False
            receipt = db.execute('INSERT OR IGNORE INTO delivery_share_views VALUES (?)', (event_id,))
            if receipt.rowcount:
                db.execute(
                    'INSERT INTO activity (project_id, subject_id, at, actor, action, summary) VALUES (?, ?, ?, ?, ?, ?)',
                    (row[0], row[1], self._clock(), FRAMEIO_ACTOR, 'delivery_share_viewed', 'client viewed the delivery share'),
                )
            return True


def share_viewed_handler(views: DeliveryShareViews) -> Handler:
    """The main worker's handler for `frameio.share_viewed`.

    The edge queues the Frame.io webhook body as it arrived: the share is
    `resource.id`, and the job's dedupe key names the event, so a redelivery
    writes nothing. Only the API and the main worker open the t6 database, so
    this never runs on the Frame.io worker.
    """
    def handle(job: WorkerJob) -> JsonValue:
        kind = job.payload.get('type')
        if kind is not None and kind != 'share.viewed':
            return {'ignored': str(kind)}
        resource = job.payload.get('resource')
        share_id = resource.get('id') if isinstance(resource, Mapping) else None
        if not isinstance(share_id, str) or not share_id.strip():
            raise Refused('share viewed job without a share id')
        return {'recorded': views.record(job.dedupe_key, share_id)}
    return handle
