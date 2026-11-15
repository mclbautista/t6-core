"""Hold review activity for a Frame.io project no show owns, and ask for the link (#272)."""
from __future__ import annotations

from collections.abc import Callable

from t6.domain.events import JsonValue
from t6.frameio_hold import HeldActivity
from t6.worker.client import WorkerJob


class HoldUnlinked:
    """The main worker's `hold` for `ReviewMessage`.

    It keeps the job in the t6 database (the main worker opens it) and asks the API to offer one operator
    card for the Frame.io project. The card is offered once per project while it is live, however many events
    wait. If the offer fails the job raises and is tried again; holding the same job twice keeps one row.
    """

    def __init__(self, held: HeldActivity, offer: Callable[[str], None]) -> None:
        self._held = held
        self._offer = offer

    def __call__(self, job: WorkerJob, frameio_project_id: str) -> JsonValue:
        self._held.hold(frameio_project_id, job.dedupe_key, job.payload)
        self._offer(frameio_project_id)
        return {"held": True, "frameio_project_id": frameio_project_id}
