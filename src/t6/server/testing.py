"""Test helpers: a clock the test moves by hand."""

from __future__ import annotations

__all__ = ["FakeClock"]


class FakeClock:
    """A callable returning seconds since the epoch. Time moves only on `advance`."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds
