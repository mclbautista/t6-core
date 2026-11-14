"""Test helpers: a clock the test moves by hand."""

from __future__ import annotations

from collections.abc import Callable
from types import TracebackType
from t6.server.app import Request, Response

__all__ = ["FakeClock"]


class FakeClock:
    """A callable returning seconds since the epoch. Time moves only on `advance`."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class HTTPHarness:
    """Run a request handler on a loopback socket for HTTP integration tests."""

    def __init__(self, handle: Callable[[Request], Response]) -> None:
        import threading
        from t6.server.http import make_server
        self.server = make_server(('127.0.0.1', 0), handle)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> HTTPHarness:
        self.thread.start()
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 traceback: TracebackType | None) -> None:
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    @property
    def url(self) -> str:
        return f'http://127.0.0.1:{self.server.server_port}'
