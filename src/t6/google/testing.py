"""A fake calendar for tests. No network and no credential."""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from t6.google.calendar import CalendarError, EventExists, EventNotFound


class FakeCalendar:
    def __init__(self) -> None:
        self.events: dict[tuple[str, str], dict[str, Any]] = {}
        self.acl: dict[str, dict[str, str]] = {}
        self.calls: list[tuple[str, str]] = []
        self._failures = 0
        self._after = False

    def fail_next(self, count: int, *, after_applying: bool = False) -> None:
        """Fail the next `count` writes. With `after_applying`, the write lands but its answer is lost."""
        self._failures, self._after = count, after_applying

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
        self._write('insert', calendar, lambda: self.events.__setitem__((calendar, event), dict(body)))

    def update_event(self, calendar: str, event: str, body: dict[str, Any]) -> None:
        if (calendar, event) not in self.events:
            raise EventNotFound(event)
        self._write('update', calendar, lambda: self.events.__setitem__((calendar, event), dict(body)))

    def delete_event(self, calendar: str, event: str) -> None:
        if (calendar, event) not in self.events:
            raise EventNotFound(event)
        self._write('delete', calendar, lambda: self.events.pop((calendar, event)))

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
