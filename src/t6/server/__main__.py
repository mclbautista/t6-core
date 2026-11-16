"""`python -m t6.server`: run the webhook server from environment variables."""

from __future__ import annotations

import os
import secrets
import sys
import time
from collections.abc import Mapping
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


def concern_autoscan_seconds(environ: Mapping[str, str]) -> float | None:
    """Seconds between automatic PM Notes scans, or None when the scan is off (the default)."""
    if environ.get("T6_CONCERN_AUTOSCAN", "").strip().lower() not in {"1", "true", "on", "yes"}:
        return None
    return max(60.0, float(environ.get("T6_CONCERN_AUTOSCAN_SECONDS", "900")))


def _autoscan(api: VerbAPI, seconds: float) -> None:
    while True:
        try:
            api.scan_concerns()
        except Exception as error:  # One bad scan must not stop the next.
            print(f"t6.server: concern scan failed: {type(error).__name__}", file=sys.stderr, flush=True)
        time.sleep(seconds)


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
    inside = None
    if registry is not None:
        # ADR 0006: work that starts inside T6 uses the job table in the t6 database, which the main worker polls.
        from t6.server.handover import hand_over_inside_jobs
        from t6.worker.internal_queue import InternalJobQueue

        inside = InternalJobQueue(registry)
        hand_over_inside_jobs(queue, inside, lambda line: print(line, file=sys.stderr, flush=True))
    signing_key = environ.get("T6_PREVIEW_SIGNING_KEY") or secrets.token_urlsafe(32)
    operator = None
    if registry is not None and config.operator_ids:
        store = ActionStore(environ.get("T6_OPERATOR_DB", "t6-operator.sqlite"))
        frameio = None
        read_url, read_secret = (environ.get("T6_FRAMEIO_READ_URL") or "").strip(), environ.get("T6_FRAMEIO_READ_SECRET") or ""
        if read_url and read_secret:  # the Link Frame.io project button reads the T6 account's projects through the Frame.io worker
            from t6.frameio.client import FrameioReader
            from t6.worker.client import HttpTransport

            frameio = FrameioReader(HttpTransport(read_url, timeout=30), read_secret)
        operator = OperatorService(registry, store, config.operator_ids, people=People(registry, PreviewTokens(signing_key)), frameio=frameio, ttl=config.operator_ttl_seconds)
    app = WebhookApp(queue, config, on_notion_verification=lambda token: print(
        f"Notion verification token: {token}", file=sys.stderr, flush=True
    ), registry=registry, operator=operator, inside=inside)
    if registry is not None:
        sources, privacy_registry = read_config_from_env(environ)
        frameio_queue = None
        queue_url, queue_secret = (environ.get('T6_FRAMEIO_QUEUE_URL') or '').strip(), environ.get('T6_FRAMEIO_QUEUE_SECRET') or ''
        if queue_url and queue_secret:  # Project Start asks the Frame.io worker through the queue server the worker polls
            from t6.worker.client import HttpTransport, QueueClient

            frameio_queue = QueueClient(HttpTransport(queue_url, timeout=30), queue_secret)
        api = VerbAPI(People(registry, PreviewTokens(signing_key),
                             service_permissions_from_env(environ)),
                      queue, secrets_from_env(environ), _open_notion, app,
                      llm_service=service_from_env(environ), data_sources=sources, privacy_registry=privacy_registry,
                      transcribe_rate=float(environ.get('T6_TRANSCRIBE_RATE', '0.4')),
                      frameio_timeout=float(environ.get('T6_FRAMEIO_WRITE_TIMEOUT_SECONDS', '60')), frameio_queue=frameio_queue,
                      translate_enabled=environ.get('T6_TRANSCRIPT_TRANSLATE', '').lower() == 'true',
                      concern_observe_closes=environ.get('T6_CONCERN_OBSERVE_CLOSES', '').strip().lower() in {'1', 'true', 'on', 'yes'},
                      request_log=lambda line: print(line, file=sys.stderr, flush=True))
        interval = concern_autoscan_seconds(environ)
        if interval is not None and api.llm_service is not None:
            threading.Thread(target=_autoscan, args=(api, interval), daemon=True).start()
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
