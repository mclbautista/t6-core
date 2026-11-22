"""Transcript identity, progress and append-only history."""
from __future__ import annotations

import json
import math
import secrets
import sqlite3
from collections.abc import Callable
from contextlib import nullcontext
from typing import Any

from t6 import verbflow
from t6.ids import EntityKind, T6Id
from t6.preview import PreviewTokens
from t6.registry.sqlite import SqliteProjectRegistry
from t6.server.queue import Claim
from t6.verbs import Done, Outcome, Refused
from t6.worker.internal_queue import InternalJobQueue, JobRecord

QUEUE = 'transcribe'
LEASE_SECONDS = 300
_PREVIEW_KINDS = ('transcript.preview', 'transcript.speakers.preview', 'transcript.translate.preview')


class NotFound(LookupError):
    """The job does not exist on the transcribe queue."""


class Forbidden(PermissionError):
    """The caller may not act on this job."""


class Invalid(ValueError):
    """A worker report that does not match what the job may report; `error` is the text the API answers with, if any."""

    def __init__(self, error: str | None = None) -> None:
        super().__init__(error or 'invalid report')
        self.error = error


class ClaimLost(RuntimeError):
    """The caller's lease on the job is gone: expired, replaced, or never held."""


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
    """Transcripts, their append-only history, and the rules of the transcribe jobs that make them.

    The jobs live in `internal_jobs` and are reached only through `InternalJobQueue`. A verb (`request`) queues a
    preview job, then, on confirm, a run job; the transcribe worker claims jobs, reports back, and the reports
    (`report`, `fail`) move the Transcript and its history in the same transaction as the job.
    """

    def __init__(self, registry: SqliteProjectRegistry, tokens: PreviewTokens | None = None, *, rate: float = 0.4) -> None:
        self.registry = registry
        self.tokens = tokens
        self.rate = rate
        self.queue = InternalJobQueue(registry, clock=registry._clock)

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

    def _log(self, db: Any, identifier: str, action: str, actor: str, speaker_map: str | None = None) -> None:
        """Add a history entry (and its Activity) to an existing Transcript; ValueError when there is none."""
        row = db.execute('SELECT project_id FROM transcripts WHERE id=?', (identifier,)).fetchone()
        if row is None:
            raise ValueError('Transcript not found')
        self._entry(db, identifier, row[0], action, actor, speaker_map)

    def _mark_failed(self, db: Any, identifier: str, actor: str) -> None:
        self._log(db, identifier, 'failed', actor)
        db.execute("UPDATE transcripts SET state='failed' WHERE id=?", (identifier,))

    def fail(self, identifier: str, *, actor: str) -> None:
        with self.registry._transaction() as db:
            self._mark_failed(db, identifier, actor)

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

        Only a Transcript still `running` is touched, so a second sweep writes no second `failed` entry. A translate run
        that is dead gets one `failed` entry (the Transcript's state stays as it was); the dead job is settled so the
        entry is written once.
        """
        with (self.registry._transaction() if _db is None else nullcontext(_db)) as db:
            self.queue.bury(QUEUE, db=db)  # a job of this queue whose lease ran out for the last time is dead now, whether or not a worker has polled since
            for job in self.queue.find('transcript.run', states=['dead'], db=db):
                payload: Any = job.payload
                if self.queue.find('transcript.run', after=job.job_id, payload={'transcript_id': payload['transcript_id']}, db=db):
                    continue  # a later run of this Transcript was queued: the dead one is old news
                if db.execute("SELECT 1 FROM transcripts WHERE id=? AND state='running'", (payload['transcript_id'],)).fetchone():
                    self._mark_failed(db, payload['transcript_id'], payload['actor'])
            for job in self.queue.find('transcript.translate.run', states=['dead'], db=db):
                payload = job.payload
                if self.queue.find('transcript.translate.run', after=job.job_id, payload={'transcript': payload['transcript']}, db=db):
                    continue  # translated again since: the dead run is old news
                if self.queue.settle(job.job_id, db=db):  # once per dead job, however often it is looked for
                    self.fail_translation(db, payload['transcript'], actor=payload['actor'])

    def is_running(self, *, transcript: str | None = None, media: str | None = None, _db: Any = None) -> bool:
        """Whether a Transcript (by id, or by its Media location) has a run going; a run whose job is dead is failed first."""
        with (self.registry._transaction() if _db is None else nullcontext(_db)) as db:
            self.fail_dead_runs(_db=db)
            row = db.execute('SELECT state FROM transcripts WHERE ' + ('id=?' if transcript is not None else 'media_id=?'), (transcript if transcript is not None else media,)).fetchone()
            return bool(row and row[0] == 'running')

    # -- the verbs: create, speakers and translate, each a preview job and then a run job ------------------

    @staticmethod
    def _request(verb: str, actor: str, values: dict[str, Any], key: str) -> tuple[str, str]:
        phase = 'confirm' if values.get('confirm') else 'preview'
        return f'transcript {verb}:{phase}:{key}', json.dumps({'verb': verb, 'phase': phase, 'actor': actor, **values}, sort_keys=True, separators=(',', ':'))

    def _replay(self, db: Any, verb: str, actor: str, values: dict[str, Any], key: str | None) -> Outcome | None:
        """The first result of a request retried with the same key; Refused when the key was used for another request."""
        if key is None:
            return None
        if not key.strip():
            return Refused(rule='idempotency_key', reason=verbflow.KEY_REQUIRED)
        return verbflow.retry_of(db, *self._request(verb, actor, values, key))

    def _remember(self, db: Any, verb: str, actor: str, values: dict[str, Any], key: str | None, outcome: Outcome) -> None:
        """Keep a Done result under its key, so a retry returns it instead of acting again."""
        if key is None or not isinstance(outcome, Done):
            return
        verbflow.keep(db, *self._request(verb, actor, values, key), outcome.model_dump(mode='json')['result'], ignore_repeat=True)

    def request(self, verb: str, inputs: Any, actor: str, *, translate_enabled: bool = False) -> Outcome:
        """`transcript create`, `speakers` or `translate` for an authorised actor: a preview job, or on confirm the run job.

        The retry check, the job insert and the kept result share one transaction: a crash leaves neither a job without
        its key (a retry would queue a second preview, or find its token spent) nor a key without its job.
        """
        if self.tokens is None:
            raise RuntimeError('Preview tokens are required')
        if verb == 'translate' and not translate_enabled:
            return Refused(rule='translate_disabled', reason='Transcript translation is disabled')
        values = inputs.model_dump(exclude={'idempotency_key'})
        with self.registry._transaction() as db:
            outcome = self._replay(db, verb, actor, values, inputs.idempotency_key)
            if outcome is None:
                outcome = {'create': self._create, 'speakers': self._speakers, 'translate': self._translate}[verb](db, inputs, actor)
                self._remember(db, verb, actor, values, inputs.idempotency_key, outcome)
            return outcome

    def _submit(self, db: Any, kind: str, payload: dict[str, Any]) -> int:
        return self.queue.submit(QUEUE, kind, secrets.token_urlsafe(24), payload, db=db).job_id

    def _confirm(self, db: Any, nonce: str, *, current: dict[str, Any], busy: Callable[[], Refused | None], busy_first: bool,
                 missing: tuple[str, str], stale: tuple[str, str], build: Callable[[dict[str, Any]], dict[str, Any] | Refused]) -> dict[str, Any] | Refused:
        """The confirm steps all three verbs share; returns the run job's payload, or the refusal.

        The token must be unused; the saved preview must exist and match the `current` state of the inputs; nothing
        else may be running (`busy`, before or after the preview check); `build` makes the payload from the saved
        preview. Only then is the token spent.
        """
        if spent := verbflow.token_spent(db, nonce):
            return spent
        if busy_first and (refusal := busy()):
            return refusal
        saved = db.execute('SELECT snapshot FROM transcript_previews WHERE nonce=?', (nonce,)).fetchone()
        if not saved:
            return Refused(rule=missing[0], reason=missing[1])
        snapshot = json.loads(saved[0])
        if any(snapshot[name] != value for name, value in current.items()):
            return Refused(rule=stale[0], reason=stale[1])
        if not busy_first and (refusal := busy()):
            return refusal
        payload = build(snapshot)
        if isinstance(payload, Refused):
            return payload
        verbflow.spend(db, nonce)
        return payload

    def _open(self, db: Any, kind: str, transcript: str) -> bool:
        return bool(self.queue.find(kind, states=['queued', 'claimed'], payload={'transcript': transcript}, db=db))

    def _verified(self, inputs: Any, verb: str, actor: str, values: dict[str, Any]) -> str | Refused | None:
        assert self.tokens is not None
        return self.tokens.verify(inputs.confirm, 'transcript ' + verb, actor, values) if inputs.confirm else None

    def _translate(self, db: Any, inputs: Any, actor: str) -> Outcome:
        values = {'transcript': inputs.transcript}
        nonce = self._verified(inputs, 'translate', actor, values)
        if isinstance(nonce, Refused):
            return nonce
        row = db.execute("SELECT m.ref FROM transcripts t JOIN media_locations m ON m.id=t.media_id WHERE t.id=? AND m.active=1 AND m.kind='nas-path'", (inputs.transcript,)).fetchone()
        if not row:
            return Refused(rule='transcript', reason='Transcript not found')
        if self.is_running(transcript=inputs.transcript, _db=db):
            return Refused(rule='transcript_running', reason='Transcript is running')
        payload: dict[str, Any] = {**values, 'actor': actor, 'audio_folder': row[0], 'speaker_map': self.speaker_map(inputs.transcript, _db=db)}
        kind = 'transcript.translate.preview'
        if nonce:
            outcome = self._confirm(
                db, nonce, current={'audio_folder': row[0]}, busy_first=False, missing=('input_changed', 'Transcript inputs changed'), stale=('input_changed', 'Transcript inputs changed'),
                busy=lambda: Refused(rule='transcript_running', reason='Translation is already running') if self._open(db, 'transcript.translate.run', inputs.transcript) else None,
                build=lambda saved: {**payload, 'snapshot': saved['snapshot']})
            if isinstance(outcome, Refused):
                return outcome
            payload, kind = outcome, 'transcript.translate.run'
        return Done(result={'job_id': self._submit(db, kind, payload)})

    def _speakers(self, db: Any, inputs: Any, actor: str) -> Outcome:
        from t6.transcribe.speakers import parse_speaker_map
        try:
            parse_speaker_map(inputs.csv)
        except ValueError as error:
            return Refused(rule='speaker_map', reason=str(error))
        values = inputs.model_dump(exclude={'confirm', 'idempotency_key'})
        nonce = self._verified(inputs, 'speakers', actor, values)
        if isinstance(nonce, Refused):
            return nonce
        row = db.execute("SELECT m.ref, t.settings FROM transcripts t JOIN media_locations m ON m.id=t.media_id WHERE t.id=? AND m.active=1 AND m.kind='nas-path'", (inputs.transcript,)).fetchone()
        if not row:
            return Refused(rule='transcript', reason='Transcript not found')
        if self.is_running(transcript=inputs.transcript, _db=db):
            return Refused(rule='transcript_running', reason='Transcript is running')
        previous_map = self.speaker_map(inputs.transcript, _db=db)
        settings = json.loads(row[1])
        payload: dict[str, Any] = {**values, 'actor': actor, 'audio_folder': row[0], 'previous_map': previous_map, 'start_timecode': settings.get('start_timecode'), 'formats': settings.get('formats')}
        kind = 'transcript.speakers.preview'
        if nonce:
            outcome = self._confirm(
                db, nonce, current={'audio_folder': row[0], 'previous_map': previous_map}, busy_first=True, missing=('preview', 'Preview not found'), stale=('input_changed', 'Transcript inputs changed'),
                busy=lambda: Refused(rule='transcript_running', reason='Speaker job is already running') if self._open(db, 'transcript.speakers.run', inputs.transcript) else None,
                build=lambda saved: {**payload, 'snapshot': saved['snapshot']})
            if isinstance(outcome, Refused):
                return outcome
            payload, kind = outcome, 'transcript.speakers.run'
        return Done(result={'job_id': self._submit(db, kind, payload)})

    def _create(self, db: Any, inputs: Any, actor: str) -> Outcome:
        values = inputs.model_dump(exclude={'confirm', 'idempotency_key'})
        nonce = self._verified(inputs, 'create', actor, values)
        if isinstance(nonce, Refused):
            return nonce
        media = db.execute("SELECT ref, project_id FROM media_locations WHERE id=? AND kind='nas-path' AND active=1", (inputs.media,)).fetchone()
        if not media:
            return Refused(rule='media', reason='Active NAS Media location required')
        if inputs.deliverable and not db.execute('SELECT 1 FROM deliverables WHERE id=? AND project_id=?', (inputs.deliverable, media[1])).fetchone():
            return Refused(rule='deliverable', reason='Deliverable must belong to the Project')
        if not nonce:
            return Done(result={'job_id': self._submit(db, 'transcript.preview', {**values, 'actor': actor, 'audio_folder': media[0], 'rate': self.rate})})

        def busy() -> Refused | None:
            existing = db.execute('SELECT id FROM transcripts WHERE media_id=?', (inputs.media,)).fetchone()
            if existing and self._open(db, 'transcript.speakers.run', existing[0]):
                return Refused(rule='transcript_running', reason='Speaker job is already running')
            if self.is_running(media=inputs.media, _db=db):
                return Refused(rule='transcript_running', reason='A transcription is already running')
            return None

        def build(saved: dict[str, Any]) -> dict[str, Any] | Refused:
            try:
                transcript = self.start(inputs.media, inputs.deliverable, {**values, 'rate': saved['rate']}, actor=actor, _db=db)
            except ValueError as error:
                return Refused(rule='transcript', reason=str(error))
            return {**values, 'actor': actor, 'audio_folder': media[0], 'rate': saved['rate'], 'snapshot': saved['snapshot'], 'already_done': saved.get('already_done'),
                    'hours_pending': saved.get('hours_pending'), 'transcript_id': transcript, 'speaker_map': self.speaker_map(transcript, _db=db)}

        payload = self._confirm(db, nonce, current={'audio_folder': media[0]}, busy_first=False, missing=('preview', 'Preview not found'),
                                stale=('media_changed', 'Media location changed since preview'), busy=busy, build=build)
        if isinstance(payload, Refused):
            return payload
        return Done(result={'job_id': self._submit(db, 'transcript.run', payload), 'transcript_id': payload['transcript_id']})

    # -- the transcribe worker's side: claim, lease, report, fail ------------------------------------------

    def claim(self) -> Claim | None:
        """Lease the next transcribe job; Transcripts whose run job just went dead are failed after it."""
        claim = self.queue.claim(QUEUE, LEASE_SECONDS)
        self.fail_dead_runs()
        return claim

    def holds_translate_lease(self, job_id: object, token: object) -> bool:
        """Whether the caller holds the live lease of a translate run: the only reason to let it call the LLM."""
        if not isinstance(job_id, int) or not isinstance(token, str):
            return False
        job = self.queue.get(job_id, kind='transcript.translate.run')
        return job is not None and self._leased(job, token)

    def _leased(self, job: JobRecord, token: object) -> bool:
        return job.state == 'claimed' and job.lease_token == token and (job.lease_until or 0) > self.registry._clock()

    def result(self, job_id: int, actor: str) -> Any:
        """The stored outcome of a job the actor started, or None while it runs."""
        job = self.queue.get(job_id, queue=QUEUE)
        if job is None:
            raise NotFound(job_id)
        if actor != job.payload['actor']:
            raise Forbidden(actor)
        found = self.queue.result(job_id, QUEUE)
        return None if found is None else found.value

    def _worker_job(self, db: sqlite3.Connection | None, job_id: int, token: object, actor: str) -> JobRecord:
        job = self.queue.get(job_id, queue=QUEUE, db=db)
        if job is None:
            raise NotFound(job_id)
        if actor != 'transcribe':
            raise Forbidden(actor)
        if not self._leased(job, token):
            raise ClaimLost(job_id)
        return job

    def extend(self, job_id: int, token: object, actor: str) -> None:
        self._worker_job(None, job_id, token, actor)
        if not self.queue.extend(job_id, str(token), LEASE_SECONDS):
            raise ClaimLost(job_id)

    def fail_job(self, job_id: int, token: object, actor: str) -> None:
        """The worker gives up on a job: a run marks its Transcript failed, a translate run writes a `failed` entry."""
        with self.registry._transaction() as db:
            job = self._worker_job(db, job_id, token, actor)
            if job.kind == 'transcript.translate.run':
                self.fail_translation(db, str(job.payload['transcript']), actor=str(job.payload['actor']))
            if job.kind == 'transcript.run':
                self._mark_failed(db, str(job.payload['transcript_id']), str(job.payload['actor']))
            self.queue.finish(job_id, {'outcome': 'refused', 'rule': 'transcribe', 'reason': 'Transcription job failed'}, state='failed', db=db)

    def report(self, job_id: int, token: object, kind: object, data: object, actor: str) -> None:
        """The worker's report on a job it holds; Invalid (or KeyError, TypeError) when the report is not valid."""
        if self.tokens is None:
            raise RuntimeError('Preview tokens are required')
        with self.registry._transaction() as db:
            job = self._worker_job(db, job_id, token, actor)
            if not isinstance(data, dict):
                raise Invalid()
            payload: Any = job.payload

            def done(outcome: dict[str, Any]) -> None:
                self.queue.finish(job_id, outcome, db=db)
            if job.kind in _PREVIEW_KINDS and kind == 'failed':
                refusal = failure_refusal(job.kind, data)
                if refusal is None:
                    raise Invalid()
                done(refusal)
            elif job.kind == 'transcript.translate.preview' and kind == 'preview':
                self._translate_preview(db, job, data)
            elif job.kind == 'transcript.translate.run' and kind in ('complete', 'failed'):
                if data not in ({}, {'rule': 'input_changed'}, {'rule': 'translation'}, {'rule': 'outside_shares'}):
                    raise Invalid()
                if kind == 'complete':
                    self._log(db, payload['transcript'], 'translated', payload['actor'])
                    done({'outcome': 'done', 'result': {'job_id': job_id, 'transcript_id': payload['transcript']}})
                else:
                    self.fail_translation(db, payload['transcript'], actor=payload['actor'])
                    done({'outcome': 'refused', 'rule': data.get('rule', 'translation'), 'reason': 'Translation failed'})
            elif job.kind == 'transcript.speakers.preview' and kind == 'preview':
                self._speakers_preview(db, job, data)
            elif job.kind == 'transcript.speakers.run' and kind in ('complete', 'failed'):
                if (kind == 'complete' and data != {}) or (kind == 'failed' and data not in ({'rule': 'input_changed'}, {'rule': 'outside_shares'})):
                    raise Invalid()
                if kind == 'complete':
                    self._log(db, payload['transcript'], 'speakers changed', payload['actor'], payload['csv'])
                    done({'outcome': 'done', 'result': {'job_id': job_id, 'transcript_id': payload['transcript']}})
                else:
                    done({'outcome': 'refused', 'rule': data['rule'], 'reason': 'Transcript inputs changed' if data['rule'] == 'input_changed' else 'Folder is outside the worker shares'})
            elif kind == 'preview' and job.kind == 'transcript.preview':
                self._create_preview(db, job, data)
            elif job.kind == 'transcript.run' and kind in ('progress', 'complete', 'failed'):
                self._run_report(db, job, kind, data)
            else:
                raise Invalid('invalid report')

    def _issue_preview(self, db: Any, job: JobRecord, verb: str, values: dict[str, Any], plan: Any, snapshot: dict[str, Any]) -> None:
        """Sign the Preview a preview job found, keep what a confirm must match, and end the job with the Preview as its result."""
        assert self.tokens is not None
        actor = str(job.payload['actor'])
        preview = self.tokens.create(verb, actor, values, plan)
        nonce = self.tokens.verify(preview.token, verb, actor, values)
        if not isinstance(nonce, str):
            raise Invalid()
        db.execute('INSERT INTO transcript_previews VALUES (?, ?)', (nonce, json.dumps(snapshot)))
        self.queue.finish(job.job_id, preview.model_dump(mode='json'), db=db)

    def _translate_preview(self, db: Any, job: JobRecord, data: dict[str, Any]) -> None:
        if set(data) != {'clips', 'cue_count', 'estimated_tokens', 'snapshot'} or not isinstance(data['clips'], list) or not all(isinstance(x, str) for x in data['clips']) or any(type(data[k]) is not int or data[k] < 0 for k in ('cue_count', 'estimated_tokens')) or not isinstance(data['snapshot'], str) or len(data['snapshot']) != 64:
            raise Invalid()
        self._issue_preview(db, job, 'transcript translate', {'transcript': job.payload['transcript']}, {k: data[k] for k in ('clips', 'cue_count', 'estimated_tokens')},
                            {'snapshot': data['snapshot'], 'audio_folder': job.payload['audio_folder']})

    def _speakers_preview(self, db: Any, job: JobRecord, data: dict[str, Any]) -> None:
        changes, snapshot, missing = data.get('changes'), data.get('snapshot'), data.get('missing_audio')
        if set(data) != {'changes', 'missing_audio', 'snapshot'} or not isinstance(changes, list) or not isinstance(snapshot, str) or len(snapshot) != 64 or not isinstance(missing, list) or not all(isinstance(x, str) for x in missing):
            raise Invalid()
        if any(not isinstance(c, dict) or set(c) != {'source', 'speaker', 'from', 'to'} or not all(isinstance(v, str) for v in c.values()) for c in changes):
            raise Invalid()
        payload = job.payload
        self._issue_preview(db, job, 'transcript speakers', {'transcript': payload['transcript'], 'csv': payload['csv']}, {'changes': changes, 'missing_audio': missing},
                            {'snapshot': snapshot, 'audio_folder': payload['audio_folder'], 'previous_map': payload['previous_map']})

    def _create_preview(self, db: Any, job: JobRecord, data: dict[str, Any]) -> None:
        keys = ('files_found', 'already_done', 'hours_pending', 'estimated_cost', 'snapshot')
        payload = job.payload
        if set(data) != set(keys) or any(not _number(data[k]) for k in keys[:-1]) or not isinstance(data['snapshot'], str) or len(data['snapshot']) != 64 or any(c not in '0123456789abcdef' for c in data['snapshot']):
            raise Invalid('invalid preview')
        if data['already_done'] > data['files_found'] or not math.isclose(data['estimated_cost'], data['hours_pending'] * payload['rate']):
            raise Invalid('invalid estimate')
        if payload['max_hours'] is not None and data['hours_pending'] > payload['max_hours']:
            raise Invalid('hour limit exceeded')
        values = {k: v for k, v in payload.items() if k not in ('actor', 'audio_folder', 'rate')}
        self._issue_preview(db, job, 'transcript create', values, {k: data[k] for k in keys[:-1]},
                            {'snapshot': data['snapshot'], 'audio_folder': payload['audio_folder'], 'rate': payload['rate'], 'already_done': data['already_done'], 'hours_pending': data['hours_pending']})

    def _run_report(self, db: Any, job: JobRecord, kind: object, data: dict[str, Any]) -> None:
        identifier = str(job.payload['transcript_id'])
        if kind == 'progress':
            safe = {k: data[k] for k in ('state', 'completed', 'failed', 'total', 'current_clip', 'processed_seconds', 'total_seconds') if k in data}
            if set(safe) != set(data) or safe.get('state') not in ('completed', 'failed') or any(not _number(safe.get(k)) for k in ('completed', 'failed', 'total', 'processed_seconds', 'total_seconds')) or not isinstance(safe.get('current_clip'), str):
                raise Invalid('invalid progress')
            db.execute('UPDATE transcripts SET progress=? WHERE id=?', (json.dumps(safe), identifier))
            return
        safe = {k: data[k] for k in ('clips_done', 'failed', 'skipped', 'audio_hours', 'estimated_cost', 'rule') if k in data}
        if set(safe) != set(data) or (kind == 'complete' and 'rule' in safe) or ('rule' in safe and safe['rule'] not in ('input_changed', 'outside_shares')) or any(not _number(v) for k, v in safe.items() if k != 'rule'):
            raise Invalid('invalid summary')
        state = 'failed' if kind == 'failed' else 'done'
        db.execute('UPDATE transcripts SET state=?, summary=? WHERE id=?', (state, json.dumps(safe), identifier))
        if state == 'failed':
            self._log(db, identifier, 'failed', str(job.payload['actor']))
        self.queue.finish(job.job_id, {'outcome': 'refused', 'rule': safe.get('rule', 'transcribe'), 'reason': 'Transcription job failed'} if state == 'failed'
                          else {'outcome': 'done', 'result': {'transcript_id': identifier}}, db=db)
