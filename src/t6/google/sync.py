"""Outbound sync of Bookings: one internal job per change, one event per Booking."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

from t6.domain.events import JsonValue
from t6.google.calendar import Calendar, EventExists, EventNotFound
from t6.google.config import CALENDARS_VARIABLE, bay_calendars, credential_path
from t6.registry.sqlite import SqliteProjectRegistry
from t6.worker.client import WorkerJob
from t6.worker.runner import Handler, Refused

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


class Names(Protocol):
    """The Project's and the Person's names, which Notion keeps. A lookup that fails raises `CalendarError`, so the job retries."""

    def project(self, project: str) -> str: ...
    def person(self, person: str) -> str: ...


def event_body(names: Names, booking: str, project: str, person: str, start: str, end: str) -> dict[str, Any]:
    """The Project's name as the title; the description names the Person and the Booking ID, nothing else."""
    return {'summary': names.project(project), 'description': f'Person: {names.person(person)}\nBooking ID: {booking}', 'start': start, 'end': end}


class NoCalendar:
    """The handler when no calendar is set up: the job finishes and says why. It never fails, so no job dies."""

    def handle(self, job: WorkerJob) -> JsonValue:
        return {'event': 'skipped', 'reason': f'no calendar configured: set {CALENDARS_VARIABLE} and the Google credential'}


def calendar_handler(
    environ: Mapping[str, str],
    open_registry: Callable[[], SqliteProjectRegistry],
    open_names: Callable[[SqliteProjectRegistry], Names],
    *,
    http: Any = None,
) -> Handler:
    """The `booking.calendar_sync` handler for the main worker: real when the bay calendars and the credential are set, else `NoCalendar`.

    A set but unusable setting raises `ValueError` (the worker refuses to start).
    """
    calendars = bay_calendars(environ)
    credential = credential_path(environ)
    if not calendars or not credential:
        return NoCalendar().handle
    from t6.google.http import GoogleCalendar, ServiceAccount, UrllibHttp
    try:
        account = ServiceAccount.from_json(Path(credential).read_text(encoding='utf-8'))
    except OSError as error:
        raise ValueError(f'cannot read the Google credential file: {error.__class__.__name__}') from error
    registry = open_registry()
    return CalendarSync(registry, GoogleCalendar(account, http or UrllibHttp()), calendars, open_names(registry)).handle


class CalendarSync:
    def __init__(self, registry: SqliteProjectRegistry, calendar: Calendar, calendars: Mapping[str, str], names: Names) -> None:
        self.registry, self.calendar, self.calendars, self.names = registry, calendar, calendars, names

    def handle(self, job: WorkerJob) -> JsonValue:
        booking, previous = job.payload['booking_id'], job.payload.get('previous_bay')
        with self.registry._transaction() as db:
            row = db.execute('SELECT bay_id, project_id, person_id, start, end, cancelled FROM bookings WHERE id=?', (booking,)).fetchone()
        if row is None or not isinstance(booking, str):
            raise Refused('Booking not found')
        bay, project, person, start, end, cancelled = row
        event = event_id(booking)
        if isinstance(previous, str) and previous != bay:
            self._delete(previous, event, required=False)
        if cancelled:
            self._delete(bay, event, required=True)
            return {'event': 'deleted'}
        calendar = self._calendar(bay)
        body = event_body(self.names, booking, project, person, start, end)
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
