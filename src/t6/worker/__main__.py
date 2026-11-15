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
from urllib.error import URLError

from t6.domain.events import JobKind, JsonValue
from t6.frameio.readapi import ReadApi
from t6.frameio.requests import FrameioRequest, ShareLinks, ask
from t6.media_links import MediaLinks
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
"""How long the review message waits for the Frame.io answer to the file and comments reads."""
SHARE_ASK_SECONDS = 60.0
"""How long it waits for the share links: a cold lookup reads a project's share list and up to 30 asset lists (#301)."""
MAIN_LEASE_SECONDS = 150.0
"""The main queue's lease: the review message's two short asks and the share ask (15 + 15 + 60) fit inside it."""
FRAMEIO_LEASE_SECONDS = 120.0
"""The Frame.io queue's lease: a cold share lookup, with a rate-limit wait or two, fits inside it."""


def review_ask_seconds(request: FrameioRequest) -> float:
    """How long the review message waits for the answer to `request`."""
    return SHARE_ASK_SECONDS if isinstance(request, ShareLinks) else REVIEW_ASK_SECONDS


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


def registry_transport(environ: Mapping[str, str], queue_transport: Transport) -> Transport:
    """Where the main worker sends registry and operator calls: the t6 API at `T6_API_URL`.

    The queue server (`T6_SERVER_URL`) is the VPS edge in production and has no registry: it answers 404,
    which `RegistryClient` reads as "no such row". So with `T6_DATABASE` set (the worker sits with the t6
    database, next to the API) a missing `T6_API_URL` is a `ConfigError`, never a fall back to the queue
    server. Without `T6_DATABASE` the worker is in a one-server setup, where the queue server is the API.
    """
    url = (environ.get("T6_API_URL") or "").strip()
    if url:
        return HttpTransport(url)
    if environ.get("T6_DATABASE"):
        raise ConfigError("missing environment variable: T6_API_URL (the t6 API; the queue server has no registry)")
    return queue_transport


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

    `transport` (`T6_SERVER_URL`) is for queue calls only. Every registry and operator call goes to
    the API (`registry_transport`).
    """
    if settings.queue != "main":
        return {}
    api = registry_transport(environ, transport)
    handlers: dict[str, Handler] = {}
    gateway_url, gateway_secret = environ.get("T6_GATEWAY_URL"), environ.get("T6_GATEWAY_SECRET")
    operator = OperatorClient(api, settings.secret)
    if gateway_url and gateway_secret:
        gateway = GatewayClient(HttpTransport(gateway_url), gateway_secret)
        handlers.update(CardDelivery(operator, gateway).handlers())
        from t6.domain.studio_people import operator_ids
        from t6.worker.operator import StateEditNotification
        handlers['notion.state_flagged'] = StateEditNotification(gateway, operator_ids(environ)).deliver
    token, projects = environ.get("T6_NOTION_TOKEN"), environ.get("T6_NOTION_PROJECTS_DATA_SOURCE")
    if token and projects:
        from t6.notion import NotionClient
        from t6.notion.projection import ProjectProjection
        from t6.worker.registry import RegistryClient

        offer = operator.offer_unbound_page if gateway_url and gateway_secret else None
        projection = ProjectProjection(
            NotionClient(token), RegistryClient(api, settings.secret), projects, offer=offer
        )
        handlers.update(projection.handlers())
    job_orders = environ.get("T6_NOTION_JOB_ORDERS_DATA_SOURCE")
    if token and job_orders:
        from t6.notion import NotionClient
        from t6.notion.jo_projection import JobOrderProjection
        from t6.worker.registry import RegistryClient
        jo_projection = JobOrderProjection(NotionClient(token), RegistryClient(api, settings.secret), job_orders)
        project_reconcile = handlers.get("notion.page_changed")
        handlers.update(jo_projection.handlers())
        if project_reconcile is not None:
            def reconcile(job: WorkerJob) -> JsonValue:
                result = project_reconcile(job)
                if isinstance(result, dict) and result.get("outcome") == "not_a_project_page":
                    return jo_projection.reconcile(job)
                return result
            handlers["notion.page_changed"] = reconcile
    deliverables = environ.get('T6_NOTION_DELIVERABLES_DATA_SOURCE')
    if token and deliverables:
        from t6.notion import NotionClient
        from t6.notion.deliverable_projection import DeliverableProjection
        from t6.worker.registry import RegistryClient
        deliverable_projection = DeliverableProjection(NotionClient(token), RegistryClient(api, settings.secret), deliverables)
        handlers['deliverable.sync'] = deliverable_projection.sync
        prior_reconcile = handlers.get('notion.page_changed')
        if prior_reconcile is None:
            handlers['notion.page_changed'] = deliverable_projection.reconcile
        else:
            def reconcile_deliverable(job: WorkerJob) -> JsonValue:
                assert prior_reconcile is not None
                result = prior_reconcile(job)
                if isinstance(result, dict) and result.get('outcome') in ('not_a_project_page', 'not_a_job_order_page'):
                    return deliverable_projection.reconcile(job)
                return result
            handlers['notion.page_changed'] = reconcile_deliverable
    if gateway_url and gateway_secret:
        chat = _whole_number(environ, "T6_REVIEW_CHAT", required=True)
        topic = _whole_number(environ, "T6_REVIEW_TOPIC", required=False)
        assert chat is not None
        queue = QueueClient(transport, settings.secret)
        from t6.worker.registry import RegistryClient

        review_registry = RegistryClient(api, settings.secret)
        from t6.worker.review_target import ReviewTarget

        target = ReviewTarget(review_media_links(environ, review_registry), review_registry)

        track_send = None
        if environ.get("T6_DATABASE"):
            from t6.registry.sqlite import SqliteProjectRegistry
            from t6.worker.linked_chats import LinkedChats
            track_send = LinkedChats(SqliteProjectRegistry(environ["T6_DATABASE"]), gateway).track_send
        is_linked, hold, is_held = None, None, None
        if environ.get("T6_DATABASE"):
            # Review activity on a Frame.io project no show holds waits in the t6 database (this worker opens it)
            # and one operator card per project asks for the show (#272).
            from t6.frameio_hold import HeldActivity
            from t6.registry.sqlite import SqliteProjectRegistry
            from t6.worker.review_hold import HoldUnlinked

            held = HeldActivity(SqliteProjectRegistry(environ["T6_DATABASE"]))
            is_linked, is_held = target.linked, held.has
            hold = HoldUnlinked(held, operator.offer_frameio_link)
        review = ReviewMessage(
            GatewayClient(HttpTransport(gateway_url), gateway_secret),
            chat,
            topic,
            lambda request: ask(queue, request, timeout=review_ask_seconds(request)),
            target=target,
            version=review_registry.review_version,
            track_send=track_send,
            is_linked=is_linked,
            hold=hold,
            is_held=is_held,
        )
        handlers.update(review.handlers())
    if environ.get("T6_DATABASE"):
        handlers["booking.calendar_sync"] = booking_calendar_handler(environ)
        handlers.update(inbound_calendar_handlers(environ))
        handlers.update(sharing_calendar_handlers(environ))
        if environ.get("T6_PREVIEW_SIGNING_KEY"):
            handlers["project_start.preview"] = project_start_handler(environ)
        else:
            print(
                "t6.worker: `project start` plans are off: set T6_PREVIEW_SIGNING_KEY (the API's preview signing key) to turn them on",
                file=sys.stderr,
                flush=True,
            )
        read_url, read_secret = (environ.get("T6_FRAMEIO_READ_URL") or "").strip(), environ.get("T6_FRAMEIO_READ_SECRET") or ""
        if read_url and read_secret:
            handlers[JobKind.FRAMEIO_FILE_VERSIONED.value] = file_versions_handler(environ)
        else:
            print(
                "t6.worker: Versions from Frame.io events are off: set T6_FRAMEIO_READ_URL and T6_FRAMEIO_READ_SECRET "
                "(the Frame.io worker's read API) to turn them on",
                file=sys.stderr,
                flush=True,
            )
    return {"main": handlers} if handlers else {}


def file_versions_handler(environ: Mapping[str, str]) -> Handler:
    """`frameio.file_versioned`: records a Version for a file landing in a Deliverable's T6 Frame.io folder (#265).

    It opens the t6 database, so it is the main worker's. The folder of the file comes from the Frame.io worker's
    read API (`T6_FRAMEIO_READ_URL`, `T6_FRAMEIO_READ_SECRET`); the caller registers it only when both are set.
    """
    from t6.frameio.client import FrameioReader
    from t6.frameio.file_versions import FrameioFileVersions
    from t6.registry.sqlite import SqliteProjectRegistry

    reader = FrameioReader(HttpTransport(environ["T6_FRAMEIO_READ_URL"].strip(), timeout=60), environ["T6_FRAMEIO_READ_SECRET"])

    def handle(job: WorkerJob) -> JsonValue:
        return FrameioFileVersions(SqliteProjectRegistry(environ["T6_DATABASE"]), reader).handle(job)

    return handle


def project_start_handler(environ: Mapping[str, str]) -> Handler:
    """`project_start.preview` (an internal job): builds the plan of a `project start` and answers with its Preview.

    It opens the t6 database, so it is the main worker's. The Project Type comes from Notion (`T6_NOTION_TOKEN`);
    the Preview token is signed with `T6_PREVIEW_SIGNING_KEY`, the API's key.
    """
    from t6.preview import PreviewTokens
    from t6.project_start import ProjectStartStore
    from t6.registry.sqlite import SqliteProjectRegistry

    def project_type(page: str) -> str | None:
        token = environ.get("T6_NOTION_TOKEN")
        if not token:
            raise ValueError("missing environment variable: T6_NOTION_TOKEN (the plan reads the Project Type)")
        from t6.notion import NotionClient, NotionError
        from t6.notion.generated import ProjectsRow

        try:
            with NotionClient(token) as client:
                return ProjectsRow.from_page(client.retrieve_page(page)).project_type
        except NotionError as error:
            raise ValueError(f"could not read the Project Type from Notion: {error}") from error

    def project_name(page: str) -> str | None:
        token = environ.get("T6_NOTION_TOKEN")
        if not token:
            raise ValueError("missing environment variable: T6_NOTION_TOKEN (the plan reads the Project name for Frame.io)")
        from t6.notion import NotionClient, NotionError
        from t6.notion.generated import ProjectsRow

        try:
            with NotionClient(token) as client:
                return ProjectsRow.from_page(client.retrieve_page(page)).name
        except NotionError as error:
            raise ValueError(f"could not read the Project name from Notion: {error}") from error

    store = ProjectStartStore(SqliteProjectRegistry(environ["T6_DATABASE"]), PreviewTokens(environ["T6_PREVIEW_SIGNING_KEY"]), project_type, project_name)
    return lambda job: store.run_preview(job.payload)


def booking_calendar_handler(environ: Mapping[str, str]) -> Handler:
    """`booking.calendar_sync` (an internal job): writes Booking events to the Bay calendars when they are set up.

    With no calendar configured the job finishes with a result that says so. The event content needs
    the Project's and the Person's names, so calendars without `T6_NOTION_TOKEN` are a `ConfigError`.
    """
    from t6.google.sync import Names, calendar_handler
    from t6.registry.sqlite import SqliteProjectRegistry

    def names(registry: SqliteProjectRegistry) -> Names:
        token = environ.get("T6_NOTION_TOKEN")
        if not token:
            raise ValueError("missing environment variable: T6_NOTION_TOKEN (the calendar events carry Project and Person names)")
        from t6.google.names import NotionNames
        from t6.notion import NotionClient

        return NotionNames(registry, NotionClient(token))

    try:
        return calendar_handler(environ, lambda: SqliteProjectRegistry(environ["T6_DATABASE"]), names)
    except ValueError as error:
        raise ConfigError(str(error)) from error


def inbound_calendar_handlers(environ: Mapping[str, str]) -> dict[str, Handler]:
    """`google.calendar_changed` (the edge's notification job) and `google.renew_channels` (an internal daily job).

    Both finish with a result that says so when the Bay calendars, the credential, `T6_GOOGLE_NOTIFY_URL` or
    `T6_GOOGLE_CHANNEL_TOKEN` is missing. The names and emails come from Notion, so `T6_NOTION_TOKEN` is required with them.
    """
    from t6.google.inbound import inbound_handlers
    from t6.registry.sqlite import SqliteProjectRegistry

    def names(registry: SqliteProjectRegistry) -> object:
        token = environ.get("T6_NOTION_TOKEN")
        if not token:
            raise ValueError("missing environment variable: T6_NOTION_TOKEN (inbound calendar edits map the editor's email to a Person)")
        from t6.google.names import NotionNames
        from t6.notion import NotionClient

        return NotionNames(registry, NotionClient(token))

    try:
        return inbound_handlers(environ, lambda: SqliteProjectRegistry(environ["T6_DATABASE"]), names)
    except ValueError as error:
        raise ConfigError(str(error)) from error


def sharing_calendar_handlers(environ: Mapping[str, str]) -> dict[str, Handler]:
    """`booking.calendar_sharing` (queued by `person grant` and `person revoke` for `bookings`) and `google.reconcile_sharing` (daily, internal).

    Both finish with a result that says so when the Bay calendars or the credential is missing. The emails come from
    Notion, so `T6_NOTION_TOKEN` is required with them.
    """
    from t6.google.sharing import sharing_handlers
    from t6.registry.sqlite import SqliteProjectRegistry

    def identities(registry: SqliteProjectRegistry) -> object:
        token = environ.get("T6_NOTION_TOKEN")
        if not token:
            raise ValueError("missing environment variable: T6_NOTION_TOKEN (calendar sharing maps a Person to a Google email)")
        from t6.google.names import NotionNames
        from t6.notion import NotionClient

        return NotionNames(registry, NotionClient(token))

    try:
        return sharing_handlers(environ, lambda: SqliteProjectRegistry(environ["T6_DATABASE"]), identities)
    except ValueError as error:
        raise ConfigError(str(error)) from error


def review_media_links(environ: Mapping[str, str], registry: MediaLinks) -> MediaLinks:
    """Where the review message reads Media locations: the t6 database when `T6_DATABASE` is set.

    The queue server this worker polls is the VPS edge in production and has no
    registry, so only a worker without `T6_DATABASE` falls back to the HTTP route.
    """
    if not environ.get("T6_DATABASE"):
        return registry
    from t6.media import DatabaseMediaLinks
    from t6.registry.sqlite import SqliteProjectRegistry

    return DatabaseMediaLinks(SqliteProjectRegistry(environ["T6_DATABASE"]))


def frameio_backend(environ: Mapping[str, str]) -> tuple[Tokens, Run]:
    """The Frame.io worker's token store and API.

    The token store signs in to Adobe with the OAuth Web App credential in the
    environment and keeps the token in `T6_FRAMEIO_TOKEN_FILE`. With
    `T6_API_URL` (the t6 API on Sixto, not the queue server) and
    `T6_REGISTRY_READ_SECRET` (its read-only registry grant), used for this client
    only, the briefing feed finds each Frame.io project's Project in the API's
    Media locations; one without the other is a `ConfigError`. `T6_WORKER_SECRET`
    is not used for it. It writes nothing to Notion; without them the other reads
    still work.
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

    from t6.worker.registry import RegistryClient

    links = None
    feed = [name for name in ("T6_API_URL", "T6_REGISTRY_READ_SECRET") if environ.get(name)]
    if feed and len(feed) < 2:
        raise ConfigError("the briefing feed needs both T6_API_URL and T6_REGISTRY_READ_SECRET")
    if feed:
        links = RegistryClient(HttpTransport(environ["T6_API_URL"]), environ["T6_REGISTRY_READ_SECRET"])
    from t6.frameio.api import FrameioApi
    from t6.frameio.reads import FrameioReads

    from t6.frameio.reads import SHARE_CACHE_SECONDS, SHARE_SCAN_LIMIT

    reads = FrameioReads(
        FrameioApi(tokens),
        links,
        environ.get("T6_FRAMEIO_ACCOUNT_ID") or None,
        workspace_id=environ.get("T6_FRAMEIO_WORKSPACE_ID") or None,
        share_cache_seconds=_number_setting(environ, "T6_FRAMEIO_SHARE_CACHE_SECONDS", SHARE_CACHE_SECONDS, minimum=0),
        share_scan_limit=int(_number_setting(environ, "T6_FRAMEIO_SHARE_SCAN_LIMIT", SHARE_SCAN_LIMIT, minimum=1)),
    )
    return tokens, reads


def _number_setting(environ: Mapping[str, str], name: str, default: float, *, minimum: float) -> float:
    """A number from the environment, `default` when it is unset or empty, an error when it is below `minimum`."""
    raw = (environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from None
    if not value >= minimum or value == float("inf"):
        raise ConfigError(f"{name} must be a number of at least {minimum:g}, got {raw!r}")
    return value


def frameio_worker(
    environ: Mapping[str, str], tokens: Tokens, run: Run, timers: ArmReviewTimer | None = None
) -> FrameioWorker:
    """The Frame.io worker, with its sign-in alert going to one Telegram chat through the gateway.

    `T6_FRAMEIO_ALERT_TOPIC` (optional) is the forum topic in that chat; unset means General.

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
    topic = _whole_number(environ, "T6_FRAMEIO_ALERT_TOPIC", required=False)
    gateway = GatewayClient(HttpTransport(environ["T6_GATEWAY_URL"]), environ["T6_GATEWAY_SECRET"])

    def notify(text: str, key: str) -> None:
        gateway.notify(chat, html.escape(text, quote=False), [], key, topic)

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


MAX_RETRY_SECONDS = 60.0


def serve(
    worker: Worker, stop: threading.Event, poll_seconds: float, tick: Callable[[], None] | None = None
) -> None:
    """Run jobs until `stop` is set; sleep `poll_seconds` after an empty poll.

    `tick` runs before every poll, so it runs at least once every `poll_seconds`
    plus the length of one job.

    When the queue server cannot be reached (it restarts during a deploy, or the
    network drops), the worker waits and tries again, doubling the wait from
    `poll_seconds` up to `MAX_RETRY_SECONDS`, instead of exiting. A refused
    secret (`QueueRejected`) still stops it: that needs an operator.
    """
    retry = 0.0
    while not stop.is_set():
        if tick is not None:
            tick()
        try:
            busy = worker.run_once()
        except (OSError, URLError) as error:
            retry = min(MAX_RETRY_SECONDS, retry * 2 if retry else max(poll_seconds, 1.0))
            print(f"t6.worker: queue unreachable ({error}); retrying in {retry:g}s", file=sys.stderr, flush=True)
            stop.wait(retry)
            continue
        retry = 0.0
        if not busy:
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
        transcribe_client = None
        if settings.queue == 'transcribe':
            from t6.transcribe.worker import TranscribeQueue, configured
            transcribe_client = TranscribeQueue(transport, settings.secret)
            if handlers is None:
                try:
                    queue_handlers = configured(environ, transcribe_client)
                except (ValueError, ImportError) as error:
                    raise ConfigError(str(error)) from error
        if settings.queue == FRAMEIO_QUEUE and handlers is None:
            frameio = frameio or frameio_worker(
                environ, *frameio_backend(environ), timers=ReviewTimerClient(transport, settings.secret).arm
            )
            queue_handlers, tick = frameio.handlers(), frameio.tick
            read_api = read_api_server(environ, frameio)
        if not queue_handlers and settings.queue == "main" and environ.get("T6_DATABASE"):
            queue_handlers = {}
        if queue_handlers is None or (not queue_handlers and not (settings.queue == "main" and environ.get("T6_DATABASE"))):
            raise ConfigError(f"no handler is registered for queue {settings.queue!r}")
    except ConfigError as error:
        print(f"t6.worker: {error}", file=sys.stderr)
        return 2
    if stop is None:
        stop = threading.Event()
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, lambda _signum, _frame: stop.set())
    client = transcribe_client if transcribe_client is not None else QueueClient(transport, settings.secret)
    internal_client = None
    if settings.queue == 'main' and environ.get("T6_DATABASE"):
        from t6.worker.internal_queue import InternalJobQueue, InternalQueueClient

        internal_client = InternalQueueClient(InternalJobQueue(environ["T6_DATABASE"]))
    if internal_client is not None and settings.queue == "main":
        from t6.concern import ConcernStore
        from t6.preview import PreviewTokens
        from t6.registry.sqlite import SqliteProjectRegistry

        concerns = ConcernStore(SqliteProjectRegistry(environ["T6_DATABASE"]), PreviewTokens("timer"))
        def concern_silence(job: WorkerJob) -> bool:
            concern, evidence, target = job.payload["concern"], job.payload["evidence_at"], job.payload["target"]
            if not isinstance(concern, str) or not isinstance(evidence, (int, float)) or not isinstance(target, str):
                raise ValueError("Invalid Concern timer payload")
            return concerns.silence(concern, evidence, target)
        def concern_pm_close(job: WorkerJob) -> bool:
            project = job.payload.get('project')
            if not isinstance(project, str):
                raise ValueError('Invalid Concern close payload')
            concerns.apply_pending(project)
            return True
        from t6.frameio.share_views import DeliveryShareViews, share_viewed_handler

        share_viewed = share_viewed_handler(DeliveryShareViews(SqliteProjectRegistry(environ["T6_DATABASE"])))
        queue_handlers = {**queue_handlers, "concern_silence": concern_silence, "concern_pm_close": concern_pm_close,
                          JobKind.FRAMEIO_SHARE_VIEWED.value: share_viewed}
        def unavailable_abang_nudge(job: WorkerJob) -> bool:
            raise RuntimeError('Abang nudge needs a configured gateway')
        def unavailable_subscriptions(job: WorkerJob) -> bool:
            raise RuntimeError('Chat subscriptions need a configured gateway')
        queue_handlers = {**queue_handlers, "abang_nudge": unavailable_abang_nudge, "chat_subscriptions": unavailable_subscriptions}
    if settings.queue == "main" and environ.get("T6_DATABASE") and environ.get("T6_GATEWAY_URL") and environ.get("T6_GATEWAY_SECRET"):
        from t6.registry.sqlite import SqliteProjectRegistry
        from t6.worker.linked_chats import LinkedChats
        from t6.domain.studio_people import operator_ids
        from t6.worker.gateway import GatewayRejected
        from urllib.error import URLError

        linked = LinkedChats(SqliteProjectRegistry(environ["T6_DATABASE"]), GatewayClient(HttpTransport(environ["T6_GATEWAY_URL"]), environ["T6_GATEWAY_SECRET"]), operator_ids(environ))
        queue_handlers = {**queue_handlers, "abang_nudge": linked.nudge, "chat_subscriptions": linked.replace_subscriptions}
        def linked_tick() -> None:
            try:
                linked.tick()
            except (OSError, URLError, GatewayRejected):
                pass  # Retry on the next worker poll.
        tick = linked_tick
    if read_api is not None:
        threading.Thread(target=read_api.serve_forever, daemon=True).start()
    try:
        lease = {"main": MAIN_LEASE_SECONDS, "frameio": FRAMEIO_LEASE_SECONDS}.get(settings.queue, 60.0)
        serve(
            Worker(client, settings.queue, queue_handlers, lease_seconds=lease, internal_client=internal_client),
            stop,
            settings.poll_seconds,
            tick,
        )
    finally:
        if read_api is not None:
            read_api.shutdown()
            read_api.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
