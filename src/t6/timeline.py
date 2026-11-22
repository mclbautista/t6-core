"""Timelines: Resolve sequence timelines identified by a stamp the Resolve worker writes, never by name."""
from __future__ import annotations

import json
from typing import Any

from t6 import verbflow
from t6.ids import EntityKind, T6Id
from t6.preview import PreviewTokens
from t6.registry.sqlite import SqliteProjectRegistry
from t6.resolve.stamp_jobs import ResolveStampJobs
from t6.resolve.timelines import TimelineReader
from t6.verbs import Ambiguous, Outcome, Refused


class TimelineStore:
    """`timeline link`, `timeline restamp` and `timeline list`.

    The Timeline ID is the stamp: the `t6t_` value in a marker at the timeline's first frame. A confirm
    records the Timeline **pending** and queues one `resolve.stamp` job in the same transaction; the Resolve
    worker writes the marker and reads it back, and the job's result marks it stamped or failed with the
    reason (`t6.resolve.stamp_jobs._settle`). The Resolve project is the Project's active Media location of
    kind resolve-project (on the project server), so the copy recorded here is the canonical copy. The name
    is only a label. The reader is optional: without one the stamp is recorded as given and the preview says
    it is unchecked.
    """

    def __init__(self, registry: SqliteProjectRegistry, tokens: PreviewTokens, reader: TimelineReader | None = None) -> None:
        self.registry, self.tokens, self.reader = registry, tokens, reader

    def list(self, project: str) -> list[dict[str, Any]]:
        """The Project's Timelines; LookupError for a Project that is not registered."""
        with self.registry._transaction() as db:
            if not db.execute('SELECT 1 FROM projects WHERE id=?', (project,)).fetchone():
                raise LookupError('Project not found')
            rows = db.execute('SELECT t.id, t.project_id, t.label, m.id, m.ref, m.active, t.stamp_state, t.stamp_reason FROM timelines t JOIN media_locations m ON m.id=t.resolve_media_id WHERE t.project_id=? ORDER BY t.id', (project,)).fetchall()
        return [{'id': r[0], 'stamp': r[0], 'project_id': r[1], 'stamp_state': r[6], 'stamp_reason': r[7],
                 'canonical': {'resolve_project': r[4], 'resolve_project_media': r[3], 'name': r[2], 'active': bool(r[5])}} for r in rows]

    def write(self, values: dict[str, Any], *, actor: str, idempotency_key: str | None = None, confirm: str | None = None) -> Outcome:
        project, name, stamp = values.get('project'), values.get('timeline'), values.get('stamp')
        generated = stamp is None
        if not actor.strip() or any(not isinstance(v, str) or not v.strip() for v in (project, name) + (() if generated else (stamp,))):
            return Refused(rule='input', reason='A valid actor, Project and timeline are required, and a stamp when given')
        assert isinstance(project, str) and isinstance(name, str)
        if not generated:
            assert isinstance(stamp, str)
            try:
                T6Id.parse(stamp, EntityKind.TIMELINE)
            except ValueError:
                return Refused(rule='stamp', reason='The stamp must be a t6t_ Timeline ID')
        try:
            T6Id.parse(project, EntityKind.PROJECT)
        except ValueError:
            return Refused(rule='project', reason='Project must be a T6 Project ID')
        values = {'project': project, 'timeline': name, **({} if generated else {'stamp': stamp})}
        request = verbflow.begin(self.tokens, 'timeline link', actor, values, idempotency_key, confirm)
        if isinstance(request, Refused):
            return request
        nonce, key = request.nonce, request.key
        with self.registry._transaction() as db:
            settled = verbflow.settle(db, self.tokens, request, replay_spent=True)  # a retried confirm gets the first result, its token spent or not
            if settled:
                return settled
            if not db.execute('SELECT 1 FROM projects WHERE id=?', (project,)).fetchone():
                return Refused(rule='project', reason='Project not found')
            media = db.execute("SELECT id, ref FROM media_locations WHERE owner_id=? AND kind='resolve-project' AND active=1 ORDER BY id", (project,)).fetchall()
            if not media:
                return Refused(rule='resolve_project', reason='Link a Resolve project (Media location of kind resolve-project) to the Project first')
            if len(media) > 1:
                return Ambiguous(candidates=[{'media': m[0], 'ref': m[1]} for m in media])
            saved = db.execute('SELECT snapshot FROM timeline_previews WHERE nonce=?', (nonce,)).fetchone() if nonce else None
            if nonce and not saved:
                return Refused(rule='preview', reason='Preview not found')
            if generated:
                stamp = self._generated_stamp(db, project, key, json.loads(saved[0])['stamp'] if saved else None)
            assert isinstance(stamp, str)
            plan_or_refusal = self._plan(db, project, name, stamp, media[0][0], media[0][1], key, generated)
            if not isinstance(plan_or_refusal, dict):
                return plan_or_refusal
            plan = plan_or_refusal
            if nonce is None:
                preview, preview_nonce = verbflow.issue(self.tokens, request, plan)
                db.execute('INSERT INTO timeline_previews VALUES (?, ?)', (preview_nonce, json.dumps(plan)))
                return preview
            assert saved
            plan = json.loads(saved[0])
            existing = db.execute('SELECT project_id, resolve_media_id, stamp_state FROM timelines WHERE id=?', (stamp,)).fetchone()
            if existing and tuple(existing[:2]) != (plan['project_id'], plan['resolve_media_id']):
                return Refused(rule='timeline_stamp', reason='The stamp belongs to another Timeline since the preview')
            created = existing is None
            job = None
            if created or existing[2] == 'failed':  # a failed stamp is queued again by linking it again
                job = ResolveStampJobs(self.registry).enqueue(db, project=plan['project_id'], resolve_project=plan['resolve_project'], timeline=plan['label'], stamp=stamp)
            result = {**plan, 'created': created, 'stamp_state': 'pending' if job else existing[2], 'stamp_job': job}
            if created:
                self._record(db, plan, actor, 'timeline_link', result)
            elif job:
                db.execute("UPDATE timelines SET stamp_state='pending', stamp_reason=NULL WHERE id=?", (stamp,))
            return verbflow.finish(db, request, result)

    def _record(self, db: Any, plan: dict[str, Any], actor: str, action: str, result: dict[str, Any]) -> None:
        """Record the planned Timeline as pending, with its Audit and Activity."""
        now, stamp = self.registry._clock(), plan['stamp']
        db.execute("INSERT INTO timelines (id, project_id, resolve_media_id, label, stamp_state) VALUES (?, ?, ?, ?, 'pending')", (stamp, plan['project_id'], plan['resolve_media_id'], plan['label']))
        audit = db.execute('INSERT INTO audit (project_id, action, actor, at, before, after) VALUES (?, ?, ?, ?, NULL, ?)', (stamp, action, actor, now, json.dumps(result)))
        db.execute('INSERT INTO activity (project_id, subject_id, at, actor, action, summary, audit_id) VALUES (?, ?, ?, ?, ?, ?, ?)', (plan['project_id'], stamp, now, actor, action, action.replace('_', ' '), audit.lastrowid))

    def restamp(self, values: dict[str, Any], *, actor: str, idempotency_key: str | None = None, confirm: str | None = None) -> Outcome:
        """`timeline restamp`: give the duplicate `timeline` (name or Resolve ID) that carries the Project's Timeline
        `stamp` a new `t6t_` ID. The confirm records the copy as a new pending Timeline and queues a stamp job that
        replaces `stamp` on it; the canonical Timeline keeps `stamp`."""
        project, name, stamp = values.get('project'), values.get('timeline'), values.get('stamp')
        if not actor.strip() or any(not isinstance(v, str) or not v.strip() for v in (project, name, stamp)):
            return Refused(rule='input', reason='A valid actor, Project, timeline and stamp are required')
        assert isinstance(project, str) and isinstance(name, str) and isinstance(stamp, str)
        try:
            T6Id.parse(stamp, EntityKind.TIMELINE)
        except ValueError:
            return Refused(rule='stamp', reason='The stamp must be a t6t_ Timeline ID')
        request = verbflow.begin(self.tokens, 'timeline restamp', actor, {'project': project, 'timeline': name, 'stamp': stamp}, idempotency_key, confirm)
        if isinstance(request, Refused):
            return request
        with self.registry._transaction() as db:
            settled = verbflow.settle(db, self.tokens, request, replay_spent=True)
            if settled:
                return settled
            canonical = db.execute('SELECT t.label, t.resolve_media_id, m.ref FROM timelines t JOIN media_locations m ON m.id=t.resolve_media_id WHERE t.id=? AND t.project_id=?', (stamp, project)).fetchone()
            if not canonical:
                return Refused(rule='timeline_stamp', reason='No Timeline of this Project has that stamp')
            if name == canonical[0]:
                return Refused(rule='timeline', reason='That is the canonical Timeline; name the copy (or its Resolve ID)')
            saved = db.execute('SELECT snapshot FROM timeline_previews WHERE nonce=?', (request.nonce,)).fetchone() if request.nonce else None
            if request.nonce and not saved:
                return Refused(rule='preview', reason='Preview not found')
            new = self._generated_stamp(db, project, request.key, json.loads(saved[0])['stamp'] if saved else None)
            plan = {'project_id': project, 'timeline_id': new, 'stamp': new, 'replaces': stamp, 'resolve_project': canonical[2], 'resolve_media_id': canonical[1],
                    'label': name, 'stamp_generated': True, 'idempotency_key': request.key}
            if request.nonce is None:
                preview, preview_nonce = verbflow.issue(self.tokens, request, plan)
                db.execute('INSERT INTO timeline_previews VALUES (?, ?)', (preview_nonce, json.dumps(plan)))
                return preview
            job = ResolveStampJobs(self.registry).enqueue(db, project=project, resolve_project=plan['resolve_project'], timeline=name, stamp=new, replaces=stamp)
            result = {**plan, 'created': True, 'stamp_state': 'pending', 'stamp_job': job}
            self._record(db, plan, actor, 'timeline_restamp', result)
            return verbflow.finish(db, request, result)

    @staticmethod
    def _generated_stamp(db: Any, project: str, key: str, previewed: str | None) -> str:
        """The stamp this key shows: the one its confirmed preview carries, else the one an earlier preview of
        the same key showed (so a retried preview shows the same stamp), else a new `t6t_` ID."""
        if previewed:
            return previewed
        row = db.execute("SELECT json_extract(snapshot, '$.stamp') FROM timeline_previews WHERE json_extract(snapshot, '$.idempotency_key')=? AND json_extract(snapshot, '$.project_id')=? AND json_extract(snapshot, '$.stamp_generated')=1 ORDER BY rowid LIMIT 1", (key, project)).fetchone()
        return str(row[0]) if row else str(T6Id.mint(EntityKind.TIMELINE))

    def _plan(self, db: Any, project: str, name: str, stamp: str, media_id: str, resolve_ref: str, key: str, generated: bool = False) -> dict[str, Any] | Outcome:
        """The link to record, or the refusal. One Timeline per stamp: a stamp already recorded for another
        Project or Resolve project is refused, and so is a stamp on two timelines of the Resolve project."""
        existing = db.execute('SELECT project_id, resolve_media_id FROM timelines WHERE id=?', (stamp,)).fetchone()
        if existing and tuple(existing) != (project, media_id):
            return Refused(rule='timeline_stamp', reason='The stamp is already linked to another Timeline; one Timeline per stamp')
        label, verified = name, False
        if self.reader is not None:
            found = self.reader.timelines(resolve_ref)
            if found is None:
                return Refused(rule='resolve', reason='The Resolve project cannot be read')
            matches = [t for t in found if name in (t.name, t.uuid)]
            if not matches:
                return Refused(rule='timeline', reason='No timeline of that name or Resolve ID in the Resolve project')
            if len(matches) > 1:
                return Ambiguous(candidates=[{'uuid': t.uuid, 'name': t.name, 'stamp': t.stamp} for t in matches])
            chosen = matches[0]
            if generated:
                if chosen.stamp not in (None, stamp):  # the stamp the preview showed, written since, is the one expected
                    return Refused(rule='stamp', reason='The timeline already carries a stamp; pass it with --stamp')
            elif chosen.stamp != stamp and not (chosen.stamp is None and existing):  # a linked stamp not yet written (failed) is queued again
                return Refused(rule='stamp', reason='The timeline has no first-frame marker with this stamp; link it without --stamp and T6 writes one' if chosen.stamp is None else 'The timeline carries a different stamp')
            if not generated and sum(1 for t in found if t.stamp == stamp) > 1:
                return Refused(rule='timeline_stamp', reason='The stamp is on more than one timeline (a duplicate); re-stamp the copy with `timeline restamp`')
            label, verified = chosen.name, True
        return {'project_id': project, 'timeline_id': stamp, 'stamp': stamp, 'resolve_project': resolve_ref, 'resolve_media_id': media_id, 'label': label, 'verified': verified, 'stamp_generated': generated, 'idempotency_key': key}
