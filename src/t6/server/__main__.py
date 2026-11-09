"""`python -m t6.server`: run the webhook server from environment variables."""

from __future__ import annotations

import os
import sys

from t6.server.app import ServerConfig, WebhookApp
from t6.server.http import serve
from t6.server.queue import JobQueue


def _address(value: str) -> tuple[str, int]:
    host, _, port = value.rpartition(":")
    return host, int(port)


def main() -> int:
    environ = os.environ
    queue = JobQueue(environ.get("T6_QUEUE_DB", "t6-queue.sqlite"))
    app = WebhookApp(queue, ServerConfig.from_env(environ))
    serve(
        app,
        _address(environ.get("T6_PUBLIC_BIND", "127.0.0.1:8080")),
        _address(environ.get("T6_TAILNET_BIND", "127.0.0.1:8081")),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
