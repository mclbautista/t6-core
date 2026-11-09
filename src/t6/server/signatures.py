"""Check that a webhook came from the provider and is fresh. No I/O.

Frame.io signs `v0:{timestamp}:{body}` and sends the timestamp in a header.
Notion signs the raw body only; its timestamp is in the event, and a retry can
legitimately arrive up to about a day late, so its age limit is much longer.
Both compare in constant time and sign the exact bytes received.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import hmac
import json

FRAMEIO_TOLERANCE_SECONDS = 300.0
"""Frame.io recommends five minutes. Applied to past and future timestamps."""

NOTION_MAX_AGE_SECONDS = 25 * 3600.0
"""Notion retries for about 24 hours after the trigger. One hour of margin."""

NOTION_MAX_FUTURE_SECONDS = 300.0


class WebhookRejected(Exception):
    """The request must not be queued. `reason` is safe to log; it holds no secret."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _signature_matches(expected: str, received: str) -> bool:
    return hmac.compare_digest(expected.encode(), received.encode())


def verify_frameio(
    secret: str, headers: Mapping[str, str], body: bytes, now: float
) -> None:
    """Raise WebhookRejected unless the Frame.io signature is valid and fresh.

    `headers` keys are lower case.
    """
    timestamp_text = headers.get("x-frameio-request-timestamp", "")
    signature = headers.get("x-frameio-signature", "")
    if not timestamp_text or not signature:
        raise WebhookRejected("missing signature headers")
    try:
        timestamp = int(timestamp_text)
    except ValueError:
        raise WebhookRejected("bad timestamp") from None
    message = b"v0:" + timestamp_text.encode() + b":" + body
    expected = "v0=" + hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    if not _signature_matches(expected, signature):
        raise WebhookRejected("bad signature")
    # Checked after the signature so an unsigned caller learns nothing about timing rules.
    if abs(now - timestamp) > FRAMEIO_TOLERANCE_SECONDS:
        raise WebhookRejected("stale timestamp")


def notion_signature(token: str, body: bytes) -> str:
    return "sha256=" + hmac.new(token.encode(), body, hashlib.sha256).hexdigest()


def verify_notion(
    token: str,
    headers: Mapping[str, str],
    body: bytes,
    now: float,
    max_age: float = NOTION_MAX_AGE_SECONDS,
) -> None:
    """Raise WebhookRejected unless the Notion signature is valid and the event is fresh."""
    signature = headers.get("x-notion-signature", "")
    if not signature:
        raise WebhookRejected("missing signature header")
    if not _signature_matches(notion_signature(token, body), signature):
        raise WebhookRejected("bad signature")
    try:
        event = json.loads(body)
        stamp = datetime.fromisoformat(str(event["timestamp"]).replace("Z", "+00:00"))
    except (ValueError, KeyError, TypeError):
        raise WebhookRejected("missing or bad event timestamp") from None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    age = now - stamp.timestamp()
    if age > max_age:
        raise WebhookRejected("stale timestamp")
    if age < -NOTION_MAX_FUTURE_SECONDS:
        raise WebhookRejected("timestamp in the future")
