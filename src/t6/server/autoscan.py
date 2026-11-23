"""The daily autoscan schedule: a wall-clock run at 21:00 Manila, never at start, with one catch-up for a missed run (#638)."""

from __future__ import annotations

import sys
import time as clocks
from collections.abc import Callable, Mapping
from datetime import datetime, time, timedelta
from typing import Any

from t6.domain.studio_time import MANILA


def autoscan_at(environ: Mapping[str, str]) -> time | None:
    """The Manila time of the daily autoscan, or None when it is off (the default)."""
    if environ.get('T6_CONCERN_AUTOSCAN', '').strip().lower() not in {'1', 'true', 'on', 'yes'}:
        return None
    return time.fromisoformat(environ.get('T6_CONCERN_AUTOSCAN_AT', '21:00').strip())


def latest_slot(now: float, at: time) -> float:
    """The most recent run time (`at`, Manila) at or before `now`."""
    today = datetime.fromtimestamp(now, MANILA)
    slot = datetime.combine(today.date(), at, MANILA)
    return (slot if slot <= today else slot - timedelta(days=1)).timestamp()


def last_completed(registry: Any) -> float | None:
    with registry._transaction() as db:
        row = db.execute('SELECT completed_at FROM concern_autoscan_runs WHERE id = 1').fetchone()
    return row[0] if row else None


def record_completed(registry: Any, at: float) -> None:
    with registry._transaction() as db:
        db.execute('INSERT INTO concern_autoscan_runs VALUES (1, ?) ON CONFLICT (id) DO UPDATE SET completed_at = excluded.completed_at', (at,))


def run_forever(run: Callable[[float], bool], registry: Any, at: time, *,
                clock: Callable[[], float] = clocks.time, sleep: Callable[[float], None] = clocks.sleep) -> None:
    """Call `run(slot)` once for each daily slot that has no completed run; `run` returns True when the run completed.

    At start the most recent slot runs only when no run completed since it: the one catch-up. A failed run is not retried
    until the next slot, and only a completed run is stored."""
    attempted: float | None = None
    while True:
        slot = latest_slot(clock(), at)
        done = last_completed(registry)
        if slot != attempted and (done is None or done < slot):
            attempted = slot
            try:
                completed = run(slot)
            except Exception as error:  # One bad run must not stop the next.
                print(f't6.server: concern scan failed: {type(error).__name__}', file=sys.stderr, flush=True)
                completed = False
            if completed:
                record_completed(registry, clock())
        sleep(max(1.0, latest_slot(clock(), at) + 86400 - clock()))
