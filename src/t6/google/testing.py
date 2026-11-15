"""A fake calendar for tests. No network and no credential."""
from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from t6.google.calendar import CalendarError, Changes, Channel, EventExists, EventNotFound, SyncTokenExpired


class FakeCalendar:
    """Events by (calendar, event id) as written. Every change is logged with an etag and its editor, so `list_changes` and `get_event` read like Google.

    Writes through the adapter methods have no editor (Google names none for the service account's own writes);
    `external_edit`, `external_move` and `external_delete` stand for a person using the calendar.
    """

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self.events: dict[tuple[str, str], dict[str, Any]] = {}
        self.meta: dict[tuple[str, str], dict[str, Any]] = {}
        self.channels: dict[str, dict[str, Any]] = {}
        self.stopped: list[tuple[str, str]] = []
        self._log: list[tuple[str, str]] = []
        self._versions = 0
        self._valid_from = 0
        self._read_failures = 0
        self._watch_failures = 0
        self.acl: dict[str, dict[str, str]] = {}
        self.calls: list[tuple[str, str]] = []
        self._failures = 0
        self._after = False

    def fail_next(self, count: int, *, after_applying: bool = False) -> None:
        """Fail the next `count` writes. With `after_applying`, the write lands but its answer is lost."""
        self._failures, self._after = count, after_applying

    def fail_reads(self, count: int) -> None:
        self._read_failures = count

    def fail_watch(self, count: int) -> None:
        self._watch_failures = count

    def expire_sync_tokens(self) -> None:
        """Every sync token handed out so far is now invalid (HTTP 410)."""
        self._valid_from = len(self._log)

    def _changed(self, calendar: str, event: str, editor: str | None = None) -> None:
        self._versions += 1
        self.meta[(calendar, event)] = {'etag': f'"{self._versions}"', 'updated': f'v{self._versions}', 'editor': editor}
        self._log.append((calendar, event))

    def external_edit(self, calendar: str, event: str, *, editor: str | None, **fields: str) -> None:
        self.events[(calendar, event)].update(fields)
        self._changed(calendar, event, editor)

    def touch(self, calendar: str, event: str, *, editor: str | None) -> None:
        self._changed(calendar, event, editor)

    def external_delete(self, calendar: str, event: str, *, editor: str | None) -> None:
        del self.events[(calendar, event)]
        self._changed(calendar, event, editor)

    def external_move(self, source: str, target: str, event: str, *, editor: str | None) -> None:
        self.events[(target, event)] = self.events.pop((source, event))
        self._changed(source, event, editor)
        self._changed(target, event, editor)

    def _read(self) -> None:
        if self._read_failures > 0:
            self._read_failures -= 1
            raise CalendarError('calendar unavailable')

    def _flat(self, calendar: str, event: str) -> dict[str, Any]:
        if (calendar, event) not in self.events:
            meta = self.meta.get((calendar, event), {})
            return {'id': event, 'status': 'cancelled', 'etag': meta.get('etag'), 'editor': meta.get('editor')}
        return {'id': event, 'status': 'confirmed', **self.events[(calendar, event)], **self.meta.get((calendar, event), {'etag': '"0"', 'updated': 'v0', 'editor': None})}

    def get_event(self, calendar: str, event: str) -> dict[str, Any] | None:
        self._read()
        return self._flat(calendar, event) if (calendar, event) in self.events else None

    def list_changes(self, calendar: str, sync_token: str | None) -> Changes:
        self._read()
        if sync_token is not None and int(sync_token) < self._valid_from:
            raise SyncTokenExpired('sync token expired')
        if sync_token is None:
            names = [event for (cal, event) in self.events if cal == calendar]
        else:
            names = list(dict.fromkeys(event for cal, event in self._log[int(sync_token):] if cal == calendar))
        return Changes([self._flat(calendar, event) for event in names], str(len(self._log)))

    def watch_events(self, calendar: str, channel: str, address: str, token: str, ttl_seconds: int) -> Channel:
        if self._watch_failures > 0:
            self._watch_failures -= 1
            raise CalendarError('calendar unavailable')
        self.channels[channel] = {'calendar': calendar, 'address': address, 'token': token}
        return Channel('res-' + channel, self._clock() + ttl_seconds)

    def stop_channel(self, channel: str, resource_id: str) -> None:
        self.stopped.append((channel, resource_id))
        self.channels.pop(channel, None)

    def _write(self, action: str, calendar: str, apply: Any) -> None:
        failing = self._failures > 0
        if failing:
            self._failures -= 1
            if not self._after:
                raise CalendarError('calendar unavailable')
        apply()
        self.calls.append((action, calendar))
        if failing:
            raise CalendarError('answer lost')

    def insert_event(self, calendar: str, event: str, body: dict[str, Any]) -> None:
        if (calendar, event) in self.events:
            raise EventExists(event)
        self._write('insert', calendar, lambda: self._store(calendar, event, body))

    def update_event(self, calendar: str, event: str, body: dict[str, Any]) -> None:
        if (calendar, event) not in self.events:
            raise EventNotFound(event)
        self._write('update', calendar, lambda: self._store(calendar, event, body))

    def delete_event(self, calendar: str, event: str) -> None:
        if (calendar, event) not in self.events:
            raise EventNotFound(event)
        self._write('delete', calendar, lambda: self._remove(calendar, event))

    def _store(self, calendar: str, event: str, body: dict[str, Any]) -> None:
        self.events[(calendar, event)] = dict(body)
        self._changed(calendar, event)

    def _remove(self, calendar: str, event: str) -> None:
        del self.events[(calendar, event)]
        self._changed(calendar, event)

    def list_acl(self, calendar: str) -> list[dict[str, str]]:
        return [{'email': email, 'role': role} for email, role in sorted(self.acl.get(calendar, {}).items())]

    def set_acl(self, calendar: str, email: str, role: str) -> None:
        self.acl.setdefault(calendar, {})[email] = role


class FakeNames:
    """Names by id. `fail_next` makes the next lookups fail, as a Notion outage does."""

    def __init__(self, projects: dict[str, str], people: dict[str, str]) -> None:
        self.projects, self.people, self._failures = projects, people, 0

    def fail_next(self, count: int) -> None:
        self._failures = count

    def _lookup(self, table: dict[str, str], key: str) -> str:
        if self._failures > 0:
            self._failures -= 1
            raise CalendarError('name lookup failed')
        if key not in table:
            raise CalendarError(f'no name for {key}')
        return table[key]

    def project(self, project: str) -> str:
        return self._lookup(self.projects, project)

    def person(self, person: str) -> str:
        return self._lookup(self.people, person)


class FakeIdentities:
    """Person id to Google email, as Notion's Contact page holds it. `fail_next` makes lookups fail, as a Notion outage does."""

    def __init__(self, emails: dict[str, str]) -> None:
        self.emails, self._failures = emails, 0

    def fail_next(self, count: int) -> None:
        self._failures = count

    def email(self, person: str) -> str | None:
        if self._failures > 0:
            self._failures -= 1
            raise CalendarError('email lookup failed')
        return self.emails.get(person)


@dataclass(frozen=True)
class HttpCall:
    method: str
    url: str
    headers: dict[str, str]
    body: bytes


class FakeHttp:
    """A fake HTTP transport: answers by method and URL (query ignored), and records each request."""

    def __init__(self) -> None:
        self.calls: list[HttpCall] = []
        self._answers: dict[tuple[str, str], tuple[int, bytes]] = {}
        self._failure: Exception | None = None

    def answer(self, method: str, url: str, status: int, body: Any) -> None:
        self._answers[(method, url)] = (status, b'' if body is None else json.dumps(body).encode())

    def fail(self, error: Exception) -> None:
        self._failure = error

    def send(self, method: str, url: str, headers: Mapping[str, str], body: bytes) -> tuple[int, bytes]:
        self.calls.append(HttpCall(method, url, dict(headers), body))
        if self._failure is not None:
            error, self._failure = self._failure, None
            raise error
        key = (method, url.split('?', 1)[0])
        if key not in self._answers:
            return 404, b'{"error": {"code": 404, "message": "no recorded answer"}}'
        return self._answers[key]
