"""The calendar adapter interface: events create, update, delete and ACL list and set."""
from __future__ import annotations

from typing import Any, Protocol


class CalendarError(Exception):
    """The calendar could not be written now. The job may retry."""


class EventExists(CalendarError):
    """An event with this id is already on the calendar."""


class EventNotFound(CalendarError):
    """No event has this id on the calendar."""


class Calendar(Protocol):
    def insert_event(self, calendar: str, event: str, body: dict[str, Any]) -> None: ...
    def update_event(self, calendar: str, event: str, body: dict[str, Any]) -> None: ...
    def delete_event(self, calendar: str, event: str) -> None: ...
    def list_acl(self, calendar: str) -> list[dict[str, str]]: ...
    def set_acl(self, calendar: str, email: str, role: str) -> None: ...
