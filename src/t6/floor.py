"""Floor reads derived from Deliverables, Milestones, Packages and dates: bangko, hand-to-mouth, sabit.

Nothing is stored. Dates are Asia/Manila days. A Deliverable is ready on the earliest of its
`delivered` Milestone date and the day its delivery Package became kasado (t6.package.delivery_kasado_since, which applies the one rule t6.package.is_kasado:
the last blocking checklist item ticked, or the day it was prepared when it has none; Package not rejected). Margins come from config (environment), never from code paths.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from t6.domain.studio_time import MANILA
from t6.package import delivery_kasado_since
from t6.registry.sqlite import SqliteProjectRegistry

BANGKO_VARIABLE = 'T6_BANGKO_DAYS'
HAND_TO_MOUTH_VARIABLE = 'T6_HAND_TO_MOUTH_DAYS'
DEFAULT_BANGKO_DAYS = 3
DEFAULT_HAND_TO_MOUTH_DAYS = 1


@dataclass(frozen=True, slots=True)
class FloorMargins:
    bangko_days: int = DEFAULT_BANGKO_DAYS
    hand_to_mouth_days: int = DEFAULT_HAND_TO_MOUTH_DAYS

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> FloorMargins:
        """Read margins from config; raise ValueError naming the variable when one is not a whole number of days >= 0."""
        values: dict[str, int] = {}
        for field, variable, default in (('bangko_days', BANGKO_VARIABLE, DEFAULT_BANGKO_DAYS),
                                         ('hand_to_mouth_days', HAND_TO_MOUTH_VARIABLE, DEFAULT_HAND_TO_MOUTH_DAYS)):
            raw = environ.get(variable)
            if raw is None or not raw.strip():
                values[field] = default
                continue
            try:
                days = int(raw.strip())
            except ValueError:
                raise ValueError(f'{variable} must be a whole number of days') from None
            if days < 0:
                raise ValueError(f'{variable} must be zero or more days')
            values[field] = days
        return cls(**values)


class FloorReads:
    def __init__(self, registry: SqliteProjectRegistry, margins: FloorMargins | None = None) -> None:
        self.registry, self.margins = registry, margins or FloorMargins()

    def _project(self, db: Any, ref: str | None) -> str | None:
        if ref is None:
            return None
        from t6.deliverable import DeliverableStore
        found = DeliverableStore._project(db, ref)
        if found is None:
            raise LookupError('Project not found')
        return found

    def _ready(self, db: Any, project: str | None) -> list[dict[str, Any]]:
        rows = db.execute('SELECT id, project_id, code, episode, air FROM deliverables WHERE air IS NOT NULL AND air <> ? AND (? IS NULL OR project_id = ?) ORDER BY code, id', ('', project, project)).fetchall()
        ready: list[dict[str, Any]] = []
        for identifier, project_id, code, episode, air in rows:
            signals: list[tuple[str, str]] = []
            delivered = db.execute("SELECT MIN(occurred_on) FROM milestones WHERE deliverable_id = ? AND milestone = 'delivered'", (identifier,)).fetchone()[0]
            if delivered:
                signals.append((delivered[:10], 'delivered'))
            since = delivery_kasado_since(db, identifier)
            if since is not None:
                signals.append((datetime.fromtimestamp(since, MANILA).date().isoformat(), 'kasado'))
            if not signals:
                continue
            on, via = min(signals)
            ready.append({'deliverable_id': identifier, 'project_id': project_id, 'code': code, 'episode': episode,
                          'air': air, 'ready_on': on, 'via': via,
                          'days_ahead': (date.fromisoformat(air[:10]) - date.fromisoformat(on)).days})
        return ready

    def bangko(self, project: str | None = None) -> list[dict[str, Any]]:
        with self.registry._transaction() as db:
            return [item for item in self._ready(db, self._project(db, project)) if item['days_ahead'] >= self.margins.bangko_days]

    def hand_to_mouth(self, project: str | None = None) -> list[dict[str, Any]]:
        with self.registry._transaction() as db:
            return [item for item in self._ready(db, self._project(db, project)) if 0 <= item['days_ahead'] <= self.margins.hand_to_mouth_days]

    def sabit(self, project: str | None = None) -> list[dict[str, Any]]:
        """Deliverables with a recorded miss. A date move with a reason is delayed and never listed."""
        with self.registry._transaction() as db:
            scope = self._project(db, project)
            rows = db.execute('SELECT d.id, d.project_id, d.code, d.episode, d.due, d.air, m.reason, m.recorded_at FROM deliverable_misses m JOIN deliverables d ON d.id = m.deliverable_id '
                              'WHERE (? IS NULL OR d.project_id = ?) ORDER BY d.code, d.id, m.rowid', (scope, scope)).fetchall()
        latest = {row[0]: row for row in rows}  # one entry per Deliverable: the latest miss (rows run oldest first)
        rows = list(latest.values())
        return [{'deliverable_id': r[0], 'project_id': r[1], 'code': r[2], 'episode': r[3], 'due': r[4], 'air': r[5],
                 'reason': r[6], 'missed_on': datetime.fromtimestamp(r[7], MANILA).date().isoformat()} for r in rows]
