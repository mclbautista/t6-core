"""`python -m t6.server`: run the webhook server from environment variables."""

from __future__ import annotations

import os
import sys
from dataclasses import replace

from t6.operator import ActionStore, OperatorService
from t6.registry import SqliteProjectRegistry
from t6.server.app import ServerConfig, WebhookApp
from t6.server.http import serve
from t6.server.queue import JobQueue
from t6.server.routes import load_routes


def _address(value: str) -> tuple[str, int]:
    host, _, port = value.rpartition(":")
    return host, int(port)


def main() -> int:
    environ = os.environ
    config = ServerConfig.from_env(environ)
    mode = environ.get("T6_SERVER_MODE", "full")
    if mode not in {"full", "edge"}:
        raise ValueError("T6_SERVER_MODE must be full or edge")
    if mode == "edge":
        filename = environ.get("T6_ROUTES_FILE", "")
        if not filename:
            raise ValueError("T6_ROUTES_FILE is required in edge mode")
        config = replace(config, edge_routes=load_routes(filename, environ))
    queue = JobQueue(environ.get("T6_QUEUE_DB", "t6-queue.sqlite"))
    registry = None if mode == "edge" else SqliteProjectRegistry(environ.get("T6_REGISTRY_DB", "t6-registry.sqlite"))
    operator = None
    if registry is not None and config.operator_ids:
        store = ActionStore(environ.get("T6_OPERATOR_DB", "t6-operator.sqlite"))
        operator = OperatorService(registry, store, config.operator_ids, ttl=config.operator_ttl_seconds)
    app = WebhookApp(queue, config, on_notion_verification=lambda token: print(
        f"Notion verification token: {token}", file=sys.stderr, flush=True
    ), registry=registry, operator=operator)
    serve(
        app,
        _address(environ.get("T6_PUBLIC_BIND", "127.0.0.1:8080")),
        _address(environ.get("T6_TAILNET_BIND", "127.0.0.1:8081")),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
