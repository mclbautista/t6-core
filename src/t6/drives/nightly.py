"""One internal reload per Manila night, using the operator reload workflow."""
from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from t6.domain.events import JsonValue
from t6.drives.practice import practice_mode
from t6.drives.reload import reload_drives
from t6.drives.sheets import configured_sources
from t6.preview import PreviewTokens
from t6.registry.sqlite import SqliteProjectRegistry
from t6.verbs import Done, Preview, Refused
from t6.worker import WorkerJob
from t6.worker.internal_queue import InternalJobQueue
from t6.worker.runner import Refused as JobRefused


class NightlyReload:
    def __init__(self, registry: SqliteProjectRegistry, tokens: PreviewTokens, queue: InternalJobQueue, *,
                 clock: Callable[[], float] = time.time, environ: Mapping[str, str] | None = None) -> None:
        import os
        self.registry, self.tokens, self.queue = registry, tokens, queue
        self.clock, self.environ = clock, os.environ if environ is None else environ

    def schedule(self) -> None:
        if not practice_mode(self.registry):
            return
        tomorrow = datetime.fromtimestamp(self.clock(), ZoneInfo('Asia/Manila')).date() + timedelta(days=1)
        midnight = datetime.combine(tomorrow, datetime.min.time(), ZoneInfo('Asia/Manila'))
        self.queue.submit('main', 'drive.reload', tomorrow.isoformat(), run_at=midnight.timestamp())

    def handle(self, job: WorkerJob) -> JsonValue:
        if not practice_mode(self.registry):
            return {'practice': False, 'nightly_enabled': False}
        self.schedule()  # A failed read must not end the nightly chain.
        try:
            source, tracker = configured_sources(self.environ)
            key = f'drive-nightly:{job.job_id}'
            preview = reload_drives(self.registry, self.tokens, source, tracker=tracker, confirm=None, actor='drive-nightly', idempotency_key=key)
            if isinstance(preview, Refused):
                raise JobRefused(preview.reason)
            assert isinstance(preview, Preview)
            result = reload_drives(self.registry, self.tokens, source, tracker=tracker, confirm=preview.token, actor='drive-nightly', idempotency_key=key)
            if isinstance(result, Refused):
                raise JobRefused(result.reason)
            assert isinstance(result, Done)
            return result.result  # type: ignore[no-any-return]
        except (OSError, ValueError) as error:
            raise JobRefused(str(error)) from error
