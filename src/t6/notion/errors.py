"""The one error type the Notion client raises."""

from __future__ import annotations


class NotionError(Exception):
    """A failed Notion call.

    `status` is the HTTP status, or None when no response arrived (a timeout or
    a network error). `code` is Notion's error code, or `timeout` /
    `network_error`. `retry_after` is the Retry-After header in seconds.
    """

    def __init__(
        self,
        status: int | None,
        code: str,
        message: str,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(f"Notion {status if status is not None else '-'} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.retry_after = retry_after
