"""Inbound Google Calendar edits: a person's change to a Booking event goes through the booking rules (#270).

Google's notification names no event: it says "this calendar changed". The handler lists what changed since the
stored sync token, matches each event to its Booking by the event id (derived from the Booking id), and either runs
the matching verb as the Person who edited it or writes the Booking's current state back with a note.
The Booking verbs stay the only writer of Bookings (`BookingStore.write`).
"""
from __future__ import annotations

import hashlib
import json
import secrets
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from t6.bay import BookingStore
from t6.domain.events import JsonValue
from t6.domain.studio_time import MANILA
from t6.google.calendar import Calendar, CalendarError, EventExists, EventNotFound, SyncTokenExpired
from t6.google.config import (
    CALENDARS_VARIABLE, CHANNEL_TOKEN_VARIABLE, NOTIFY_URL_VARIABLE, bay_calendars, channel_token, credential_path, notify_url,
)
from t6.google.sync import BOOKING_SYNC_KIND, Names, event_body, event_id
from t6.person import People
from t6.preview import PreviewTokens
from t6.registry.sqlite import SqliteProjectRegistry
from t6.verbs import Done, Preview, Refused
from t6.worker.client import WorkerJob
from t6.server.queue import Submitted
from t6.worker.internal_queue import InternalJobQueue
from t6.worker.runner import Handler

CHANGED_KIND = 'google.calendar_changed'
RENEW_KIND = 'google.renew_channels'
QUEUE = 'main'
CHANNEL_TTL = 7 * 86400        # Google's documented default; the maximum is not documented
RENEW_BEFORE = 3 * 86400       # replace a channel this long before it expires
RENEW_EVERY = 86400            # the renewal job runs daily
NO_PERMISSION = 'the editor is not a Person with the bookings permission'
NO_TIME = 'the event needs a start and end with a time zone'


class Identities(Protocol):
    """The Google email a Person holds in Notion. A lookup that fails raises `CalendarError`, so the job retries."""

    def email(self, person: str) -> str | None: ...


class NoInbound:
    """The handler when inbound edits are not set up: the job finishes and says why. It never fails, so no job dies."""

    def handle(self, job: WorkerJob) -> JsonValue:
        return {'event': 'skipped', 'reason': f'no inbound calendar edits configured: set {CALENDARS_VARIABLE}, the Google credential, {NOTIFY_URL_VARIABLE} and {CHANNEL_TOKEN_VARIABLE}'}


def _instant(text: object) -> datetime:
    moment = datetime.fromisoformat(str(text))
    if moment.tzinfo is None:
        raise ValueError('no time zone')
    return moment.astimezone(MANILA)


class InboundEdits:
    """The `google.calendar_changed` handler (main worker)."""

    def __init__(self, registry: SqliteProjectRegistry, calendar: Calendar, calendars: Mapping[str, str], names: Names, identities: Identities,
                 *, clock: Callable[[], float] = time.time) -> None:
        self.registry, self.calendar, self.calendars, self.names, self.identities, self.clock = registry, calendar, dict(calendars), names, identities, clock
        tokens = PreviewTokens(secrets.token_hex(32))  # a preview made and confirmed here, in one call: the key never leaves this object
        self.people, self.bookings = People(registry, tokens), BookingStore(registry, tokens)

    def handle(self, job: WorkerJob) -> JsonValue:
        headers = job.payload.get('headers')
        headers = headers if isinstance(headers, dict) else {}
        channel, state = str(headers.get('x-goog-channel-id', '')), str(headers.get('x-goog-resource-state', ''))
        with self.registry._transaction() as db:
            row = db.execute('SELECT calendar FROM calendar_channels WHERE channel_id = ? AND active = 1 AND expires_at > ?', (channel, self.clock())).fetchone()
        if row is None:
            return {'ignored': 'unknown channel'}
        if state == 'sync':
            return {'ignored': 'sync message'}
        calendar = str(row[0])
        with self.registry._transaction() as db:
            stored = db.execute('SELECT sync_token FROM calendar_sync_tokens WHERE calendar = ?', (calendar,)).fetchone()
        try:
            changes = self.calendar.list_changes(calendar, stored[0] if stored else None)
        except SyncTokenExpired:
            changes = self.calendar.list_changes(calendar, None)  # Google forgot the token: list everything; versions and content checks make it safe
        by_event = {event_id(b['id']): b for b in self.bookings.list()}
        outcomes: dict[str, int] = {}
        for event in changes.events:
            outcome = self._event(calendar, event, by_event.get(str(event.get('id'))))
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
        with self.registry._transaction() as db:
            db.execute('INSERT INTO calendar_sync_tokens VALUES (?, ?) ON CONFLICT (calendar) DO UPDATE SET sync_token = excluded.sync_token', (calendar, changes.next_token))
        return {'channel': channel, 'events': len(changes.events), 'outcomes': dict(sorted(outcomes.items()))}

    def _event(self, calendar: str, event: Mapping[str, Any], booking: dict[str, Any] | None) -> str:
        if booking is None:
            return 'foreign'  # not an event t6 wrote: t6 does not touch it
        name = str(event['id'])
        version = event.get('etag') or event.get('updated')
        with self.registry._transaction() as db:
            if version and db.execute('SELECT 1 FROM calendar_event_versions WHERE calendar = ? AND event = ? AND version = ?', (calendar, name, version)).fetchone():
                return 'seen'
            if db.execute("SELECT 1 FROM internal_jobs WHERE kind = ? AND state IN ('queued', 'claimed') AND dedupe_key LIKE ?", (BOOKING_SYNC_KIND, booking['id'] + ':%')).fetchone():
                return 'sync pending'  # our own write is on its way and will overwrite this event; the notification is stale
        outcome = self._decide(calendar, event, booking, str(version or ''))
        if version:
            with self.registry._transaction() as db:
                db.execute('INSERT OR IGNORE INTO calendar_event_versions VALUES (?, ?, ?, ?)', (calendar, name, version, outcome))
        return outcome

    def _decide(self, calendar: str, event: Mapping[str, Any], booking: dict[str, Any], version: str) -> str:
        """`equal` (our own echo), `applied`, `reverted` or `ignored`."""
        mine = self.calendars.get(booking['bay_id'])
        bays = {cal: bay for bay, cal in self.calendars.items()}
        if booking['cancelled'] or mine is None or calendar not in bays:
            return 'ignored'
        name = event_id(booking['id'])
        if event.get('status') == 'cancelled':
            if calendar != mine or any(other != calendar and self.calendar.get_event(other, name) is not None for other in bays):
                return 'ignored'  # a stale copy, or the event moved to another Bay calendar (that calendar's change says so)
            verb, values = 'cancel', {'booking': booking['id']}
        else:
            try:
                start, end = _instant(event.get('start')), _instant(event.get('end'))
                if end <= start:
                    raise ValueError('end before start')
            except (ValueError, TypeError):
                return self._revert(calendar, mine, booking, NO_TIME)
            old_start, old_end = _instant(booking['start']), _instant(booking['end'])
            if bays[calendar] == booking['bay_id'] and (start, end) == (old_start, old_end):
                return 'equal'  # the event already says what the Booking says: our own write coming back
            if bays[calendar] == booking['bay_id'] and start == old_start and end > old_end:
                verb, values = 'extend', {'booking': booking['id'], 'end': end.isoformat()}
            else:
                verb, values = 'move', {'booking': booking['id'], 'bay': bays[calendar], 'start': start.isoformat(), 'end': end.isoformat()}
        actor = self._actor(event.get('editor'))
        if actor is None:
            return self._revert(calendar, mine, booking, NO_PERMISSION)
        key = f'google:{calendar}:{name}:' + (version or hashlib.sha256(json.dumps([event.get('status'), event.get('start'), event.get('end')]).encode()).hexdigest()[:16])
        outcome = self.bookings.write(verb, values, actor=actor, idempotency_key=key, confirm=None)
        if isinstance(outcome, Preview):
            outcome = self.bookings.write(verb, values, actor=actor, idempotency_key=key, confirm=outcome.token)
        if isinstance(outcome, Done):
            return 'applied'
        return self._revert(calendar, mine, booking, getattr(outcome, 'reason', None) or 'the booking rules refused the change')

    def _actor(self, editor: object) -> str | None:
        """The Person whose Notion email is the editor's and who holds `bookings`; None for an unknown editor."""
        if not isinstance(editor, str) or not editor.strip():
            return None
        with self.registry._transaction() as db:
            holders: list[str] = [str(r[0]) for r in db.execute('SELECT p.id FROM persons p JOIN person_permissions g ON g.person_id = p.id WHERE p.active = 1 AND g.permission = ? ORDER BY p.id', ('bookings',))]
        for person in holders:
            email = self.identities.email(person)
            if email and email.strip().casefold() == editor.strip().casefold() and self.people.authorize(person, 'bookings') is None:
                return person
        return None

    def _revert(self, calendar: str, mine: str, booking: dict[str, Any], reason: str) -> str:
        """Write the Booking's current state back to its own calendar, with a note saying why the change was undone."""
        name = event_id(booking['id'])
        body = event_body(self.names, booking['id'], booking['project_id'], booking['person_id'], booking['start'], booking['end'])
        body['description'] += f'\nNote: change reverted: {reason}.'
        if calendar != mine:
            try:
                self.calendar.delete_event(calendar, name)
            except EventNotFound:
                pass
        try:
            self.calendar.update_event(mine, name, body)
        except EventNotFound:
            try:
                self.calendar.insert_event(mine, name, body)
            except EventExists:
                self.calendar.update_event(mine, name, body)
        return 'reverted'


class ChannelRenewal:
    """The `google.renew_channels` handler: every Bay calendar keeps a live notification channel, replaced before it expires."""

    def __init__(self, registry: SqliteProjectRegistry, calendar: Calendar, calendars: Mapping[str, str], *, address: str, token: str,
                 clock: Callable[[], float] = time.time, new_id: Callable[[], str] | None = None) -> None:
        self.registry, self.calendar, self.calendars, self.address, self.token, self.clock = registry, calendar, dict(calendars), address, token, clock
        self.new_id = new_id or (lambda: 't6ch-' + secrets.token_hex(12))

    def schedule(self, queue: InternalJobQueue | None = None, run_at: float | None = None) -> Submitted:
        """Queue one renewal run, once per day: the key is the day it is due."""
        queue = queue or InternalJobQueue(self.registry, clock=self.clock)
        due = self.clock() if run_at is None else run_at
        return queue.submit(QUEUE, RENEW_KIND, f'renew:{int(due // 86400)}', {}, run_at=due)

    def handle(self, job: WorkerJob) -> JsonValue:
        now = self.clock()
        self.schedule(run_at=now + RENEW_EVERY)  # first, so a failed run does not end the chain
        fingerprint = hashlib.sha256(self.token.encode()).hexdigest()  # the token itself is never stored
        with self.registry._transaction() as db:
            db.execute('UPDATE calendar_channels SET active = 0 WHERE active = 1 AND expires_at <= ?', (now,))
        renewed: list[str] = []
        kept = 0
        for calendar in dict.fromkeys(self.calendars.values()):
            with self.registry._transaction() as db:
                rows = db.execute('SELECT channel_id, resource_id, expires_at, token_hash FROM calendar_channels WHERE calendar = ? AND active = 1', (calendar,)).fetchall()
                baseline = db.execute('SELECT 1 FROM calendar_sync_tokens WHERE calendar = ?', (calendar,)).fetchone()
            if any(r[2] - now >= RENEW_BEFORE and r[3] == fingerprint for r in rows):
                kept += 1
                continue
            if baseline is None:
                start = self.calendar.list_changes(calendar, None)  # what is there now is the baseline; only later changes are inbound edits
                with self.registry._transaction() as db:
                    db.execute('INSERT OR IGNORE INTO calendar_sync_tokens VALUES (?, ?)', (calendar, start.next_token))
            channel = self.new_id()
            made = self.calendar.watch_events(calendar, channel, self.address, self.token, CHANNEL_TTL)
            with self.registry._transaction() as db:
                db.execute('INSERT INTO calendar_channels VALUES (?, ?, ?, ?, ?, 1)', (channel, calendar, made.resource_id, made.expires_at, fingerprint))
            renewed.append(channel)
            for old_channel, resource, _, _ in rows:
                try:
                    self.calendar.stop_channel(old_channel, resource)
                except CalendarError:
                    pass  # it expires by itself
                with self.registry._transaction() as db:
                    db.execute('UPDATE calendar_channels SET active = 0 WHERE channel_id = ?', (old_channel,))
        return {'event': 'renewed', 'new_channels': [*renewed], 'kept': kept}


def inbound_handlers(
    environ: Mapping[str, str],
    open_registry: Callable[[], SqliteProjectRegistry],
    open_names: Callable[[SqliteProjectRegistry], Any],
    *,
    http: Any = None,
) -> dict[str, Handler]:
    """The `google.calendar_changed` and `google.renew_channels` handlers for the main worker.

    Real when the Bay calendars, the credential, the notify address and the channel token are all set, else `NoInbound`.
    `open_names` gives an object that is both `Names` and `Identities`. A set but unusable setting raises `ValueError`.
    """
    calendars, credential, address, token = bay_calendars(environ), credential_path(environ), notify_url(environ), channel_token(environ)
    if not (calendars and credential and address and token):
        skipped = NoInbound().handle
        return {CHANGED_KIND: skipped, RENEW_KIND: skipped}
    from t6.google.http import GoogleCalendar, ServiceAccount, UrllibHttp
    try:
        account = ServiceAccount.from_json(Path(credential).read_text(encoding='utf-8'))
    except OSError as error:
        raise ValueError(f'cannot read the Google credential file: {error.__class__.__name__}') from error
    registry = open_registry()
    calendar = GoogleCalendar(account, http or UrllibHttp())
    names = open_names(registry)
    renewal = ChannelRenewal(registry, calendar, calendars, address=address, token=token)
    renewal.schedule()
    return {CHANGED_KIND: InboundEdits(registry, calendar, calendars, names, names).handle, RENEW_KIND: renewal.handle}
