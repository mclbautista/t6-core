"""Serve `WebhookApp` over real sockets (standard library `http.server`).

Run `python -m t6.server` on the VPS. Two listeners: the public one on
`T6_PUBLIC_BIND` (default 127.0.0.1:8080, behind the Caddy edge) and the
tailnet one on `T6_TAILNET_BIND` (default 127.0.0.1:8081; set it to the
VPS's Tailscale address). Secrets come from the environment.
"""

from __future__ import annotations

from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

from t6.server.app import Request, Response, WebhookApp

MAX_BODY_BYTES = 1_000_000

__all__ = ["make_server"]


def make_server(
    address: tuple[str, int], handle: Callable[[Request], Response]
) -> ThreadingHTTPServer:
    """A server that passes every request to `handle`. Call `serve_forever` on it."""

    class Handler(BaseHTTPRequestHandler):
        def _respond(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY_BYTES:
                answer = Response(413)
            else:
                body = self.rfile.read(length) if length else b""
                answer = handle(Request(self.command, self.path, dict(self.headers.items()), body))
            payload = json.dumps(answer.body).encode()
            self.send_response(answer.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = do_PUT = do_DELETE = _respond  # noqa: N815

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return  # request lines can carry ids; log nothing by default

    return ThreadingHTTPServer(address, Handler)


def serve(app: WebhookApp, public: tuple[str, int], tailnet: tuple[str, int]) -> None:
    servers = [make_server(public, app.public), make_server(tailnet, app.tailnet)]
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers[1:]]
    for thread in threads:
        thread.start()
    servers[0].serve_forever()
