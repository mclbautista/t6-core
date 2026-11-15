"""Transcript identity, progress and append-only history."""
from __future__ import annotations

import json
from contextlib import nullcontext
from typing import Any

from t6.ids import EntityKind, T6Id
from t6.registry.sqlite import SqliteProjectRegistry


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
            result['history'] = [dict(zip(('action', 'actor', 'at'), entry)) for entry in db.execute('SELECT action, actor, at FROM transcript_history WHERE transcript_id=? ORDER BY id', (identifier,))]
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

    def _entry(self, db: Any, identifier: str, project: str, action: str, actor: str) -> None:
        now = self.registry._clock()
        db.execute('INSERT INTO transcript_history (transcript_id, action, actor, at) VALUES (?, ?, ?, ?)', (identifier, action, actor, now))
        audit = db.execute("INSERT INTO audit (project_id, action, actor, at, before, after) VALUES (?, ?, ?, ?, NULL, '{}')", (identifier, 'transcript_' + action, actor, now))
        db.execute('INSERT INTO activity (project_id, subject_id, at, actor, action, summary, audit_id) VALUES (?, ?, ?, ?, ?, ?, ?)', (project, identifier, now, actor, 'transcript_' + action, 'transcript ' + action, audit.lastrowid))

    def fail(self, identifier: str, *, actor: str) -> None:
        with self.registry._transaction() as db:
            row = db.execute('SELECT project_id FROM transcripts WHERE id=?', (identifier,)).fetchone()
            if row is None:
                raise ValueError('Transcript not found')
            db.execute("UPDATE transcripts SET state='failed' WHERE id=?", (identifier,))
            self._entry(db, identifier, row[0], 'failed', actor)
