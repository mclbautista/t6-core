"""Remove exact, configured secret values (tokens, keys) from text and log lines.

This is not ``withhold``. ``withhold`` finds credentials by their labels in
free text. ``Scrubber`` knows the secret values themselves and removes every
occurrence of them wherever they show up, for example inside a URL carried by
a network error.
"""

from __future__ import annotations

from collections.abc import Iterable
import logging

__all__ = ["REDACTED", "ScrubbingFormatter", "Scrubber"]

REDACTED = "[redacted]"


class Scrubber:
    """Replaces every occurrence of the given secret values with ``REDACTED``.

    Empty values are ignored. Longer secrets are replaced first, so a secret
    that contains another one is removed whole.
    """

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        self._secrets = sorted({s for s in secrets if s}, key=len, reverse=True)

    def scrub(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        return text


class ScrubbingFormatter(logging.Formatter):
    """A log formatter that scrubs the whole formatted record, exceptions included."""

    def __init__(self, scrubber: Scrubber, fmt: str = "%(asctime)s %(levelname)s %(name)s: %(message)s") -> None:
        super().__init__(fmt)
        self._scrubber = scrubber

    def format(self, record: logging.LogRecord) -> str:
        return self._scrubber.scrub(super().format(record))
