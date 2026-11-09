"""Webhook server and job queue (the `t6[server]` extra). Standard library only.

The server runs on the VPS. It verifies provider signatures, turns events into
jobs in a SQLite (WAL) queue and serves workers over the tailnet. It never
opens a connection to a worker: workers pull.
"""

from t6.server.app import Grant, Request, Response, ServerConfig, WebhookApp
from t6.server.queue import (
    Claim,
    JobQueue,
    JobResult,
    QueueError,
    Submitted,
)
from t6.server.signatures import WebhookRejected, verify_frameio, verify_notion

__all__ = [
    "Claim",
    "Grant",
    "JobQueue",
    "JobResult",
    "QueueError",
    "Request",
    "Response",
    "ServerConfig",
    "Submitted",
    "WebhookApp",
    "WebhookRejected",
    "verify_frameio",
    "verify_notion",
]
