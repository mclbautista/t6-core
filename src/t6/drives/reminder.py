"""Daily Manila morning reminder, delivered through the worker gateway."""
from __future__ import annotations

import html
import os
import time
from collections.abc import Callable, Mapping
from datetime import date, datetime, time as daytime, timedelta
from zoneinfo import ZoneInfo

from t6.domain.events import JsonValue
from t6.drive_reads import overdue
from t6.drives.practice import practice_mode
from t6.drives.inbox import inbox
from t6.drives.notifications import dm_target
from t6.drives.debug import message_link
from t6.verbs import Done
from t6.drives.sqlite import SqliteDriveStore
from t6.person import People
from t6.worker import WorkerJob
from t6.worker.gateway import GatewayClient
from t6.worker.internal_queue import InternalJobQueue


class MorningReminder:
    def __init__(self, people: People, queue: InternalJobQueue, gateway: GatewayClient, custodian: str, *,
                 clock: Callable[[], float] = time.time, environ: Mapping[str, str] | None = None) -> None:
        self.people, self.queue, self.gateway, self.custodian = people, queue, gateway, custodian
        self.clock = clock
        self.environ = os.environ if environ is None else environ

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
        unfinished = inbox(self.people.registry)
        assert isinstance(unfinished, Done)
        items = unfinished.result['items']
        target = dm_target(self.people.registry, {**self.environ, 'T6_DRIVE_CUSTODIAN_PERSON_ID': self.custodian})
        if target and (result.overdue or items):
            lines = ['T6 Drives OUT for more than 14 days'] if result.overdue else []
            lines.extend(f'{row.label or row.serial or row.drive_id} — {row.person or "Unknown person"} — {row.released_on}'
                         for row in result.overdue)
            if items:
                lines.append('Unfinished Items')
                lines.extend(f"{item['text']} — {message_link(item['chat_id'], item['topic_id'], item['message_id'])}" for item in items)
            if result.practice:
                lines.insert(0, 'Practice')
            self.gateway.notify(target[0], html.escape('\n'.join(lines), quote=False), [],
                                f'drive-reminder:{today.isoformat()}', target[1])
        return {'overdue': [row.model_dump(mode='json') for row in result.overdue], 'practice': result.practice,
                'unfinished': items}
