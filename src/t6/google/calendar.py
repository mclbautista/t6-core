"""The calendar adapter interface: events create, update, delete, read and list changes, notification channels, and ACL list and set."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class CalendarError(Exception):
    """The calendar could not be written now. The job may retry."""


class EventExists(CalendarError):
    """An event with this id is already on the calendar."""


class EventNotFound(CalendarError):
    """No event has this id on the calendar."""


class SyncTokenExpired(CalendarError):
    """Google no longer accepts this sync token (HTTP 410). The caller lists everything again."""


@dataclass(frozen=True)
class Changes:
    """Events changed since a sync token (all events for no token) and the token for the next call.

    An event is flat: `id`, `status` (`confirmed` or `cancelled`), and for a live event `summary`, `description`,
    `start`, `end` (text), `etag`, `updated`, and `editor` (the editor's email, or None when Google names none).
    """

    events: list[dict[str, Any]]
    next_token: str


@dataclass(frozen=True)
class Channel:
    """A notification channel Google accepted: its resource id and its expiry in epoch seconds."""

    resource_id: str
    expires_at: float


class Calendar(Protocol):
    def insert_event(self, calendar: str, event: str, body: dict[str, Any]) -> None: ...
    def update_event(self, calendar: str, event: str, body: dict[str, Any]) -> None: ...
    def delete_event(self, calendar: str, event: str) -> None: ...
    def get_event(self, calendar: str, event: str) -> dict[str, Any] | None: ...
    def list_changes(self, calendar: str, sync_token: str | None) -> Changes: ...
    def watch_events(self, calendar: str, channel: str, address: str, token: str, ttl_seconds: int) -> Channel: ...
    def stop_channel(self, channel: str, resource_id: str) -> None: ...
    def list_acl(self, calendar: str) -> list[dict[str, str]]: ...  # every rule: `scope` (user, group, domain, default), `email` (the scope's value, empty for default), `role`
    def set_acl(self, calendar: str, email: str, role: str, scope: str = 'user') -> None: ...
