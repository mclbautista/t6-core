"""`project start`: a previewed plan built by a job, and the t6 steps it applies (#267).

The verb returns a job id. The main worker runs `run_preview`, whose result is the Preview with its signed
token. Confirm applies the t6 steps (the Approval, the Deliverables with the milestone template of the Project
Type, the Media locations and the initial Bookings) through the stores that own them, each under a key derived
from the Start's own key, so a retry creates nothing twice. The steps that are done by hand (NAS folders, the
Resolve project, the Resolve timelines with their stamps) are listed in the plan and recorded in Activity.
"""
from __future__ import annotations

import json
import secrets
from collections.abc import Callable, Mapping
from typing import Any

from t6.approval import Approvals
from t6.bay import BookingStore
from t6.deliverable import DeliverableStore
from t6.deliverable_templates import TEMPLATES
from t6.ids import EntityKind, T6Id
from t6.media import MediaStore
from t6.person import People
from t6.preview import PreviewTokens
from t6.registry.sqlite import SqliteProjectRegistry
from t6.verbs import Done, Outcome, Preview, Refused
from t6.worker.internal_queue import InternalJobQueue

QUEUE = 'main'
JOB_KIND = 'project_start.preview'
VERB = 'project start'
GATE = 'project start'
# What a person does by hand after a Project Start, in the plan's order. The Frame.io project and the
# per-Deliverable folders come with #275 and are listed only so the order of the plan is complete.
MANUAL_STEPS = (
    ('nas folders', 'NAS folders', 'Create the NAS folders by hand: no worker has NAS write yet.'),
    ('resolve project', 'Resolve project', 'Create the Resolve project by hand, then link it with `media link`.'),
    ('resolve timelines', 'Resolve timelines with their stamps',
     'Create the Resolve timelines and write their stamps by hand (manual stamping), then record each with `timeline link`.'),
)
FRAMEIO_KINDS = ('frameio-project', 'frameio-folder')


def _fingerprint(actor: str, inputs: Mapping[str, Any]) -> str:
    return json.dumps({'verb': VERB, 'actor': actor, **inputs}, sort_keys=True, separators=(',', ':'))


def _step(outcome: Outcome, label: str) -> Refused | None:
    """A refusal from a step's own store, named by step; None when the store accepted the step."""
    return Refused(rule=outcome.rule, reason=f'{label}: {outcome.reason}') if isinstance(outcome, Refused) else None


class ProjectStartStore:
    def __init__(self, registry: SqliteProjectRegistry, tokens: PreviewTokens,
                 project_type: Callable[[str], str | None] | None = None) -> None:
        self.registry, self.tokens = registry, tokens
        self.project_type = project_type
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
        result = self.queue.result(job_id, QUEUE)
        if result is None:
            return Done(result={'job_id': job_id, 'state': 'pending'})
        if result.status != 'ok' or not isinstance(result.value, dict):
            return Refused(rule='job_failed', reason=f'The plan job failed: {result.value}')
        return Preview.model_validate(result.value) if result.value.get('outcome') == 'preview' else Refused.model_validate(result.value)

    def _job(self, job_id: int, field: str) -> Any:
        """One field of a Project Start job's payload; None for any other job."""
        with self.registry._transaction() as db:
            row = db.execute('SELECT payload FROM internal_jobs WHERE id=? AND kind=?', (job_id, JOB_KIND)).fetchone()
        return json.loads(row[0]).get(field) if row else None

    # The job, worker side

    def run_preview(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Build the plan for a queued job and return its result: the Preview with its token, or the refusal."""
        actor, inputs = payload['actor'], dict(payload['inputs'])
        key = inputs['idempotency_key']
        values = {name: value for name, value in inputs.items() if name != 'idempotency_key'}
        plan = self._plan(values, actor, key, None)
        if isinstance(plan, Refused):
            return plan.model_dump(mode='json')
        preview = self.tokens.create(VERB, actor, inputs, plan)
        nonce = self.tokens.verify(preview.token, VERB, actor, inputs)
        assert isinstance(nonce, str)
        with self.registry._transaction() as db:
            db.execute('INSERT INTO jo_state_previews VALUES (?, ?)', (nonce, json.dumps(plan)))
        return preview.model_dump(mode='json')

    def _plan(self, values: dict[str, Any], actor: str, key: str, template: str | None) -> dict[str, Any] | Refused:
        """The plan for these inputs, asked of the stores that apply each step; a refusal names the rule that stopped it."""
        project = values['project']
        try:
            T6Id.parse(project, EntityKind.PROJECT)
        except ValueError:
            return Refused(rule='project', reason='Project must be a T6 Project ID')
        with self.registry._transaction() as db:
            found = db.execute('SELECT 1 FROM projects WHERE id=?', (project,)).fetchone()
            archived = db.execute('SELECT 1 FROM project_archives WHERE project_id=?', (project,)).fetchone()
            started = db.execute("SELECT 1 FROM activity WHERE project_id=? AND action='project_start'", (project,)).fetchone()
        if not found:
            return Refused(rule='project', reason='Project not found')
        if archived:
            return Refused(rule='project_archived', reason='Project is archived')
        if started:
            return Refused(rule='project_started', reason='Project has already been started')
        if any(item['kind'] in FRAMEIO_KINDS for item in values['media']):
            return Refused(rule='frameio', reason='The Frame.io project and folders come with #275, not Project Start')
        codes = [item['code'] for item in values['deliverables']]
        if len(set(codes)) != len(codes):
            return Refused(rule='deliverable_code', reason='A Deliverable code appears twice')
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
                  {'step': 'frameio', 'kind': 'later', 'detail': 'The Frame.io project and the folder per Deliverable come with #275.'},
                  {'step': 'resolve project', 'kind': 'manual', 'detail': MANUAL_STEPS[1][2]},
                  {'step': 'resolve timelines', 'kind': 'manual', 'detail': MANUAL_STEPS[2][2]},
                  {'step': 'media locations', 'kind': 't6', 'locations': media},
                  {'step': 'milestone template', 'kind': 't6', 'template': template,
                   'milestones': list(TEMPLATES[template]) if template else [], 'deliverables': deliverables},
                  {'step': 'bookings', 'kind': 't6', 'bookings': bookings}]
        return {'project_id': project, 'idempotency_key': key, 'steps': steps, 'manual_steps': [name for _, name, _ in MANUAL_STEPS]}

    # The verb, confirm side

    def confirm(self, values: dict[str, Any], *, actor: str, idempotency_key: str | None, token: str) -> Outcome:
        """Apply the t6 steps of a previewed plan once; the same key again returns the first result."""
        if not idempotency_key or not idempotency_key.strip():
            return Refused(rule='idempotency_key', reason='A nonblank idempotency key is required')
        key = idempotency_key
        inputs = {**values, 'idempotency_key': key}
        fingerprint = _fingerprint(actor, inputs)
        nonce = self.tokens.verify(token, VERB, actor, inputs)
        if isinstance(nonce, Refused):
            return nonce
        with self.registry._transaction() as db:
            previous = db.execute('SELECT fingerprint, result FROM verb_requests WHERE idempotency_key=?', (key,)).fetchone()
            if previous and previous[0] != fingerprint:
                return Refused(rule='idempotency_key', reason='Idempotency key was used for a different request')
            if previous:  # a retried confirm gets the first result, never a second Start
                return Done(result=json.loads(previous[1]))
            if db.execute('SELECT 1 FROM used_preview_tokens WHERE nonce=?', (nonce,)).fetchone():
                return Refused(rule='token_used', reason='Preview token has already been used')
            saved = db.execute('SELECT state FROM jo_state_previews WHERE nonce=?', (nonce,)).fetchone()
        if not saved:
            return Refused(rule='preview', reason='Preview not found')
        planned = json.loads(saved[0])
        template = next(step['template'] for step in planned['steps'] if step['step'] == 'milestone template')
        fresh = self._plan(values, actor, key, template)
        if isinstance(fresh, Refused):
            return fresh
        if fresh != planned:
            return Refused(rule='project_changed', reason='The plan changed since the preview; preview again')
        result = self._apply(values, planned, actor, key)
        if isinstance(result, Refused):
            return result
        return self._finish(planned, result, nonce, fingerprint, actor, key)

    def _call(self, kind: str, values: dict[str, Any], actor: str, key: str, confirm: str | None) -> Outcome:
        """One step's write on the store that owns it: its preview without `confirm`, its Done with the preview's token."""
        if kind == 'approval':
            return Approvals(self.registry, self.tokens).write('request', values, actor=actor, idempotency_key=key, confirm=confirm)
        if kind == 'deliverable':
            return DeliverableStore(self.registry, self.tokens, self.project_type).define(values, actor=actor, idempotency_key=key, confirm=confirm)
        if kind == 'media':
            return MediaStore(self.registry, self.tokens).write('link', values, actor=actor, idempotency_key=key, confirm=confirm)
        return BookingStore(self.registry, self.tokens).write('book', values, actor=actor, idempotency_key=key, confirm=confirm)

    def _apply(self, values: dict[str, Any], plan: dict[str, Any], actor: str, key: str) -> dict[str, Any] | Refused:
        """Run each t6 step through its own store, preview then confirm, under keys derived from the Start's key."""
        project, template = plan['project_id'], next(s['template'] for s in plan['steps'] if s['step'] == 'milestone template')
        steps: list[tuple[str, str, str, dict[str, Any]]] = [('approval', 'Approval', 'approval', {'gate': GATE, 'target': project})]
        steps += [('deliverable', f'Deliverable {item["code"]}', f'deliverable:{item["code"]}', {'project': project, **item, 'template': template})
                  for item in values['deliverables']]
        steps += [('media', f'Media location {index + 1}', f'media:{index}', {'owner': project, **item}) for index, item in enumerate(values['media'])]
        steps += [('booking', f'Booking {index + 1}', f'booking:{index}', {**item, 'project': project}) for index, item in enumerate(values['bookings'])]
        results: dict[str, list[dict[str, Any]]] = {'approval': [], 'deliverable': [], 'media': [], 'booking': []}
        for kind, label, suffix, step_values in steps:
            outcome = self._call(kind, step_values, actor, f'{key}:{suffix}', None)
            if isinstance(outcome, Preview):
                outcome = self._call(kind, step_values, actor, f'{key}:{suffix}', outcome.token)
            if refusal := _step(outcome, label):
                return refusal
            if not isinstance(outcome, Done):
                return Refused(rule='step', reason=f'{label}: needs a person')
            results[kind].append(outcome.result)
        return {'project_id': project, 'idempotency_key': key, 'approval_id': results['approval'][0]['approval_id'],
                'deliverable_ids': [r['deliverable_id'] for r in results['deliverable']],
                'media_ids': [r['media_id'] for r in results['media']],
                'booking_ids': [r['booking_id'] for r in results['booking']],
                'manual_steps': plan['manual_steps']}

    def _finish(self, plan: dict[str, Any], result: dict[str, Any], nonce: str, fingerprint: str, actor: str, key: str) -> Outcome:
        """Record the Start in Audit and Activity with its manual steps, spend the token and keep the result under its key."""
        project, now = plan['project_id'], self.registry._clock()
        with self.registry._transaction() as db:
            previous = db.execute('SELECT result FROM verb_requests WHERE idempotency_key=?', (key,)).fetchone()
            if previous:
                return Done(result=json.loads(previous[0]))
            audit = db.execute('INSERT INTO audit (project_id, action, actor, at, before, after) VALUES (?, ?, ?, ?, NULL, ?)',
                               (project, 'project_start', actor, now, json.dumps(result)))
            for action, summary in [('project_start', 'project started'),
                                    *(('project_start_manual', f'manual step: {name}') for name in plan['manual_steps'])]:
                db.execute('INSERT INTO activity (project_id, subject_id, at, actor, action, summary, audit_id) VALUES (?, ?, ?, ?, ?, ?, ?)',
                           (project, project, now, actor, action, summary, audit.lastrowid))
            db.execute('INSERT INTO used_preview_tokens VALUES (?)', (nonce,))
            db.execute('INSERT INTO verb_requests VALUES (?, ?, ?)', (key, fingerprint, json.dumps(result)))
        return Done(result=result)
