"""Package handoffs with immutable spec snapshots and recorded checklist ticks."""
from __future__ import annotations

from dataclasses import asdict
import json
import secrets
from typing import Any

from t6.ids import EntityKind, T6Id
from t6.preview import PreviewTokens
from t6.registry.sqlite import SqliteProjectRegistry
from t6.verbs import Ambiguous, Done, Outcome, Refused


class PackageStore:
    def __init__(self, registry: SqliteProjectRegistry, tokens: PreviewTokens, specs: Any = None) -> None:
        self.registry, self.tokens, self.specs = registry, tokens, specs

    @staticmethod
    def _record(row: Any) -> dict[str, Any]:
        record = dict(zip(('id', 'deliverable_id', 'kind', 'from_person', 'to', 'recipient', 'note', 'spec', 'checklist', 'state', 'reason', 'revision'), row))
        record['spec'] = json.loads(record['spec'])
        record['checklist'] = json.loads(record['checklist'])
        record['missing'] = [item['text'] for item in record['checklist'] if item['checked_at'] is None]
        record['kasado'] = all(item['checked_at'] is not None for item in record['checklist'] if item['blocking'])
        return record

    def get(self, package: str) -> dict[str, Any] | None:
        with self.registry._transaction() as db:
            row = db.execute('SELECT * FROM packages WHERE id = ?', (package,)).fetchone()
            return self._record(row) if row else None

    def list(self, missing: bool = False) -> list[dict[str, Any]]:
        with self.registry._transaction() as db:
            records = [self._record(row) for row in db.execute("SELECT * FROM packages WHERE state IN ('prepared', 'handed_off') ORDER BY rowid")]
        return [record for record in records if record['missing']] if missing else records

    def write(self, verb: str, values: dict[str, Any], *, actor: str, idempotency_key: str | None = None, confirm: str | None = None) -> Outcome:
        if verb not in ('prepare', 'check', 'handoff', 'accept', 'reject'):
            return Refused(rule='verb', reason='Unknown Package verb')
        if (idempotency_key is not None and not idempotency_key.strip()) or (confirm and not idempotency_key):
            return Refused(rule='idempotency_key', reason='A nonblank idempotency key is required')
        key = idempotency_key or secrets.token_hex(16)
        inputs = {**values, 'idempotency_key': key}
        fingerprint = json.dumps({'verb': verb, 'actor': actor, **inputs}, sort_keys=True, separators=(',', ':'))
        nonce = self.tokens.verify(confirm, 'package ' + verb, actor, inputs) if confirm else None
        if isinstance(nonce, Refused):
            return nonce
        with self.registry._transaction() as db:
            if nonce and db.execute('SELECT 1 FROM used_preview_tokens WHERE nonce = ?', (nonce,)).fetchone():
                return Refused(rule='token_used', reason='Preview token has already been used')
            previous = db.execute('SELECT fingerprint, result FROM verb_requests WHERE idempotency_key = ?', (key,)).fetchone()
            if previous:
                if previous[0] != fingerprint:
                    return Refused(rule='idempotency_key', reason='Idempotency key was used for a different request')
                if nonce:
                    db.execute('INSERT INTO used_preview_tokens VALUES (?)', (nonce,))
                    return Done(result=json.loads(previous[1]))
                return self.tokens.create('package ' + verb, actor, inputs, json.loads(previous[1]))
            if not db.execute('SELECT 1 FROM persons WHERE id = ? AND active = 1', (actor,)).fetchone():
                return Refused(rule='person', reason='An active Person is required')
            package = values.get('package')
            if verb == 'prepare':
                if any(not isinstance(values.get(k), str) or not values[k].strip() for k in ('deliverable', 'kind', 'to', 'recipient')):
                    return Refused(rule='input', reason='Deliverable, kind, destination and recipient are required')
                row = db.execute('SELECT project_id FROM deliverables WHERE id = ?', (values['deliverable'],)).fetchone()
                if not row:
                    return Refused(rule='deliverable', reason='Deliverable not found')
                project = row[0]
                if not db.execute('SELECT 1 FROM persons WHERE id = ? AND active = 1', (values['recipient'],)).fetchone():
                    return Refused(rule='recipient', reason='Active recipient Person not found')
                if self.specs is None:
                    return Refused(rule='spec', reason='Package Spec reader is required')
                try:
                    specs = self.specs.find(values['kind'], values.get('client_spec'))
                except ValueError as error:
                    return Refused(rule='spec', reason=str(error))
                if not specs:
                    return Refused(rule='spec', reason='Package Spec not found')
                if len(specs) > 1:
                    return Ambiguous(candidates=[spec.page_id for spec in specs])
                spec = json.loads(json.dumps(asdict(specs[0])))
                checklist = [{**asdict(item), 'checked_by': None, 'checked_at': None} for item in specs[0].items]
                snapshot = json.dumps([spec, checklist], sort_keys=True)
                record = None
            else:
                row = db.execute('SELECT * FROM packages WHERE id = ?', (package,)).fetchone()
                if not row:
                    return Refused(rule='package', reason='Package not found')
                record = self._record(row)
                project = db.execute('SELECT project_id FROM deliverables WHERE id = ?', (record['deliverable_id'],)).fetchone()[0]
                snapshot = json.dumps([package, record['revision']])
                if verb in ('accept', 'reject') and actor != record['recipient']:
                    return Refused(rule='recipient', reason='Only the recipient may accept or reject')
                if verb == 'reject' and (not isinstance(values.get('reason'), str) or not values['reason'].strip()):
                    return Refused(rule='reason', reason='A reason is required to reject')
                if verb == 'check':
                    if record['state'] != 'prepared':
                        return Refused(rule='state', reason='Only a prepared Package can be checked')
                    matches = [item for item in record['checklist'] if item['text'] == values.get('item')]
                    if len(matches) != 1:
                        return Refused(rule='item', reason='Checklist item must identify one item')
                    if matches[0]['checked_at'] is not None:
                        return Refused(rule='item', reason='Checklist item is already checked')
                elif verb == 'handoff':
                    if record['state'] != 'prepared':
                        return Refused(rule='state', reason='Package is not prepared')
                    blocking = [i['text'] for i in record['checklist'] if i['blocking'] and i['checked_at'] is None]
                    if blocking:
                        return Refused(rule='checklist', reason='Missing blocking items: ' + ', '.join(blocking))
                elif record['state'] != 'handed_off':
                    return Refused(rule='state', reason='Package has not been handed off')
            plan = {'action': verb, **values, 'project_id': project, 'idempotency_key': key}
            if verb == 'prepare':
                plan.update(spec=spec, checklist=checklist)
            elif verb == 'handoff':
                plan['missing'] = record['missing']
            if not nonce:
                preview = self.tokens.create('package ' + verb, actor, inputs, plan)
                preview_nonce = self.tokens.verify(preview.token, 'package ' + verb, actor, inputs)
                db.execute('INSERT INTO jo_state_previews VALUES (?, ?)', (preview_nonce, snapshot))
                return preview
            observed = db.execute('SELECT state FROM jo_state_previews WHERE nonce = ?', (nonce,)).fetchone()
            if not observed or observed[0] != snapshot:
                return Refused(rule='package_changed', reason='Package or spec changed since preview; preview again')
            now = self.registry._clock()
            if verb == 'prepare':
                package = str(T6Id.mint(EntityKind.PACKAGE))
                db.execute('INSERT INTO packages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 1)', (package, values['deliverable'], values['kind'], actor, values['to'], values['recipient'], values.get('note'), json.dumps(spec), json.dumps(checklist), 'prepared'))
            elif verb == 'check':
                matches[0]['checked_by'], matches[0]['checked_at'] = actor, now
                db.execute('UPDATE packages SET checklist = ?, revision = revision + 1 WHERE id = ?', (json.dumps(record['checklist']), package))
            else:
                db.execute('UPDATE packages SET state = ?, reason = ?, revision = revision + 1 WHERE id = ?', ({'handoff': 'handed_off', 'accept': 'accepted', 'reject': 'rejected'}[verb], values.get('reason'), package))
            result = {**plan, 'package_id': package}
            cursor = db.execute('INSERT INTO audit (project_id, action, actor, at, before, after) VALUES (?, ?, ?, ?, ?, ?)', (package, 'package_' + verb, actor, now, json.dumps(record), json.dumps(result)))
            summary = 'Package ' + verb
            if verb == 'handoff' and plan['missing']:
                summary += '; missing: ' + ', '.join(plan['missing'])
            db.execute('INSERT INTO activity (project_id, subject_id, at, actor, action, summary, audit_id) VALUES (?, ?, ?, ?, ?, ?, ?)', (project, package, now, actor, 'package_' + verb, summary, cursor.lastrowid))
            db.execute('INSERT INTO used_preview_tokens VALUES (?)', (nonce,))
            db.execute('INSERT INTO verb_requests VALUES (?, ?, ?)', (key, fingerprint, json.dumps(result)))
            return Done(result=result)
