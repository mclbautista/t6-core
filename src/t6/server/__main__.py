"""`python -m t6.server`: run the webhook server from environment variables."""

from __future__ import annotations

import os
import sys

from t6.operator import ActionStore, OperatorService
from t6.registry import SqliteProjectRegistry
from t6.server.app import ServerConfig, WebhookApp
from t6.server.http import serve
from t6.server.queue import JobQueue


def _address(value: str) -> tuple[str, int]:
    host, _, port = value.rpartition(":")
    return host, int(port)


def _print_notion_token(token: str) -> None:
    """Notion sends its verification token once; the operator copies it from the log."""
    print(f"Notion verification token: {token}", file=sys.stderr, flush=True)


def main() -> int:
    environ = os.environ
    queue = JobQueue(environ.get("T6_QUEUE_DB", "t6-queue.sqlite"))
    registry = SqliteProjectRegistry(environ.get("T6_REGISTRY_DB", "t6-registry.sqlite"))
    config = ServerConfig.from_env(environ)
    operator = None
    if config.operator_ids:
        store = ActionStore(environ.get("T6_OPERATOR_DB", "t6-operator.sqlite"))
        operator = OperatorService(registry, store, config.operator_ids, ttl=config.operator_ttl_seconds)
    app = WebhookApp(queue, config, on_notion_verification=_print_notion_token, registry=registry, operator=operator)
    serve(
        app,
        _address(environ.get("T6_PUBLIC_BIND", "127.0.0.1:8080")),
        _address(environ.get("T6_TAILNET_BIND", "127.0.0.1:8081")),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
