"""Daily Manila morning reminder, delivered through the worker gateway."""
from __future__ import annotations

import html
import time
from collections.abc import Callable
from datetime import date, datetime, time as daytime, timedelta
from zoneinfo import ZoneInfo

from t6.domain.events import JsonValue
from t6.drive_reads import overdue
from t6.drives.practice import practice_mode
from t6.drives.sqlite import SqliteDriveStore
from t6.person import People
from t6.worker import WorkerJob
from t6.worker.gateway import GatewayClient
from t6.worker.internal_queue import InternalJobQueue
from t6.worker.runner import Refused


class MorningReminder:
    def __init__(self, people: People, queue: InternalJobQueue, gateway: GatewayClient, custodian: str, *,
                 clock: Callable[[], float] = time.time) -> None:
        self.people, self.queue, self.gateway, self.custodian = people, queue, gateway, custodian
        self.clock = clock

    def schedule(self, on: date | None = None) -> None:
        day = on or datetime.fromtimestamp(self.clock(), ZoneInfo('Asia/Manila')).date()
        morning = datetime.combine(day, daytime(8), ZoneInfo('Asia/Manila'))
        self.queue.submit('main', 'drive.reminder', day.isoformat(), run_at=morning.timestamp())

    def handle(self, job: WorkerJob) -> JsonValue:
        today = datetime.fromtimestamp(self.clock(), ZoneInfo('Asia/Manila')).date()
        self.schedule(today + timedelta(days=1))
        if job.dedupe_key != today.isoformat():
            self.schedule(today)
            return {'skipped': 'obsolete date'}
        with self.people.registry._transaction() as db:
            result = overdue(SqliteDriveStore(self.people.registry, within=db), today)
        result.practice = practice_mode(self.people.registry)
        if result.overdue:
            person = self.people.get(self.custodian)
            if person is None or not person['active'] or not person['telegram_id'] or int(person['telegram_id']) <= 0:
                raise Refused('Drive custodian must be an active Person with a Telegram ID')
            lines = ['T6 Drives OUT for more than 14 days']
            lines.extend(f'{row.label or row.serial or row.drive_id} — {row.person or "Unknown person"} — {row.released_on}'
                         for row in result.overdue)
            if result.practice:
                lines.insert(0, 'Practice')
            self.gateway.notify(int(person['telegram_id']), html.escape('\n'.join(lines), quote=False), [],
                                f'drive-reminder:{today.isoformat()}')
        return {'overdue': [row.model_dump(mode='json') for row in result.overdue], 'practice': result.practice}
