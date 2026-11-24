"""The Concerns sheet export (#734): one CSV row per Concern that is not closed, and the push of it to the VPS server.

The API builds it at the end of each 21:00 autoscan and pushes it over the tailnet (the edge server stores the latest file and serves it
to the read token). The sheet holds no rules, so the allowed actions are decided here."""
from __future__ import annotations

import csv
import io
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from typing import Any

from t6.domain.studio_time import MANILA
from t6.registry.sqlite import SqliteProjectRegistry
from t6.worker.client import HttpTransport, Transport

DAY = 24 * 60 * 60
COLUMNS = ('Concern id', 'revision', 'Project code', 'Project name', 'Concern text', 'owner', 'waiting on', 'State', 'close requested by', 'close requested at',
           'source', 'quoted line', 'as of', 'last evidence', 'opened', 'days open', 'allowed actions', 'last result')
ACTION_SEPARATOR = ', '
"""Between the actions in the `allowed actions` cell."""
_ROWS = """SELECT c.id, c.revision, c.project_id, COALESCE(NULLIF(c.text, ''), c.anchor), c.owner_id, c.waiting_on, c.source, c.anchor, c.as_of, c.evidence_at, c.opened_at,
           p.actor, p.created_at, p.concern_id IS NOT NULL
    FROM concerns c LEFT JOIN concern_pending_closes p ON p.concern_id = c.id
    WHERE c.state != 'closed' ORDER BY c.opened_at, c.id"""


def allowed_actions(source: str, pending_close: bool) -> tuple[str, ...]:
    """What a Closer may choose for a Concern: a date-rule Concern follows data only, a pending close may be undone, any other may be closed."""
    if source == 'date_rule':
        return ('Keep',)
    return ('Keep', 'Undo close') if pending_close else ('Keep', 'Close')


def _day(at: float) -> str:
    return datetime.fromtimestamp(at, MANILA).date().isoformat()


def concerns_export_csv(registry: SqliteProjectRegistry, projects: Mapping[str, tuple[str, str]], people: Mapping[str, str]) -> str:
    """The export as CSV text with a header row, oldest Concern first, with Manila dates.

    `projects` maps a t6 Project id to its (code, name) and `people` a t6 Person id to a name; an id that is not there shows as it is
    (a close from the sheet is recorded as `sheet:<email>`). `last result` is empty here; the sheet submit job fills it."""
    now = registry._clock()
    with registry._transaction() as db:
        rows = db.execute(_ROWS).fetchall()
    out = io.StringIO(newline='')
    writer = csv.writer(out)
    writer.writerow(COLUMNS)
    for (concern, revision, project, text, owner, waiting, source, anchor, as_of, evidence_at, opened_at, closer, closed_at, pending) in rows:
        code, name = projects.get(project, ('', ''))
        writer.writerow([concern, revision, code, name, text, people.get(owner, owner or ''), waiting, 'Closing after PSR' if pending else 'Open',
                         people.get(closer, closer or ''), _day(closed_at) if pending else '', source, anchor, as_of, _day(evidence_at), _day(opened_at),
                         int(max(0, now - opened_at) // DAY), ACTION_SEPARATOR.join(allowed_actions(source, bool(pending))), ''])
    return out.getvalue()


class ConcernsExportPush:
    """Pushes the export to the VPS server's tailnet route with its bearer secret. Raises when the push does not land; the caller logs it."""

    def __init__(self, transport: Transport, secret: str) -> None:
        self._transport, self._headers = transport, {'Authorization': f'Bearer {secret}', 'Content-Type': 'text/csv; charset=utf-8'}

    def __call__(self, export: str) -> None:
        status, answer = self._transport.send('PUT', '', self._headers, export.encode())
        if status != 200:
            raise RuntimeError(f'export push answered {status}: {answer.get("error", "")}')


def concerns_export_from_env(environ: Mapping[str, str]) -> ConcernsExportPush | None:
    """The push from `T6_CONCERNS_EXPORT_URL` (the full URL of the VPS push route) and `T6_CONCERNS_EXPORT_PUSH_SECRET`; None (off) unless both are set."""
    url, secret = (environ.get('T6_CONCERNS_EXPORT_URL') or '').strip(), environ.get('T6_CONCERNS_EXPORT_PUSH_SECRET') or ''
    return ConcernsExportPush(HttpTransport(url, timeout=30), secret) if url and secret else None
