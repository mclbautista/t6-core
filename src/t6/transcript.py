"""Transcript identity, progress and append-only history."""
from __future__ import annotations

import json
import math
from contextlib import nullcontext
from typing import Any

from t6.ids import EntityKind, T6Id
from t6.registry.sqlite import SqliteProjectRegistry
from t6.verbs import Done, Outcome, Refused


def _number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def failure_refusal(kind: str, data: dict[str, Any]) -> dict[str, Any] | None:
    """The Refused result for a worker's `failed` report on a preview job, or None when the report is not valid.

    A preview job ends in a refusal for a rule the worker found while measuring: the folder is outside its
    shares, the pending audio is over the person's hours cap, or a clip has no readable length.
    """
    if data == {'rule': 'outside_shares'}:
        return {'outcome': 'refused', 'rule': 'outside_shares', 'reason': 'Folder is outside the worker shares'}
    if kind != 'transcript.preview':
        return None
    if set(data) == {'rule', 'hours_found', 'estimated_cost', 'max_hours'} and data['rule'] == 'max_hours' and all(_number(data[k]) for k in ('hours_found', 'estimated_cost', 'max_hours')):
        reason = f"Pending audio is {data['hours_found']:.3f} h (estimated cost {data['estimated_cost']:.4f} USD), over the cap of {data['max_hours']:.3f} h"
        return {'outcome': 'refused', 'rule': 'max_hours', 'reason': reason}
    clips = data.get('clips')
    if set(data) == {'rule', 'clips'} and data['rule'] == 'unreadable_clip' and isinstance(clips, list) and clips and all(isinstance(c, str) for c in clips):
        return {'outcome': 'refused', 'rule': 'unreadable_clip', 'reason': 'Cannot read the length of: ' + ', '.join(clips)}
    return None


class TranscriptStore:
    def __init__(self, registry: SqliteProjectRegistry) -> None:
        self.registry = registry

    def get(self, identifier: str) -> dict[str, Any] | None:
        with self.registry._transaction() as db:
            row = db.execute('SELECT id, media_id, deliverable_id, project_id, settings, state, progress, summary FROM transcripts WHERE id=?', (identifier,)).fetchone()
            if row is None:
                return None
            result = dict(zip(('id', 'media_id', 'deliverable_id', 'project_id', 'settings', 'state', 'progress', 'summary'), row))
            for key in ('settings', 'progress', 'summary'):
                result[key] = json.loads(result[key])
            result['history'] = [dict(zip(('action', 'actor', 'at', 'map'), entry)) for entry in db.execute('SELECT h.action, h.actor, h.at, m.csv FROM transcript_history h LEFT JOIN transcript_speaker_maps m ON m.history_id=h.id WHERE h.transcript_id=? ORDER BY h.id', (identifier,))]
            return result

    def start(self, media: str, deliverable: str | None, settings: dict[str, Any], *, actor: str, _db: Any = None) -> str:
        with (self.registry._transaction() if _db is None else nullcontext(_db)) as db:
            row = db.execute('SELECT project_id FROM media_locations WHERE id=? AND active=1 AND kind=?', (media, 'nas-path')).fetchone()
            if not row:
                raise ValueError('Active NAS Media location required')
            project = row[0]
            if deliverable and not db.execute('SELECT 1 FROM deliverables WHERE id=? AND project_id=?', (deliverable, project)).fetchone():
                raise ValueError('Deliverable must belong to the Media location Project')
            existing = db.execute('SELECT id, deliverable_id FROM transcripts WHERE media_id=?', (media,)).fetchone()
            if existing and existing[1] != deliverable:
                raise ValueError('Transcript already has a different Deliverable')
            identifier = existing[0] if existing else str(T6Id.mint(EntityKind.TRANSCRIPT))
            if existing:
                db.execute("UPDATE transcripts SET settings=?, state='running', progress='{}', summary='{}' WHERE id=?", (json.dumps(settings), identifier))
            else:
                db.execute("INSERT INTO transcripts VALUES (?, ?, ?, ?, ?, 'running', '{}', '{}')", (identifier, media, deliverable, project, json.dumps(settings)))
            self._entry(db, identifier, project, 're-run' if existing else 'created', actor)
            return str(identifier)

    def _entry(self, db: Any, identifier: str, project: str, action: str, actor: str, speaker_map: str | None = None) -> None:
        now = self.registry._clock()
        history = db.execute('INSERT INTO transcript_history (transcript_id, action, actor, at) VALUES (?, ?, ?, ?)', (identifier, action, actor, now))
        if speaker_map is not None:
            db.execute('INSERT INTO transcript_speaker_maps VALUES (?, ?)', (history.lastrowid, speaker_map))
        audit = db.execute("INSERT INTO audit (project_id, action, actor, at, before, after) VALUES (?, ?, ?, ?, NULL, '{}')", (identifier, 'transcript_' + action, actor, now))
        db.execute('INSERT INTO activity (project_id, subject_id, at, actor, action, summary, audit_id) VALUES (?, ?, ?, ?, ?, ?, ?)', (project, identifier, now, actor, 'transcript_' + action, 'transcript ' + action, audit.lastrowid))

    def fail(self, identifier: str, *, actor: str) -> None:
        with self.registry._transaction() as db:
            row = db.execute('SELECT project_id FROM transcripts WHERE id=?', (identifier,)).fetchone()
            if row is None:
                raise ValueError('Transcript not found')
            db.execute("UPDATE transcripts SET state='failed' WHERE id=?", (identifier,))
            self._entry(db, identifier, row[0], 'failed', actor)

    def fail_translation(self, db: Any, identifier: str, *, actor: str) -> None:
        """Write a `failed` history entry for a translate run that failed; the Transcript's state stays as it was."""
        row = db.execute('SELECT project_id FROM transcripts WHERE id=?', (identifier,)).fetchone()
        if row is not None:
            self._entry(db, identifier, row[0], 'failed', actor)

    def speaker_map(self, identifier: str, *, _db: Any = None) -> str:
        """The latest speaker map applied to this Transcript, or an empty string."""
        with (self.registry._transaction() if _db is None else nullcontext(_db)) as db:
            row = db.execute('SELECT m.csv FROM transcript_speaker_maps m JOIN transcript_history h ON h.id=m.history_id WHERE h.transcript_id=? ORDER BY h.id DESC LIMIT 1', (identifier,)).fetchone()
            return str(row[0]) if row else ''

    def fail_dead_runs(self, *, _db: Any = None) -> None:
        """Mark failed every Transcript whose run job is dead (its lease expired too often), so later work is not blocked.

        Only a Transcript still `running` is touched, so a second sweep writes no second `failed` entry.
        """
        with (self.registry._transaction() if _db is None else nullcontext(_db)) as db:
            for job_id, payload in db.execute("SELECT id, payload FROM internal_jobs WHERE kind='transcript.run' AND state='dead'").fetchall():
                job = json.loads(payload)
                if db.execute("SELECT 1 FROM internal_jobs WHERE kind='transcript.run' AND id>? AND json_extract(payload, '$.transcript_id')=?", (job_id, job['transcript_id'])).fetchone():
                    continue  # a later run of this Transcript was queued: the dead one is old news
                row = db.execute("SELECT project_id FROM transcripts WHERE id=? AND state='running'", (job['transcript_id'],)).fetchone()
                if row:
                    db.execute("UPDATE transcripts SET state='failed' WHERE id=?", (job['transcript_id'],))
                    self._entry(db, job['transcript_id'], row[0], 'failed', job['actor'])

    def is_running(self, *, transcript: str | None = None, media: str | None = None, _db: Any = None) -> bool:
        """Whether a Transcript (by id, or by its Media location) has a run going; a run whose job is dead is failed first."""
        with (self.registry._transaction() if _db is None else nullcontext(_db)) as db:
            self.fail_dead_runs(_db=db)
            row = db.execute('SELECT state FROM transcripts WHERE ' + ('id=?' if transcript is not None else 'media_id=?'), (transcript if transcript is not None else media,)).fetchone()
            return bool(row and row[0] == 'running')

    @staticmethod
    def _request(verb: str, actor: str, values: dict[str, Any], key: str) -> tuple[str, str]:
        phase = 'confirm' if values.get('confirm') else 'preview'
        return f'transcript {verb}:{phase}:{key}', json.dumps({'verb': verb, 'phase': phase, 'actor': actor, **values}, sort_keys=True, separators=(',', ':'))

    def replay(self, verb: str, actor: str, values: dict[str, Any], key: str | None) -> Outcome | None:
        """The first result of a request retried with the same key; Refused when the key was used for another request."""
        if key is None:
            return None
        if not key.strip():
            return Refused(rule='idempotency_key', reason='A nonblank idempotency key is required')
        name, fingerprint = self._request(verb, actor, values, key)
        with self.registry._transaction() as db:
            row = db.execute('SELECT fingerprint, result FROM verb_requests WHERE idempotency_key=?', (name,)).fetchone()
        if row is None:
            return None
        if row[0] != fingerprint:
            return Refused(rule='idempotency_key', reason='Idempotency key was used for a different request')
        return Done(result=json.loads(row[1]))

    def remember(self, verb: str, actor: str, values: dict[str, Any], key: str | None, outcome: Outcome) -> None:
        """Keep a Done result under its key, so a retry returns it instead of acting again."""
        if key is None or not isinstance(outcome, Done):
            return
        name, fingerprint = self._request(verb, actor, values, key)
        with self.registry._transaction() as db:
            db.execute('INSERT OR IGNORE INTO verb_requests VALUES (?, ?, ?)', (name, fingerprint, json.dumps(outcome.model_dump(mode='json')['result'])))
