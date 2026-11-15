"""A fake calendar for tests. No network and no credential."""
from __future__ import annotations

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
