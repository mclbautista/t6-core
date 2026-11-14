"""`python -m t6.server`: run the webhook server from environment variables."""

from __future__ import annotations

import os
import secrets
import sys
from dataclasses import replace

from t6.operator import ActionStore, OperatorService
from t6.person import People
from t6.preview import PreviewTokens
from t6.registry import SqliteProjectRegistry
from t6.server.app import Request, Response, ServerConfig, WebhookApp
from t6.server.http import serve
from t6.server.queue import JobQueue
from t6.server.routes import load_routes
from t6.server.llm import service_from_env
from t6.server.verbs import VerbAPI, secrets_from_env, service_permissions_from_env, read_config_from_env
from t6.person import People
from t6.preview import PreviewTokens
from t6.cli import _open_notion
from t6.server.http import make_server
import threading


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
        routes = load_routes(filename, environ)
        for route in routes:
            if not route.secret:
                print(f"Notion bootstrap: {route.path} serves verification handshakes only until its secret is configured", file=sys.stderr, flush=True)
        config = replace(config, edge_routes=routes)
    queue = JobQueue(environ.get("T6_QUEUE_DB", "t6-queue.sqlite"))
    registry = None if mode == "edge" else SqliteProjectRegistry(environ.get("T6_REGISTRY_DB", "t6-registry.sqlite"))
    signing_key = environ.get("T6_PREVIEW_SIGNING_KEY") or secrets.token_urlsafe(32)
    operator = None
    if registry is not None and config.operator_ids:
        store = ActionStore(environ.get("T6_OPERATOR_DB", "t6-operator.sqlite"))
        operator = OperatorService(registry, store, config.operator_ids, people=People(registry, PreviewTokens(signing_key)), ttl=config.operator_ttl_seconds)
    app = WebhookApp(queue, config, on_notion_verification=lambda token: print(
        f"Notion verification token: {token}", file=sys.stderr, flush=True
    ), registry=registry, operator=operator)
    if registry is not None:
        sources, privacy_registry = read_config_from_env(environ)
        api = VerbAPI(People(registry, PreviewTokens(signing_key),
                             service_permissions_from_env(environ)),
                      queue, secrets_from_env(environ), _open_notion, app,
                      llm_service=service_from_env(environ), data_sources=sources, privacy_registry=privacy_registry)
        def tailnet(request: Request) -> Response:
            if request.path.split("?", 1)[0].startswith("/v1/") or request.path.split("?", 1)[0] in {"/health", "/tap"}:
                return api(request)
            return app.tailnet(request)
        server = make_server(_address(environ.get("T6_TAILNET_BIND", "127.0.0.1:8081")), tailnet)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        make_server(_address(environ.get("T6_PUBLIC_BIND", "127.0.0.1:8080")), app.public).serve_forever()
        return 0
    serve(
        app,
        _address(environ.get("T6_PUBLIC_BIND", "127.0.0.1:8080")),
        _address(environ.get("T6_TAILNET_BIND", "127.0.0.1:8081")),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
