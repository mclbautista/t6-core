"""Outbound sync of Bookings: one internal job per change, one event per Booking."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from typing import Any

from t6.domain.events import JsonValue
from t6.google.calendar import Calendar, EventExists, EventNotFound
from t6.registry.sqlite import SqliteProjectRegistry
from t6.worker.client import WorkerJob
from t6.worker.runner import Refused

BOOKING_SYNC_KIND = 'booking.calendar_sync'
QUEUE = 'main'


def enqueue(db: sqlite3.Connection, booking: str, previous_bay: str | None, key: str, now: float) -> None:
    """Queue the write in the same transaction as the Booking change. `key` is the change's idempotency key."""
    db.execute("INSERT INTO internal_jobs (queue, kind, dedupe_key, payload, run_at, state, created_at) VALUES (?, ?, ?, ?, ?, 'queued', ?) "
               "ON CONFLICT (kind, dedupe_key) DO NOTHING",
               (QUEUE, BOOKING_SYNC_KIND, f'{booking}:{key}', json.dumps({'booking_id': booking, 'previous_bay': previous_bay}), now, now))


def event_id(booking: str) -> str:
    """The Booking's id is the idempotency key: it always maps to the same event id (lowercase hex)."""
    return hashlib.sha1(booking.encode()).hexdigest()


class CalendarSync:
    def __init__(self, registry: SqliteProjectRegistry, calendar: Calendar, calendars: Mapping[str, str]) -> None:
        self.registry, self.calendar, self.calendars = registry, calendar, calendars

    def handle(self, job: WorkerJob) -> JsonValue:
        booking, previous = job.payload['booking_id'], job.payload.get('previous_bay')
        with self.registry._transaction() as db:
            row = db.execute('SELECT bay_id, project_id, start, end, cancelled FROM bookings WHERE id=?', (booking,)).fetchone()
        if row is None or not isinstance(booking, str):
            raise Refused('Booking not found')
        bay, project, start, end, cancelled = row
        event = event_id(booking)
        if isinstance(previous, str) and previous != bay:
            self._delete(previous, event, required=False)
        if cancelled:
            self._delete(bay, event, required=True)
            return {'event': 'deleted'}
        calendar = self._calendar(bay)
        body: dict[str, Any] = {'summary': project, 'description': booking, 'start': start, 'end': end}
        try:
            self.calendar.insert_event(calendar, event, body)
        except EventExists:
            self.calendar.update_event(calendar, event, body)
        return {'event': 'written'}

    def _calendar(self, bay: Any) -> str:
        if bay not in self.calendars:
            raise Refused('Bay has no calendar')
        return self.calendars[bay]

    def _delete(self, bay: Any, event: str, *, required: bool) -> None:
        if bay not in self.calendars:
            if required:
                raise Refused('Bay has no calendar')
            return
        try:
            self.calendar.delete_event(self.calendars[bay], event)
        except EventNotFound:
            pass
