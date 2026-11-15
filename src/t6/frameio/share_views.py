"""Record views of shares attached to Deliveries."""

import time
from collections.abc import Callable

from t6.registry.sqlite import SqliteProjectRegistry


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
                    (row[0], row[1], self._clock(), 'frameio', 'delivery_share_viewed', 'client viewed the delivery share'),
                )
            return True
