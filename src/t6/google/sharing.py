"""Bay calendar sharing follows the `bookings` permission (#271).

Holders of `bookings` get edit (`writer`) on every Bay calendar; every other active staff Person gets view (`reader`).
t6 wins: a grant or revoke queues a `booking.calendar_sharing` job in the same transaction, and a daily
`google.reconcile_sharing` job compares each calendar's actual ACL with t6 and fixes the difference.

What the jobs own: an ACL entry whose email is a Person's email (Notion keeps it, found by the binding, as `inbound.py` does).
A writer added by hand in Google, whose email is no Person's, is lowered to `reader` (calendar sharing is the permission check for inbound
edits, so no one outside t6's people may edit); other entries whose email is no Person's are counted as `unmanaged`.
What they never touch: a manager entry (role `owner`: the calendar owner and the service account are both managers) and the service
account's email in any role. Revoke sets `reader`; it does not delete the entry.
Logs and job results name an identity only by a short hash and count the changes.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from t6.domain.events import JsonValue
from t6.google.calendar import Calendar, CalendarError
from t6.google.config import CALENDARS_VARIABLE, bay_calendars, credential_path
from t6.registry.sqlite import SqliteProjectRegistry
from t6.worker.client import WorkerJob
from t6.worker.runner import Handler

if TYPE_CHECKING:
    from t6.server.queue import Submitted
    from t6.worker.internal_queue import InternalJobQueue

SHARING_KIND = 'booking.calendar_sharing'
RECONCILE_KIND = 'google.reconcile_sharing'
QUEUE = 'main'
PERMISSION = 'bookings'
EDIT, VIEW, MANAGER = 'writer', 'reader', 'owner'
RECONCILE_EVERY = 86400
log = logging.getLogger('t6.google.sharing')


class Identities(Protocol):
    """The Google email a Person holds in Notion (the same lookup `inbound.py` uses). A failed lookup raises `CalendarError`, so the job retries."""

    def email(self, person: str) -> str | None: ...


def short_hash(email: str) -> str:
    """A short stable id for an identity in logs and results."""
    return hashlib.sha256(email.strip().casefold().encode()).hexdigest()[:8]


def enqueue(registry: SqliteProjectRegistry, db: sqlite3.Connection, person: str, verb: str, key: str, now: float) -> None:
    """Queue the sharing update in the same transaction as the grant or revoke. `key` is the change's idempotency key."""
    from t6.worker.internal_queue import InternalJobQueue  # imported here: t6.person imports this module, and the queue imports t6.person
    InternalJobQueue(registry, clock=lambda: now).submit(QUEUE, SHARING_KIND, f'{person}:{verb}:{key}', {'person_id': person}, db=db)


class NoSharing:
    """The handler when no calendar is set up: the job finishes and says why. It never fails, so no job dies."""

    def handle(self, job: WorkerJob) -> JsonValue:
        return {'event': 'skipped', 'reason': f'no calendar configured: set {CALENDARS_VARIABLE} and the Google credential'}


class SharingSync:
    def __init__(self, registry: SqliteProjectRegistry, calendar: Calendar, calendars: Mapping[str, str], identities: Identities, *,
                 service_account: str, clock: Callable[[], float] = time.time) -> None:
        self.registry, self.calendar, self.identities, self.clock = registry, calendar, identities, clock
        self.calendars = list(dict.fromkeys(calendars.values()))
        self.service_account = service_account.strip().casefold()

    def schedule(self, queue: InternalJobQueue | None = None, run_at: float | None = None) -> Submitted:
        from t6.worker.internal_queue import InternalJobQueue  # imported here: t6.person imports this module, and the queue imports t6.person
        """Queue one reconcile run, once per day: the key is the day it is due."""
        queue = queue or InternalJobQueue(self.registry, clock=self.clock)
        due = self.clock() if run_at is None else run_at
        return queue.submit(QUEUE, RECONCILE_KIND, f'reconcile:{int(due // RECONCILE_EVERY)}', {}, run_at=due)

    def handle_change(self, job: WorkerJob) -> JsonValue:
        """After a grant or revoke: set the one Person's role on every Bay calendar."""
        person = job.payload.get('person_id')
        if not isinstance(person, str):
            raise ValueError('job has no person_id')
        return self._run([person])

    def handle_reconcile(self, job: WorkerJob) -> JsonValue:
        """Daily: bring every Bay calendar's ACL to what t6 says."""
        self.schedule(run_at=self.clock() + RECONCILE_EVERY)  # first, so a failed run does not end the chain
        return self._run(None)

    def _people(self, only: list[str] | None) -> list[tuple[str, bool, bool]]:
        """(person id, active staff, holds bookings), ordered by id."""
        with self.registry._transaction() as db:
            rows = db.execute('SELECT p.id, p.kind = ? AND p.active = 1, EXISTS (SELECT 1 FROM person_permissions g WHERE g.person_id = p.id AND g.permission = ?) '
                              'FROM persons p ORDER BY p.id', ('staff', PERMISSION)).fetchall()
        return [(str(r[0]), bool(r[1]), bool(r[2])) for r in rows if only is None or r[0] in only]

    def _run(self, only: list[str] | None) -> JsonValue:
        wanted: dict[str, tuple[str, bool]] = {}  # email (lowercase) -> (role, active staff)
        no_email = lookup_failed = 0
        for person, staff, holds in self._people(only):
            if only is None and not staff:
                continue  # the reconcile needs an email only for active staff: any writer it does not know is lowered anyway
            try:
                email = self.identities.email(person)
            except CalendarError:
                if only is not None:
                    raise  # a grant or revoke job for one Person retries: the change must not be lost
                lookup_failed += 1  # the reconcile counts it and goes on; the next daily run reads that Person again
                continue
            if not email or not email.strip():
                no_email += 1
                continue
            wanted[email.strip().casefold()] = (EDIT if staff and holds else VIEW, staff)
        fixed: list[dict[str, str]] = []
        unmanaged = 0
        for calendar in self.calendars:
            entries = [(e.get('scope', 'user'), e['email'].strip(), e['role']) for e in self.calendar.list_acl(calendar)]
            current = {value.casefold(): role for scope, value, role in entries if scope == 'user'}
            roles = {email: role for email, role in current.items() if role != MANAGER and email != self.service_account}
            if only is None:
                for scope, value, listed in entries:
                    email = value.casefold()
                    if listed == MANAGER or (scope == 'user' and (email == self.service_account or email in wanted)):
                        continue
                    if listed != EDIT or lookup_failed:  # with a failed lookup a writer may be a Person this run could not read: leave it
                        unmanaged += 1
                        continue
                    self.calendar.set_acl(calendar, email if scope == 'user' else value, VIEW, scope)  # a hand-added writer: a user who is no Person, a group, a domain, or everyone
                    change = {'calendar': short_hash(calendar), 'identity': short_hash(email if scope == 'user' else f'{scope}:{value}'), 'change': f'{listed}->{VIEW}'}
                    if scope != 'user':
                        change['scope'] = scope
                    fixed.append(change)
                    log.info('calendar sharing fix: calendar=%s identity=%s role %s', change['calendar'], change['identity'], change['change'])
            for email, (role, staff) in wanted.items():
                if email == self.service_account or current.get(email) == MANAGER:
                    continue
                have = roles.get(email)
                if not staff:  # inactive or not staff: take edit away, never add or remove anything else
                    role = VIEW
                    if have != EDIT:
                        continue
                if have == role:
                    continue
                self.calendar.set_acl(calendar, email, role)
                change = {'calendar': short_hash(calendar), 'identity': short_hash(email), 'change': f'{have or "none"}->{role}'}
                fixed.append(change)
                log.info('calendar sharing fix: calendar=%s identity=%s role %s', change['calendar'], change['identity'], change['change'])
        result: dict[str, JsonValue] = {'event': 'shared' if only is not None else 'reconciled', 'calendars': len(self.calendars), 'fixed_count': len(fixed),
                                        'fixed': [dict(f) for f in fixed], 'no_email': no_email}
        if only is None:
            result['unmanaged'] = unmanaged
            result['lookup_failed'] = lookup_failed
        return result


def sharing_handlers(
    environ: Mapping[str, str],
    open_registry: Callable[[], SqliteProjectRegistry],
    open_identities: Callable[[SqliteProjectRegistry], Any],
) -> dict[str, Handler]:
    """The `booking.calendar_sharing` and `google.reconcile_sharing` handlers for the main worker.

    Real when the Bay calendars and the credential are set, else `NoSharing`. The daily run is queued here, at worker start.
    `open_identities` gives an object with `email(person)`. A set but unusable setting raises `ValueError`.
    """
    calendars, credential = bay_calendars(environ), credential_path(environ)
    if not calendars or not credential:
        skipped = NoSharing().handle
        return {SHARING_KIND: skipped, RECONCILE_KIND: skipped}
    from t6.google.http import GoogleCalendar, ServiceAccount, UrllibHttp
    try:
        account = ServiceAccount.from_json(Path(credential).read_text(encoding='utf-8'))
    except OSError as error:
        raise ValueError(f'cannot read the Google credential file: {error.__class__.__name__}') from error
    registry = open_registry()
    sharing = SharingSync(registry, GoogleCalendar(account, UrllibHttp()), calendars, open_identities(registry), service_account=account.email)
    sharing.schedule()
    return {SHARING_KIND: sharing.handle_change, RECONCILE_KIND: sharing.handle_reconcile}
