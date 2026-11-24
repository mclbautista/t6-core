"""The Concerns sheet's Submit, applied by the main worker as job kind `sheets.concerns_submit` (#735, spec #732).

The edge verifies the signed POST to `/webhook/sheets/concerns` and queues its JSON body unchanged:

    {"id": "<unique per click>", "email": "<clicker's Google email>",
     "rows": [{"concern": "<Concern id>", "revision": <revision seen>, "action": "Keep" | "Close" | "Undo close", "note": "<text or empty>"}]}

Each row is applied with the existing Concern verbs, called on the store directly (the API's role checks are not on this path: the signed
route is trusted as a Closer). The actor is `sheet:<email>`. The result per row is kept in `concern_sheet_results` and shown as `last result`
in the next export until the next Submit for that Concern. The job then pushes a fresh export; a failed push is logged and never fails the job."""
from __future__ import annotations

import sys
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from t6.concern import ConcernStore
from t6.concern_export import concerns_export_csv
from t6.domain.events import JsonValue
from t6.preview import PreviewTokens
from t6.registry.sqlite import SqliteProjectRegistry
from t6.verbs import Done, Outcome, Preview, Refused
from t6.worker.client import WorkerJob
from t6.worker.runner import Refused as JobRefused

ACTOR_PREFIX = 'sheet:'
STALE = 'Changed since your last refresh'
CLOSED, UNDONE, NOTE_ADDED = 'Closing after PSR', 'Undone', 'Note added'
ACTIONS = ('Keep', 'Close', 'Undo close')


class Names(Protocol):
    """Notion's names for event and export content."""

    def project(self, project: str) -> str: ...
    def person(self, person: str) -> str: ...


class ConcernsSheetSubmit:
    """The `sheets.concerns_submit` handler. `push` is None when the export push is off; `names` None shows the Project and Person names blank."""

    def __init__(self, registry: SqliteProjectRegistry, tokens: PreviewTokens, push: Callable[[str], None] | None, names: Names | None) -> None:
        self._registry, self._store, self._push, self._names = registry, ConcernStore(registry, tokens), push, names

    def handlers(self) -> dict[str, Callable[[WorkerJob], JsonValue]]:
        return {'sheets.concerns_submit': self.handle}

    def handle(self, job: WorkerJob) -> JsonValue:
        email, rows = job.payload.get('email'), job.payload.get('rows')
        if not isinstance(email, str) or '@' not in email or not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise JobRefused('A Submit needs the clicker email and a list of rows')
        actor = ACTOR_PREFIX + email.strip().lower()
        results: list[JsonValue] = []
        for row in rows:
            assert isinstance(row, dict)
            concern = row.get('concern')
            if not isinstance(concern, str) or not concern:
                results.append({'concern': None, 'result': 'A Concern id is required'})
                continue
            result = self._row(job.job_id, actor, concern, row)
            self._keep(concern, result, job.job_id)
            results.append({'concern': concern, 'result': result})
        self._push_export()
        return {'results': results}

    def _row(self, job_id: int, actor: str, concern: str, row: Mapping[str, Any]) -> str:
        # The check and the writes below are separate transactions; safe enough: one main worker, and each write has an idempotency key.
        with self._registry._transaction() as db:
            current = db.execute('SELECT revision FROM concerns WHERE id = ?', (concern,)).fetchone()
            # A re-delivered job has written under its own keys already: its rows are not judged stale by their own writes.
            redelivered = db.execute('SELECT 1 FROM verb_requests WHERE idempotency_key IN (?, ?, ?)', tuple(self._key(job_id, concern, verb) for verb in ('note', 'resolve', 'reopen'))).fetchone() is not None
        if current is None:
            return 'Concern not found'
        action, note = row.get('action', 'Keep'), row.get('note')
        if action not in ACTIONS or (note is not None and not isinstance(note, str)):
            return 'The action must be Keep, Close or Undo close'
        try:
            seen = int(row['revision'])
        except (KeyError, TypeError, ValueError):
            return STALE
        if seen != current[0] and not redelivered:
            return STALE
        done: list[str] = []
        if note and note.strip():
            refusal = self._apply(job_id, actor, 'note', {'concern': concern, 'text': note})
            if refusal:
                return refusal
            done.append(NOTE_ADDED)
        if action != 'Keep':
            verb = 'resolve' if action == 'Close' else 'reopen'
            refusal = self._apply(job_id, actor, verb, {'concern': concern})
            if refusal:
                return '; '.join([*done, refusal])
            done.append(CLOSED if verb == 'resolve' else UNDONE)
        return '; '.join(done) or 'Nothing to do'

    @staticmethod
    def _key(job_id: int, concern: str, verb: str) -> str:
        return f'sheet:{job_id}:{concern}:{verb}'

    def _apply(self, job_id: int, actor: str, verb: str, values: dict[str, Any]) -> str | None:
        """Preview and confirm one verb under this job's key for the Concern; the refusal text, or None when it went through."""
        key = self._key(job_id, values['concern'], verb)
        outcome: Outcome = self._store.write(verb, values, actor=actor, idempotency_key=key)
        if isinstance(outcome, Preview):
            outcome = self._store.write(verb, values, actor=actor, idempotency_key=key, confirm=outcome.token)
        return outcome.reason if isinstance(outcome, Refused) else None

    def _keep(self, concern: str, result: str, job_id: int) -> None:
        with self._registry._transaction() as db:
            if db.execute('SELECT 1 FROM concerns WHERE id = ?', (concern,)).fetchone():
                db.execute('INSERT OR REPLACE INTO concern_sheet_results (concern_id, result, job_id, at) VALUES (?, ?, ?, ?)', (concern, result, job_id, self._registry._clock()))

    def _push_export(self) -> None:
        if self._push is None:
            return
        try:
            projects, people = self._lookups()
            self._push(concerns_export_csv(self._registry, projects, people))
        except Exception as error:
            print(f't6.worker: concerns export push failed: {type(error).__name__}', file=sys.stderr, flush=True)  # the type only: no text

    def _lookups(self) -> tuple[dict[str, tuple[str, str]], dict[str, str]]:
        """Code and name of each Project, and the name of each Person, that an open Concern mentions. A name that cannot be read shows blank."""
        with self._registry._transaction() as db:
            projects = [r[0] for r in db.execute("SELECT DISTINCT project_id FROM concerns WHERE state != 'closed'")]
            people = [r[0] for r in db.execute("SELECT owner_id FROM concerns WHERE state != 'closed' AND owner_id IS NOT NULL UNION SELECT p.actor FROM concern_pending_closes p "
                                                "JOIN concerns c ON c.id = p.concern_id WHERE c.state != 'closed' AND p.actor IS NOT NULL AND p.actor NOT LIKE 'sheet:%'")]
        return ({p: (self._code(p), self._name(self._names.project, p)) for p in projects}, {p: self._name(self._names.person, p) or p for p in people}) if self._names else ({p: (self._code(p), '') for p in projects}, {})

    def _code(self, project: str) -> str:
        found = self._registry.get(project)
        return (found.code or '') if found else ''

    @staticmethod
    def _name(lookup: Callable[[str], str], entity: str) -> str:
        try:
            return lookup(entity)
        except Exception as error:
            print(f't6.worker: concerns export name lookup failed: {type(error).__name__}', file=sys.stderr, flush=True)
            return ''
