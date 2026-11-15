"""Review activity that waits for its Frame.io project to be linked to a show (#272).

The main worker holds a `frameio.review_done` job here when its Frame.io project is not linked to any
Project, and posts one operator card per Frame.io project. When the project is linked (by the card, by
`frameio project link`, or by anything else), the API releases each waiting row once, as a new
`frameio.review_done` job under the key `held:<row id>`. The internal job table dedupes by kind and key, so a
release that crashed half way, or ran twice, still delivers each row once. Nothing is dropped, and no
comment text is stored: the payload is the job's own (file, person, count, times).

Only the API and the main worker open the t6 database, so only they use this. The Frame.io worker never does.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
import json
from typing import Any

from t6.registry.sqlite import SqliteProjectRegistry

__all__ = ['HeldActivity', 'REVIEW_KIND', 'REVIEW_QUEUE']

REVIEW_KIND = 'frameio.review_done'
REVIEW_QUEUE = 'main'

Submit = Callable[[str, str, str, Mapping[str, Any]], object]
"""`InternalJobQueue.submit`'s shape: queue, kind, dedupe key, payload."""


class HeldActivity:
    def __init__(self, registry: SqliteProjectRegistry) -> None:
        self._registry = registry

    def hold(self, frameio_project_id: str, key: str, payload: Mapping[str, Any]) -> bool:
        """Keep one review job until its project is linked. False when the job (by `key`) was already held."""
        with self._registry._transaction() as db:
            made = db.execute(
                'INSERT INTO frameio_held_activity (frameio_project_id, dedupe_key, payload, held_at) VALUES (?, ?, ?, ?) '
                'ON CONFLICT (dedupe_key) DO NOTHING',
                (frameio_project_id, key, json.dumps(dict(payload), sort_keys=True), self._registry._clock()))
            return made.rowcount == 1

    def has(self, key: str) -> bool:
        """True when the job under `key` was held, whether its row is still waiting or already released."""
        with self._registry._transaction() as db:
            return db.execute('SELECT 1 FROM frameio_held_activity WHERE dedupe_key = ?', (key,)).fetchone() is not None

    def release_linked(self, submit: Submit) -> int:
        """Send every waiting row whose Frame.io project is now linked to a Project, once. Returns how many were released.

        The job goes out first and the row is marked after, so a crash between the two sends the same job
        again under the same key, which the job table ignores.
        """
        with self._registry._transaction() as db:
            rows = db.execute(
                'SELECT h.id, h.payload FROM frameio_held_activity h WHERE h.released_at IS NULL AND EXISTS '
                "(SELECT 1 FROM media_locations m WHERE m.kind = 'frameio-project' AND m.ref = h.frameio_project_id AND m.active = 1) "
                'ORDER BY h.id').fetchall()
        released = 0
        for row_id, payload in rows:
            submit(REVIEW_QUEUE, REVIEW_KIND, f'held:{row_id}', json.loads(payload))
            with self._registry._transaction() as db:
                released += db.execute('UPDATE frameio_held_activity SET released_at = ? WHERE id = ? AND released_at IS NULL',
                                       (self._registry._clock(), row_id)).rowcount
        return released
