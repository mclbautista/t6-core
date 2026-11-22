"""`project start`: a previewed plan built by a job, and the steps that wait for its Approval (#267, #382).

The verb returns a job id. The main worker runs `run_preview`, whose result is the Preview with its signed
token. Confirm checks the plan again, requests the Approval (gate `project start`) and stores the Start as
waiting; it applies nothing else and does not ask Frame.io. When the Approval is granted (`approval grant`, which
the operator card also uses), `decided` queues a `project_start.apply` job on the main queue and the main worker
runs `run_apply`: the plan is built again, then the Frame.io step, then the t6 steps (the Deliverables with the
milestone template of the Project Type, the Media locations and the initial Bookings) through the stores that own
them, each under a key derived from the Start's own key, so a retry creates nothing twice. A denied Approval ends
the Start and applies nothing. The steps that are done by hand (NAS folders, the Resolve project, the Resolve
timelines with their stamps) are listed in the plan and recorded in Activity when the Start is applied.

The apply runs as the Person who confirmed the Start. If the plan is no longer the one confirmed, a step is
refused or Frame.io fails, nothing more is applied and the Start is `failed` with the reason (Audit and Activity);
confirming again with the same key and token applies it again, with no second Approval. The same key (the job
result read, or a retried confirm) returns the state: `waiting_for_approval`, `applying`, `applied`, `denied` or
`failed`. One Start of a Project at a time: another one is refused while a Start waits; a denied Start does not
block, and neither does a failed Start that applied no step.

The Frame.io step (#275) asks the Frame.io worker, the only holder of the Frame.io token, for the Frame.io
project and one folder per Deliverable code. The apply asks first, before any t6 step, so a Frame.io failure
leaves t6 unchanged. The answer is recorded as Media locations: the project through `frameio project link`
(the one-Project holder rule) and one `frameio-folder` with role review per Deliverable. The request creates
nothing that exists (see `CreateProjectFolders`), so a retry after a crash between the Frame.io call and the
t6 record adopts what the first try made. No share is made; uploading is the editor's job.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
from collections.abc import Callable, Mapping
from typing import Any

from t6.approval import Approvals
from t6.bay import BookingStore
from t6.deliverable import DeliverableStore
from t6.domain.events import JobKind
from t6.deliverable_templates import TEMPLATES
from t6.ids import EntityKind, T6Id
from t6.media import MediaStore
from t6.frameio.project_folders import FrameioProjectWriter
from t6.frameio.requests import Failed
from t6.frameio_hold import HeldActivity
from t6.person import People
from t6.preview import PreviewTokens
from t6.registry.sqlite import SqliteProjectRegistry
from t6.verbs import Done, Outcome, Preview, Refused
from t6.worker.internal_queue import InternalJobQueue

QUEUE = 'main'
JOB_KIND = 'project_start.preview'
APPLY_KIND = 'project_start.apply'
VERB = 'project start'
GATE = 'project start'
# What a person does by hand after a Project Start, in the plan's order. The Frame.io project and the
# per-Deliverable folders (#275) are made by the Frame.io worker, not by hand.
MANUAL_STEPS = (
    ('nas folders', 'NAS folders', 'Create the NAS folders by hand: no worker has NAS write yet.'),
    ('resolve project', 'Resolve project', 'Create the Resolve project by hand, then link it with `media link`.'),
    ('resolve timelines', 'Resolve timelines with their stamps',
     'Create the Resolve timelines and write their stamps by hand (manual stamping), then record each with `timeline link`.'),
)
# User-supplied `media` may not name these: the Frame.io Media locations come from the Frame.io step only.
FRAMEIO_KINDS = ('frameio-project', 'frameio-folder')
FRAMEIO_LABEL = 'Frame.io project and per-Deliverable folder'


def _fingerprint(actor: str, inputs: Mapping[str, Any]) -> str:
    return json.dumps({'verb': VERB, 'actor': actor, **inputs}, sort_keys=True, separators=(',', ':'))


def _step(outcome: Outcome, label: str) -> Refused | None:
    """A refusal from a step's own store, named by step; None when the store accepted the step."""
    return Refused(rule=outcome.rule, reason=f'{label}: {outcome.reason}') if isinstance(outcome, Refused) else None


def _activity(db: sqlite3.Connection, project: str, now: float, actor: str, action: str, summary: str, audit_id: int | None) -> None:
    db.execute('INSERT INTO activity (project_id, subject_id, at, actor, action, summary, audit_id) VALUES (?, ?, ?, ?, ?, ?, ?)',
               (project, project, now, actor, action, summary, audit_id))


def decided(registry: SqliteProjectRegistry, db: sqlite3.Connection, approval_id: str, verb: str, actor: str, reason: str | None, now: float,
            audit_id: int | None) -> None:
    """Called by `Approvals.write` in its transaction when an Approval is granted or denied.

    A waiting Start of that Approval gets its apply job on the main queue (one job per Start, so a repeat adds none),
    or ends as denied with the reason in Activity. Any other Approval is left alone.
    """
    row = db.execute("SELECT idempotency_key, project_id FROM project_starts WHERE approval_id=? AND state='waiting'", (approval_id,)).fetchone()
    if row is None:
        return
    if verb == 'grant':
        InternalJobQueue(registry, clock=registry._clock).submit(QUEUE, APPLY_KIND, row[0], {'key': row[0]}, db=db)
        return
    db.execute("UPDATE project_starts SET state='denied', reason=? WHERE idempotency_key=?", (reason, row[0]))
    _activity(db, row[1], now, actor, 'project_start_denied', f'project start denied: {reason}', audit_id)


class ProjectStartStore:
    def __init__(self, registry: SqliteProjectRegistry, tokens: PreviewTokens,
                 project_type: Callable[[str], str | None] | None = None,
                 project_name: Callable[[str], str | None] | None = None) -> None:
        self.registry, self.tokens = registry, tokens
        self.project_type, self.project_name = project_type, project_name
        self.queue = InternalJobQueue(registry, clock=registry._clock)

    # The verb, caller side

    def start(self, values: dict[str, Any], *, actor: str, idempotency_key: str | None) -> Outcome:
        """Queue the plan job. The same key and inputs give the same job; the key with other inputs is refused."""
        if idempotency_key is not None and not idempotency_key.strip():
            return Refused(rule='idempotency_key', reason='A nonblank idempotency key is required')
        if values['bookings']:
            refusal = People(self.registry, self.tokens).authorize(actor, 'bookings')
            if refusal:
                return refusal
        key = idempotency_key or secrets.token_hex(16)
        inputs = {**values, 'idempotency_key': key}
        fingerprint = _fingerprint(actor, inputs)
        submitted = self.queue.submit(QUEUE, JOB_KIND, f'{actor}:{key}',
                                      {'verb': VERB, 'actor': actor, 'inputs': inputs, 'fingerprint': fingerprint})
        if not submitted.created and self._job(submitted.job_id, 'fingerprint') != fingerprint:
            return Refused(rule='idempotency_key', reason='Idempotency key was used for a different request')
        return Done(result={'job_id': submitted.job_id, 'idempotency_key': key})

    def job_result(self, job_id: int, *, actor: str) -> Outcome:
        """The Preview a plan job produced, `pending` while it runs, or why it ended without one."""
        if self._job(job_id, 'actor') != actor:
            return Refused(rule='job', reason='Job not found')
        start = self._start(self._job(job_id, 'inputs')['idempotency_key'])
        if start and start['actor'] == actor:  # a confirmed Start: the job result read is its state
            return Done(result=self._state(start))
        result = self.queue.result(job_id, QUEUE)
        if result is None:
            return Done(result={'job_id': job_id, 'state': 'pending'})
        if result.status != 'ok' or not isinstance(result.value, dict):
            return Refused(rule='job_failed', reason=f'The plan job failed: {result.value}')
        return Preview.model_validate(result.value) if result.value.get('outcome') == 'preview' else Refused.model_validate(result.value)

    def _job(self, job_id: int, field: str) -> Any:
        """One field of a Project Start job's payload; None for any other job."""
        job = self.queue.get(job_id, kind=JOB_KIND)
        return job.payload.get(field) if job else None

    # The job, worker side

    def run_preview(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Build the plan for a queued job and return its result: the Preview with its token, or the refusal."""
        actor, inputs = payload['actor'], dict(payload['inputs'])
        key = inputs['idempotency_key']
        values = {name: value for name, value in inputs.items() if name != 'idempotency_key'}
        plan = self._plan(values, actor, key, None, None)
        if isinstance(plan, Refused):
            return plan.model_dump(mode='json')
        preview = self.tokens.create(VERB, actor, inputs, plan)
        nonce = self.tokens.verify(preview.token, VERB, actor, inputs)
        assert isinstance(nonce, str)
        with self.registry._transaction() as db:
            db.execute('INSERT INTO jo_state_previews VALUES (?, ?)', (nonce, json.dumps(plan)))
        return preview.model_dump(mode='json')

    def _name(self, project: str) -> str | None:
        """The Project's name from Notion, trimmed; None when it has none or there is no way to read it."""
        with self.registry._transaction() as db:
            binding = db.execute("SELECT page_id FROM bindings WHERE system='notion' AND project_id=? AND active=1", (project,)).fetchone()
        name = self.project_name(binding[0]) if binding and self.project_name else None
        return name.strip() if name and name.strip() else None

    def _plan(self, values: dict[str, Any], actor: str, key: str, template: str | None, name: str | None) -> dict[str, Any] | Refused:
        """The plan for these inputs, asked of the stores that apply each step; a refusal names the rule that stopped it.

        `template` and `name` are read once, by the preview; a confirm passes the ones the preview showed.
        """
        project = values['project']
        try:
            T6Id.parse(project, EntityKind.PROJECT)
        except ValueError:
            return Refused(rule='project', reason='Project must be a T6 Project ID')
        with self.registry._transaction() as db:
            found = db.execute('SELECT 1 FROM projects WHERE id=?', (project,)).fetchone()
            archived = db.execute('SELECT 1 FROM project_archives WHERE project_id=?', (project,)).fetchone()
            started = db.execute("SELECT 1 FROM activity WHERE project_id=? AND action='project_start'", (project,)).fetchone()
            waiting = db.execute("SELECT 1 FROM project_starts s JOIN approvals a ON a.id = s.approval_id "
                                 "WHERE s.project_id=? AND s.state='waiting' AND a.state='requested' AND s.idempotency_key != ?", (project, key)).fetchone()
            # A failed Start that applied no step holds nothing: its granted Approval cannot be denied, so it must not block another Start.
            held = db.execute("SELECT 1 FROM approvals a WHERE a.gate=? AND a.target=? AND a.state != 'denied' AND a.id NOT IN ("
                              "SELECT s.approval_id FROM project_starts s WHERE s.state='failed' AND NOT EXISTS (SELECT 1 FROM verb_requests v "
                              "WHERE instr(v.idempotency_key, s.idempotency_key || ':') = 1 AND v.idempotency_key != s.idempotency_key || ':approval'))",
                              (GATE, project)).fetchone()
        begun_by_other = held and not Approvals(self.registry, self.tokens).requested_under(f'{key}:approval', GATE, project)
        if not found:
            return Refused(rule='project', reason='Project not found')
        if archived:
            return Refused(rule='project_archived', reason='Project is archived')
        if waiting:
            return Refused(rule='project_started', reason='A Start of this Project waits for its Approval')
        if started or begun_by_other:  # a Start under another key has finished, or has its Approval (not denied) and waits, is being applied or stopped halfway
            return Refused(rule='project_started', reason='Project has already been started')
        if any(item['kind'] in FRAMEIO_KINDS for item in values['media']):
            return Refused(rule='frameio', reason='Frame.io Media locations come from the Frame.io step of the Start, not from `media`')
        codes = [item['code'] for item in values['deliverables']]
        if len(set(codes)) != len(codes):
            return Refused(rule='deliverable_code', reason='A Deliverable code appears twice')
        with self.registry._transaction() as db:
            held = sorted(code for (code,) in db.execute(
                'SELECT code FROM deliverables d WHERE project_id=? AND NOT EXISTS (SELECT 1 FROM verb_requests WHERE idempotency_key = ? || d.code)',
                (project, f'{key}:deliverable:')) if code in codes)  # a Deliverable this Start's own key made is not another owner
        if held:  # one Frame.io folder per code: a Start never gives a code a second owner (the Deliverable store itself does not forbid it)
            return Refused(rule='deliverable_code', reason='The Project already has a Deliverable with the code ' + ', '.join(held))
        name = name or self._name(project)
        if name is None:
            return Refused(rule='project_name', reason='The Project has no name to give the Frame.io project')
        approval = self._call('approval', {'gate': GATE, 'target': project}, actor, f'{key}:approval', None)
        if refusal := _step(approval, 'Approval'):
            return refusal
        assert isinstance(approval, Preview)
        deliverables: list[dict[str, Any]] = []
        try:
            for item in values['deliverables']:
                outcome = self._call('deliverable', {'project': project, **item, 'template': template}, actor, f'{key}:deliverable:{item["code"]}', None)
                if refusal := _step(outcome, f'Deliverable {item["code"]}'):
                    return refusal
                assert isinstance(outcome, Preview)
                template = outcome.plan['template']  # the Project Type is read once; the rest share its template
                deliverables.append({name: item.get(name) for name in ('code', 'episode', 'due', 'air')})
        except (OSError, ValueError) as error:
            return Refused(rule='project_type', reason=str(error))
        media = []
        for index, item in enumerate(values['media']):
            outcome = self._call('media', {'owner': project, **item}, actor, f'{key}:media:{index}', None)
            if refusal := _step(outcome, f'Media location {index + 1}'):
                return refusal
            assert isinstance(outcome, Preview)
            media.append({name: outcome.plan[name] for name in ('kind', 'ref', 'role')})
        bookings: list[dict[str, Any]] = []
        for index, item in enumerate(values['bookings']):
            outcome = self._call('booking', {**item, 'project': project}, actor, f'{key}:booking:{index}', None)
            if refusal := _step(outcome, f'Booking {index + 1}'):
                return refusal
            assert isinstance(outcome, Preview)
            booking = {name: outcome.plan[name] for name in ('bay_id', 'person_id', 'start', 'end', 'forced')}
            if not item.get('force') and any(
                    other['bay_id'] == booking['bay_id'] and other['start'] < booking['end'] and other['end'] > booking['start'] for other in bookings):
                return Refused(rule='overlap', reason=f'Booking {index + 1}: the Start asks for two overlapping Bookings in one Bay')
            bookings.append(booking)
        steps: list[dict[str, Any]] = [{'step': 'approval', 'kind': 't6', 'gate': GATE, 'target': project, 'tapper_rule': approval.plan['tapper_rule']}]
        steps += [{'step': 'nas folders', 'kind': 'manual', 'detail': MANUAL_STEPS[0][2]},
                  {'step': 'frameio', 'kind': 'frameio', 'label': FRAMEIO_LABEL, 'project_name': name, 'folders': codes,
                   'detail': 'The Frame.io worker makes the project and one folder per Deliverable code if they are missing, and uses the Frame.io '
                             'project already linked to this Project when there is one. No share is made.'},
                  {'step': 'resolve project', 'kind': 'manual', 'detail': MANUAL_STEPS[1][2]},
                  {'step': 'resolve timelines', 'kind': 'manual', 'detail': MANUAL_STEPS[2][2]},
                  {'step': 'media locations', 'kind': 't6', 'locations': media},
                  {'step': 'milestone template', 'kind': 't6', 'template': template,
                   'milestones': list(TEMPLATES[template]) if template else [], 'deliverables': deliverables},
                  {'step': 'bookings', 'kind': 't6', 'bookings': bookings}]
        return {'project_id': project, 'idempotency_key': key, 'steps': steps, 'manual_steps': [name for _, name, _ in MANUAL_STEPS]}

    # The verb, confirm side

    def confirm(self, values: dict[str, Any], *, actor: str, idempotency_key: str | None, token: str,
                frameio: FrameioProjectWriter | None = None) -> Outcome:
        """Request the Approval and store the Start as waiting; the same key again returns the Start's state.

        Nothing else is applied here (see `run_apply`). A failed Start is applied again by confirming it again, which
        asks Frame.io through `frameio` and does not ask for a second Approval.
        """
        if not idempotency_key or not idempotency_key.strip():
            return Refused(rule='idempotency_key', reason='A nonblank idempotency key is required')
        key = idempotency_key
        inputs = {**values, 'idempotency_key': key}
        fingerprint = _fingerprint(actor, inputs)
        with self.registry._transaction() as db:
            previous = db.execute('SELECT fingerprint FROM verb_requests WHERE idempotency_key=?', (key,)).fetchone()
            archived = db.execute('SELECT 1 FROM project_archives WHERE project_id=?', (values['project'],)).fetchone() is not None
        start = self._start(key)
        if previous and previous[0] != fingerprint or start and start['fingerprint'] != fingerprint:
            return Refused(rule='idempotency_key', reason='Idempotency key was used for a different request')
        if previous or start and start['state'] != 'failed':  # a retried confirm gets the state, even after its token expired: never a second Start (ADR 0005)
            return Done(result=self._state(start or {'idempotency_key': key, 'state': 'applied'}))
        if start:  # a failed Start: the same token, even past its expiry, applies it again
            if archived:
                return Refused(rule='project_archived', reason='Project is archived')
            nonce = self.tokens.verify(token, VERB, actor, inputs, allow_expired=True)
            if isinstance(nonce, Refused):
                return nonce
            if frameio is None:
                return Refused(rule='config', reason='The Frame.io worker is not reachable from this process; the Frame.io project cannot be made')
            return self._apply_start(start, frameio)
        nonce = self.tokens.verify(token, VERB, actor, inputs)
        if isinstance(nonce, Refused):
            return nonce
        with self.registry._transaction() as db:
            if db.execute('SELECT 1 FROM used_preview_tokens WHERE nonce=?', (nonce,)).fetchone():
                return Refused(rule='token_used', reason='Preview token has already been used')
            saved = db.execute('SELECT state FROM jo_state_previews WHERE nonce=?', (nonce,)).fetchone()
        if not saved:
            return Refused(rule='preview', reason='Preview not found')
        planned = json.loads(saved[0])
        template = next(step['template'] for step in planned['steps'] if step['step'] == 'milestone template')
        name = next(step['project_name'] for step in planned['steps'] if step['step'] == 'frameio')
        fresh = self._plan(values, actor, key, template, name)
        if isinstance(fresh, Refused):
            return fresh
        if fresh != planned:
            return Refused(rule='project_changed', reason='The plan changed since the preview; preview again')
        approval = self._call('approval', {'gate': GATE, 'target': values['project']}, actor, f'{key}:approval', None)
        if isinstance(approval, Preview):
            approval = self._call('approval', {'gate': GATE, 'target': values['project']}, actor, f'{key}:approval', approval.token)
        if refusal := _step(approval, 'Approval'):
            return refusal
        if not isinstance(approval, Done):
            return Refused(rule='step', reason='Approval: needs a person')
        now = self.registry._clock()
        with self.registry._transaction() as db:
            made = db.execute('INSERT INTO project_starts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL) ON CONFLICT DO NOTHING',
                              (key, values['project'], approval.result['approval_id'], actor, fingerprint, json.dumps(values), json.dumps(planned), nonce, 'waiting'))
            if made.rowcount:
                audit = db.execute('INSERT INTO audit (project_id, action, actor, at, before, after) VALUES (?, ?, ?, ?, NULL, ?)',
                                   (values['project'], 'project_start_requested', actor, now, json.dumps({'idempotency_key': key, 'approval_id': approval.result['approval_id']})))
                _activity(db, values['project'], now, actor, 'project_start_requested', 'project start requested', audit.lastrowid)
        stored = self._start(key)
        assert stored is not None
        return Done(result=self._state(stored))

    def _start(self, key: str) -> dict[str, Any] | None:
        """The stored Start of this key, or None."""
        with self.registry._transaction() as db:
            row = db.execute('SELECT project_id, approval_id, actor, fingerprint, inputs, plan, nonce, state, reason FROM project_starts WHERE idempotency_key=?', (key,)).fetchone()
        names = ('project_id', 'approval_id', 'actor', 'fingerprint', 'inputs', 'plan', 'nonce', 'state', 'reason')
        return None if row is None else {'idempotency_key': key, **dict(zip(names, row))}

    def _state(self, start: dict[str, Any]) -> dict[str, Any]:
        """What a read of the Start returns: `waiting_for_approval`, `applying` (granted, not yet applied), `applied` with its result, `denied` or `failed`."""
        key, state = start['idempotency_key'], start['state']
        if state == 'applied':
            with self.registry._transaction() as db:
                return {'state': 'applied', **json.loads(db.execute('SELECT result FROM verb_requests WHERE idempotency_key=?', (key,)).fetchone()[0])}
        if state == 'waiting':
            approval = Approvals(self.registry, self.tokens).get(start['approval_id'])
            assert approval is not None
            state = 'waiting_for_approval' if approval['state'] == 'requested' else 'applying'
        return {'state': state, 'project_id': start['project_id'], 'idempotency_key': key, 'approval_id': start['approval_id'],
                **({'reason': start['reason']} if start['reason'] else {})}

    # The apply, worker side

    def run_apply(self, payload: Mapping[str, Any], frameio: FrameioProjectWriter) -> dict[str, Any]:
        """Apply the Start of a granted Approval for a queued job; running it again changes nothing."""
        start = self._start(payload['key'])
        if start is None:
            return {'outcome': 'refused', 'rule': 'start', 'reason': 'Start not found'}
        outcome = self._apply_start(start, frameio)
        if isinstance(outcome, Refused):
            return {'outcome': 'refused', 'rule': outcome.rule, 'reason': outcome.reason}
        HeldActivity(self.registry).release_linked(self.queue.submit)  # review activity that waited for this Frame.io project (#272)
        return {'outcome': 'done', 'state': self._state(self._start(start['idempotency_key']) or start)['state']}

    def _apply_start(self, start: dict[str, Any], frameio: FrameioProjectWriter) -> Outcome:
        """The Frame.io step, then the t6 steps, as the Person who confirmed the Start; a refusal or a Frame.io failure records the Start as failed."""
        key, project, actor = start['idempotency_key'], start['project_id'], start['actor']
        if start['state'] in ('applied', 'denied'):
            return Done(result=self._state(start))
        approval = Approvals(self.registry, self.tokens).get(start['approval_id'])
        if approval is None or approval['state'] != 'granted':
            return Refused(rule='approval', reason='The Approval has not been granted')
        values, planned = json.loads(start['inputs']), json.loads(start['plan'])
        with self.registry._transaction() as db:
            archived = db.execute('SELECT 1 FROM project_archives WHERE project_id=?', (project,)).fetchone() is not None
            # A step of this Start has been applied: it is finished with the plan it began with, since its own steps now exist and are not planned again.
            begun = db.execute('SELECT 1 FROM verb_requests WHERE instr(idempotency_key, ?) = 1 AND idempotency_key != ?', (f'{key}:', f'{key}:approval')).fetchone()
        if archived:
            return self._fail(start, Refused(rule='project_archived', reason='Project is archived'))
        if not begun:
            template = next(step['template'] for step in planned['steps'] if step['step'] == 'milestone template')
            name = next(step['project_name'] for step in planned['steps'] if step['step'] == 'frameio')
            fresh = self._plan(values, actor, key, template, name)
            if isinstance(fresh, Refused):
                return self._fail(start, fresh)
            if fresh != planned:
                return self._fail(start, Refused(rule='project_changed', reason='The plan changed since it was confirmed'))
        result = self._apply(values, planned, actor, key, frameio)
        if isinstance(result, Refused):
            return self._fail(start, result)
        return self._finish(planned, result, start['nonce'], start['fingerprint'], actor, key)

    def _fail(self, start: dict[str, Any], refusal: Refused) -> Refused:
        """Record the Start as failed with the rule and reason, in Audit and Activity; nothing more is applied."""
        reason, now = f'{refusal.rule}: {refusal.reason}', self.registry._clock()
        with self.registry._transaction() as db:
            db.execute("UPDATE project_starts SET state='failed', reason=? WHERE idempotency_key=?", (reason, start['idempotency_key']))
            audit = db.execute('INSERT INTO audit (project_id, action, actor, at, before, after) VALUES (?, ?, ?, ?, NULL, ?)',
                               (start['project_id'], 'project_start_failed', start['actor'], now,
                                json.dumps({'idempotency_key': start['idempotency_key'], 'rule': refusal.rule, 'reason': refusal.reason})))
            _activity(db, start['project_id'], now, start['actor'], 'project_start_failed', f'project start failed: {reason}', audit.lastrowid)
        return refusal

    def _call(self, kind: str, values: dict[str, Any], actor: str, key: str, confirm: str | None) -> Outcome:
        """One step's write on the store that owns it: its preview without `confirm`, its Done with the preview's token."""
        if kind == 'approval':
            return Approvals(self.registry, self.tokens).write('request', values, actor=actor, idempotency_key=key, confirm=confirm)
        if kind == 'deliverable':
            return DeliverableStore(self.registry, self.tokens, self.project_type).define(values, actor=actor, idempotency_key=key, confirm=confirm)
        if kind == 'media':
            return MediaStore(self.registry, self.tokens).write('link', values, actor=actor, idempotency_key=key, confirm=confirm)
        if kind == 'frameio project':
            return MediaStore(self.registry, self.tokens).frameio_project_write('link', values, actor=actor, idempotency_key=key, confirm=confirm)
        return BookingStore(self.registry, self.tokens).write('book', values, actor=actor, idempotency_key=key, confirm=confirm)

    def _apply(self, values: dict[str, Any], plan: dict[str, Any], actor: str, key: str, frameio: FrameioProjectWriter) -> dict[str, Any] | Refused:
        """Ask Frame.io, then run each t6 step through its own store, preview then confirm, under keys derived from the Start's key."""
        project, template = plan['project_id'], next(s['template'] for s in plan['steps'] if s['step'] == 'milestone template')
        step = next(s for s in plan['steps'] if s['step'] == 'frameio')
        known, taken = MediaStore(self.registry, self.tokens).frameio_projects(project)
        made = frameio.create_project_folders(project, step['project_name'], tuple(step['folders']), known_project_id=known, taken=taken)
        if isinstance(made, Failed):
            return Refused(rule='frameio', reason=f'Frame.io: {made.reason}')
        results: dict[str, list[dict[str, Any]]] = {'approval': [], 'deliverable': [], 'frameio project': [], 'frameio folder': [], 'media': [], 'booking': []}

        def run(kind: str, label: str, suffix: str, step_values: dict[str, Any], into: str | None = None) -> Refused | None:
            outcome = self._call(kind, step_values, actor, f'{key}:{suffix}', None)
            if isinstance(outcome, Preview):
                outcome = self._call(kind, step_values, actor, f'{key}:{suffix}', outcome.token)
            if refusal := _step(outcome, label):
                return refusal
            if not isinstance(outcome, Done):
                return Refused(rule='step', reason=f'{label}: needs a person')
            results[into or kind].append(outcome.result)
            return None

        steps: list[tuple[str, str, str, dict[str, Any]]] = [('approval', 'Approval', 'approval', {'gate': GATE, 'target': project})]
        steps += [('deliverable', f'Deliverable {item["code"]}', f'deliverable:{item["code"]}', {'project': project, **item, 'template': template})
                  for item in values['deliverables']]
        for kind, label, suffix, step_values in steps:
            if refusal := run(kind, label, suffix, step_values):
                return refusal
        owners = {item['code']: result['deliverable_id'] for item, result in zip(values['deliverables'], results['deliverable'])}
        if refusal := run('frameio project', 'Frame.io project', 'frameio:project', {'project': project, 'frameio_project': made.frameio_project_id}):
            return refusal
        for code in step['folders']:
            if refusal := run('media', f'Frame.io folder {code}', f'frameio:folder:{code}',
                              {'owner': owners[code], 'kind': 'frameio-folder', 'ref': made.folders[code], 'role': 'review'}, into='frameio folder'):
                return refusal
        steps = [('media', f'Media location {index + 1}', f'media:{index}', {'owner': project, **item}) for index, item in enumerate(values['media'])]
        steps += [('booking', f'Booking {index + 1}', f'booking:{index}', {**item, 'project': project}) for index, item in enumerate(values['bookings'])]
        for kind, label, suffix, step_values in steps:
            if refusal := run(kind, label, suffix, step_values):
                return refusal
        return {'project_id': project, 'idempotency_key': key, 'approval_id': results['approval'][0]['approval_id'],
                'deliverable_ids': [r['deliverable_id'] for r in results['deliverable']],
                'frameio_project_id': made.frameio_project_id,
                'frameio_folders': dict(made.folders),
                'frameio_media_ids': [r['media_id'] for r in results['frameio project'] + results['frameio folder']],
                'media_ids': [r['media_id'] for r in results['media']],
                'booking_ids': [r['booking_id'] for r in results['booking']],
                'manual_steps': plan['manual_steps']}

    def _finish(self, plan: dict[str, Any], result: dict[str, Any], nonce: str, fingerprint: str, actor: str, key: str) -> Outcome:
        """Record the Start in Audit and Activity with its manual steps, spend the token, keep the result under its key and mark the Start applied."""
        project, now = plan['project_id'], self.registry._clock()
        with self.registry._transaction() as db:
            previous = db.execute('SELECT result FROM verb_requests WHERE idempotency_key=?', (key,)).fetchone()
            if previous:
                return Done(result={'state': 'applied', **json.loads(previous[0])})
            audit = db.execute('INSERT INTO audit (project_id, action, actor, at, before, after) VALUES (?, ?, ?, ?, NULL, ?)',
                               (project, 'project_start', actor, now, json.dumps(result)))
            for action, summary in [('project_start', 'project started'),
                                    *(('project_start_manual', f'manual step: {name}') for name in plan['manual_steps'])]:
                db.execute('INSERT INTO activity (project_id, subject_id, at, actor, action, summary, audit_id) VALUES (?, ?, ?, ?, ?, ?, ?)',
                           (project, project, now, actor, action, summary, audit.lastrowid))
            db.execute('INSERT INTO used_preview_tokens VALUES (?)', (nonce,))
            db.execute('INSERT INTO verb_requests VALUES (?, ?, ?)', (key, fingerprint, json.dumps(result)))
            db.execute("UPDATE project_starts SET state='applied', reason=NULL WHERE idempotency_key=?", (key,))
            InternalJobQueue(self.registry, clock=lambda: now).submit(QUEUE, JobKind.PRIVACY_REGISTRY_REBUILD.value, key, {}, db=db)
        return Done(result={'state': 'applied', **result})
