"""`python -m t6.worker`: poll one named queue from environment variables."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import os
import signal
import sys
import threading

from t6.worker.client import HttpTransport, QueueClient, Transport
from t6.worker.gateway import GatewayClient
from t6.worker.operator import CardDelivery, OperatorClient
from t6.worker.runner import Handler, Worker

HANDLERS: dict[str, dict[str, Handler]] = {}
"""Handlers by queue name, then by job kind, that need no configuration.

A worker refuses to start for a queue with no handler: it would claim jobs and
fail them all. Handlers that need settings are built by `configured_handlers`.
"""

DEFAULT_POLL_SECONDS = 5.0


class ConfigError(Exception):
    """The environment or the handler table cannot start a worker."""


@dataclass(frozen=True, slots=True)
class WorkerSettings:
    server_url: str
    secret: str
    queue: str
    poll_seconds: float = DEFAULT_POLL_SECONDS

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> WorkerSettings:
        names = ("T6_SERVER_URL", "T6_WORKER_SECRET", "T6_WORKER_QUEUE")
        missing = [name for name in names if not environ.get(name)]
        if missing:
            raise ConfigError(f"missing environment variable: {', '.join(missing)}")
        raw_poll = environ.get("T6_WORKER_POLL_SECONDS", "")
        try:
            poll = float(raw_poll) if raw_poll else DEFAULT_POLL_SECONDS
        except ValueError:
            poll = -1.0
        if not poll > 0:
            raise ConfigError(f"T6_WORKER_POLL_SECONDS must be a positive number, got {raw_poll!r}")
        return cls(environ["T6_SERVER_URL"], environ["T6_WORKER_SECRET"], environ["T6_WORKER_QUEUE"], poll)


def configured_handlers(
    environ: Mapping[str, str], settings: WorkerSettings, transport: Transport
) -> dict[str, dict[str, Handler]]:
    """Handlers that need settings from the environment.

    With `T6_NOTION_TOKEN` and `T6_NOTION_PROJECTS_DATA_SOURCE` set, the main
    queue gets the Project registry's Notion handlers: `registry.sync` and
    `notion.page_changed` (needs the `notion` extra). With `T6_GATEWAY_URL` and
    `T6_GATEWAY_SECRET` set, it also gets `operator.deliver` (operator cards in
    Sixto), and a Projects page no Project owns is offered to the operator.
    """
    if settings.queue != "main":
        return {}
    handlers: dict[str, Handler] = {}
    gateway_url, gateway_secret = environ.get("T6_GATEWAY_URL"), environ.get("T6_GATEWAY_SECRET")
    operator = OperatorClient(transport, settings.secret)
    if gateway_url and gateway_secret:
        handlers.update(CardDelivery(operator, GatewayClient(HttpTransport(gateway_url), gateway_secret)).handlers())
    token, projects = environ.get("T6_NOTION_TOKEN"), environ.get("T6_NOTION_PROJECTS_DATA_SOURCE")
    if token and projects:
        from t6.notion import NotionClient
        from t6.notion.projection import ProjectProjection
        from t6.worker.registry import RegistryClient

        offer = operator.offer_unbound_page if gateway_url and gateway_secret else None
        projection = ProjectProjection(
            NotionClient(token), RegistryClient(transport, settings.secret), projects, offer=offer
        )
        handlers.update(projection.handlers())
    return {"main": handlers} if handlers else {}


def serve(worker: Worker, stop: threading.Event, poll_seconds: float) -> None:
    """Run jobs until `stop` is set; sleep `poll_seconds` after an empty poll."""
    while not stop.is_set():
        if not worker.run_once():
            stop.wait(poll_seconds)


def main(
    environ: Mapping[str, str] | None = None,
    handlers: Mapping[str, Mapping[str, Handler]] | None = None,
    transport: Transport | None = None,
    stop: threading.Event | None = None,
) -> int:
    environ = os.environ if environ is None else environ
    try:
        settings = WorkerSettings.from_env(environ)
        transport = transport or HttpTransport(settings.server_url)
        table = handlers if handlers is not None else {**HANDLERS, **configured_handlers(environ, settings, transport)}
        queue_handlers = table.get(settings.queue)
        if not queue_handlers:
            raise ConfigError(f"no handler is registered for queue {settings.queue!r}")
    except ConfigError as error:
        print(f"t6.worker: {error}", file=sys.stderr)
        return 2
    if stop is None:
        stop = threading.Event()
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, lambda _signum, _frame: stop.set())
    client = QueueClient(transport, settings.secret)
    serve(Worker(client, settings.queue, queue_handlers), stop, settings.poll_seconds)
    return 0


if __name__ == "__main__":
    sys.exit(main())
