"""`python -m t6.worker`: poll one named queue from environment variables."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http.server import ThreadingHTTPServer
import html
import os
from pathlib import Path
import signal
import sys
import threading

from t6.domain.events import JsonValue
from t6.frameio.readapi import ReadApi
from t6.frameio.requests import ask
from t6.frameio.worker import ArmReviewTimer, FrameioWorker, Run, Tokens
from t6.server.http import make_server
from t6.server.queue import DIAGNOSTIC_QUEUE, FRAMEIO_QUEUE
from t6.worker.client import HttpTransport, QueueClient, Transport, WorkerJob
from t6.worker.gateway import GatewayClient
from t6.worker.operator import CardDelivery, OperatorClient
from t6.worker.review import ReviewMessage
from t6.worker.review_timer import ReviewTimerClient
from t6.worker.runner import Handler, Worker

def diagnostic_echo(job: WorkerJob) -> dict[str, JsonValue]:
    return dict(job.payload)


HANDLERS: dict[str, dict[str, Handler]] = {DIAGNOSTIC_QUEUE: {"diagnostic.echo": diagnostic_echo}}
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


REVIEW_ASK_SECONDS = 15.0
"""How long the review message waits for each Frame.io answer: three asks stay inside one 60-second lease."""


def _whole_number(environ: Mapping[str, str], name: str, *, required: bool) -> int | None:
    raw = (environ.get(name) or "").strip()
    if not raw:
        if required:
            raise ConfigError(f"missing environment variable: {name}")
        return None
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a Telegram ID (a whole number)") from None


def configured_handlers(
    environ: Mapping[str, str], settings: WorkerSettings, transport: Transport
) -> dict[str, dict[str, Handler]]:
    """Handlers that need settings from the environment.

    With `T6_NOTION_TOKEN` and `T6_NOTION_PROJECTS_DATA_SOURCE` set, the main
    queue gets the Project registry's Notion handlers: `registry.sync` and
    `notion.page_changed` (needs the `notion` extra). With `T6_GATEWAY_URL` and
    `T6_GATEWAY_SECRET` set, it also gets `operator.deliver` (operator cards in
    Sixto), and a Projects page no Project owns is offered to the operator.
    The gateway settings also give it `frameio.review_done` (a message to the
    review chat when a person goes quiet on a Frame.io file), which needs
    `T6_REVIEW_CHAT` (and may have `T6_REVIEW_TOPIC`).
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
    job_orders = environ.get("T6_NOTION_JOB_ORDERS_DATA_SOURCE")
    if token and job_orders:
        from t6.notion import NotionClient
        from t6.notion.jo_projection import JobOrderProjection
        from t6.worker.registry import RegistryClient
        handlers.update(JobOrderProjection(NotionClient(token), RegistryClient(transport, settings.secret), job_orders).handlers())
    if gateway_url and gateway_secret:
        chat = _whole_number(environ, "T6_REVIEW_CHAT", required=True)
        topic = _whole_number(environ, "T6_REVIEW_TOPIC", required=False)
        assert chat is not None
        queue = QueueClient(transport, settings.secret)
        review = ReviewMessage(
            GatewayClient(HttpTransport(gateway_url), gateway_secret),
            chat,
            topic,
            lambda request: ask(queue, request, timeout=REVIEW_ASK_SECONDS),
        )
        handlers.update(review.handlers())
    return {"main": handlers} if handlers else {}


def frameio_backend(environ: Mapping[str, str]) -> tuple[Tokens, Run]:
    """The Frame.io worker's token store and API.

    The token store signs in to Adobe with the OAuth Web App credential in the
    environment and keeps the token in `T6_FRAMEIO_TOKEN_FILE`. With
    `T6_NOTION_TOKEN` and `T6_NOTION_EXTERNAL_LINKS_DATA_SOURCE` set, the
    briefing feed works and live share links get External Links rows (needs
    the `notion` extra); without them the other reads still work.
    """
    names = ("T6_FRAMEIO_CLIENT_ID", "T6_FRAMEIO_CLIENT_SECRET", "T6_FRAMEIO_REDIRECT_URI", "T6_FRAMEIO_TOKEN_FILE")
    missing = [name for name in names if not environ.get(name)]
    if missing:
        raise ConfigError(f"missing environment variable: {', '.join(missing)}")
    if not environ["T6_FRAMEIO_REDIRECT_URI"].startswith("https://"):
        raise ConfigError("T6_FRAMEIO_REDIRECT_URI must be an https:// address (Adobe accepts no other)")
    from t6.frameio.tokens import AdobeTokens

    tokens = AdobeTokens(
        environ["T6_FRAMEIO_CLIENT_ID"],
        environ["T6_FRAMEIO_CLIENT_SECRET"],
        environ["T6_FRAMEIO_REDIRECT_URI"],
        Path(environ["T6_FRAMEIO_TOKEN_FILE"]),
    )

    links = None
    notion_token, data_source = environ.get("T6_NOTION_TOKEN"), environ.get("T6_NOTION_EXTERNAL_LINKS_DATA_SOURCE")
    if notion_token and data_source:
        from t6.notion import NotionClient
        from t6.notion.external_links import ExternalLinks

        links = ExternalLinks(NotionClient(notion_token), data_source)
    from t6.frameio.api import FrameioApi
    from t6.frameio.reads import FrameioReads

    return tokens, FrameioReads(FrameioApi(tokens), links, environ.get("T6_FRAMEIO_ACCOUNT_ID") or None)


def frameio_worker(
    environ: Mapping[str, str], tokens: Tokens, run: Run, timers: ArmReviewTimer | None = None
) -> FrameioWorker:
    """The Frame.io worker, with its sign-in alert going to one Telegram chat through the gateway.

    `timers` starts the review timers for the comments it reads (`main` builds it from the server settings).
    """
    names = ("T6_GATEWAY_URL", "T6_GATEWAY_SECRET", "T6_FRAMEIO_ALERT_CHAT")
    missing = [name for name in names if not environ.get(name)]
    if missing:
        raise ConfigError(f"missing environment variable: {', '.join(missing)}")
    try:
        chat = int(environ["T6_FRAMEIO_ALERT_CHAT"])
    except ValueError:
        raise ConfigError("T6_FRAMEIO_ALERT_CHAT must be a Telegram chat ID (a whole number)") from None
    gateway = GatewayClient(HttpTransport(environ["T6_GATEWAY_URL"]), environ["T6_GATEWAY_SECRET"])

    def notify(text: str, key: str) -> None:
        gateway.notify(chat, html.escape(text, quote=False), [], key)

    return FrameioWorker(tokens, run, notify, arm_review_timer=timers)


def read_api_server(environ: Mapping[str, str], frameio: FrameioWorker) -> ThreadingHTTPServer | None:
    """The Frame.io read API's server, or None when `T6_FRAMEIO_READ_BIND` is unset.

    It needs `T6_FRAMEIO_READ_SECRET` too. Bind it to the LAN or tailnet address, never a public one.
    """
    bind, secret = (environ.get("T6_FRAMEIO_READ_BIND") or "").strip(), environ.get("T6_FRAMEIO_READ_SECRET") or ""
    if not bind:
        return None
    host, _, port = bind.rpartition(":")
    if not host or not port.isdigit():
        raise ConfigError("T6_FRAMEIO_READ_BIND must look like host:port, for example 100.64.0.2:8082")
    if not secret:
        raise ConfigError("missing environment variable: T6_FRAMEIO_READ_SECRET")
    return make_server((host, int(port)), ReadApi(frameio, secret).handle)


def serve(
    worker: Worker, stop: threading.Event, poll_seconds: float, tick: Callable[[], None] | None = None
) -> None:
    """Run jobs until `stop` is set; sleep `poll_seconds` after an empty poll.

    `tick` runs before every poll, so it runs at least once every `poll_seconds`
    plus the length of one job.
    """
    while not stop.is_set():
        if tick is not None:
            tick()
        if not worker.run_once():
            stop.wait(poll_seconds)


def main(
    environ: Mapping[str, str] | None = None,
    handlers: Mapping[str, Mapping[str, Handler]] | None = None,
    transport: Transport | None = None,
    stop: threading.Event | None = None,
    frameio: FrameioWorker | None = None,
) -> int:
    """`frameio` replaces the Frame.io worker the environment would build (tests pass one on fakes)."""
    environ = os.environ if environ is None else environ
    tick: Callable[[], None] | None = None
    read_api: ThreadingHTTPServer | None = None
    try:
        settings = WorkerSettings.from_env(environ)
        transport = transport or HttpTransport(settings.server_url)
        table = handlers if handlers is not None else {**HANDLERS, **configured_handlers(environ, settings, transport)}
        queue_handlers = table.get(settings.queue)
        if settings.queue == FRAMEIO_QUEUE and handlers is None:
            frameio = frameio or frameio_worker(
                environ, *frameio_backend(environ), timers=ReviewTimerClient(transport, settings.secret).arm
            )
            queue_handlers, tick = frameio.handlers(), frameio.tick
            read_api = read_api_server(environ, frameio)
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
    internal_client = None
    if environ.get("T6_DATABASE"):
        from t6.worker.internal_queue import InternalJobQueue, InternalQueueClient

        internal_client = InternalQueueClient(InternalJobQueue(environ["T6_DATABASE"]))
    if read_api is not None:
        threading.Thread(target=read_api.serve_forever, daemon=True).start()
    try:
        serve(Worker(client, settings.queue, queue_handlers, internal_client=internal_client), stop, settings.poll_seconds, tick)
    finally:
        if read_api is not None:
            read_api.shutdown()
            read_api.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
