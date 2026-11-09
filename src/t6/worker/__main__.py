"""`python -m t6.worker`: poll one named queue from environment variables."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import os
import signal
import sys
import threading

from t6.worker.client import HttpTransport, QueueClient, Transport
from t6.worker.runner import Handler, Worker

HANDLERS: dict[str, dict[str, Handler]] = {}
"""Handlers by queue name, then by job kind. Later tickets register theirs here.

A worker refuses to start for a queue with no handler: it would claim jobs and
fail them all.
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
    table = HANDLERS if handlers is None else handlers
    try:
        settings = WorkerSettings.from_env(environ)
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
    client = QueueClient(transport or HttpTransport(settings.server_url), settings.secret)
    serve(Worker(client, settings.queue, queue_handlers), stop, settings.poll_seconds)
    return 0


if __name__ == "__main__":
    sys.exit(main())
