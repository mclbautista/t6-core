"""Drive custody and assignment verbs on the t6 database.

Custody is an append-only event log and status is derived from it. An
Assignment carries its label, Project, role and dates; unassign ends it and
keeps it. A shipment groups the releases of one handover.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import date, datetime
from typing import Any

from t6.domain.studio_time import MANILA
from t6.drives import Assignment, CustodyEvent, CustodyKind, Party, PartyKind, Ref, Role
from t6.drives.sqlite import SqliteDriveStore, _assignment, _dump
from t6.ids import EntityKind, T6Id
from t6.preview import PreviewTokens
from t6.registry.core import normalize_page_id
from t6.registry.sqlite import SqliteProjectRegistry
from t6.verbs import Ambiguous, Done, Outcome, Refused

VERBS = ('receive', 'release', 'return', 'retire', 'assign', 'unassign')
_KINDS = {'receive': CustodyKind.RECEIVED, 'release': CustodyKind.RELEASED, 'return': CustodyKind.RETURNED, 'retire': CustodyKind.RETIRED}
_PAGE = re.compile(r'[0-9a-f]{32}')


def _party(values: dict[str, Any]) -> Party | None:
    text = (values.get('party') or '').strip()
    if not text:
        return None
    page = normalize_page_id(text)
    return Party(PartyKind(values['party_kind']), Ref(page_id=page) if _PAGE.fullmatch(page) else Ref(text=text))


class DriveVerbs:
    def __init__(self, registry: SqliteProjectRegistry, tokens: PreviewTokens) -> None:
        self.registry, self.tokens = registry, tokens

    def _today(self) -> str:
        return datetime.fromtimestamp(self.registry._clock(), MANILA).date().isoformat()

    def write(self, verb: str, values: dict[str, Any], *, actor: str, idempotency_key: str | None = None,
              confirm: str | None = None) -> Outcome:
        if not actor.strip() or verb not in VERBS:
            return Refused(rule='input', reason='A valid actor and verb are required')
        if (idempotency_key is not None and not idempotency_key.strip()) or (confirm and not idempotency_key):
            return Refused(rule='idempotency_key', reason='A nonblank idempotency key is required')
        invalid = self._validate(verb, values)
        if invalid:
            return invalid
        key = idempotency_key or secrets.token_hex(16)
        inputs = {**values, 'idempotency_key': key}
        name = 'drive ' + verb
        fingerprint = json.dumps({'verb': name, 'actor': actor, **inputs}, sort_keys=True, separators=(',', ':'))
        nonce = self.tokens.verify(confirm, name, actor, inputs) if confirm else None
        if isinstance(nonce, Refused):
            return nonce
        with self.registry._transaction() as db:
            if nonce and db.execute('SELECT 1 FROM used_preview_tokens WHERE nonce=?', (nonce,)).fetchone():
                return Refused(rule='token_used', reason='Preview token has already been used')
            previous = db.execute('SELECT fingerprint, result FROM verb_requests WHERE idempotency_key=?', (key,)).fetchone()
            if previous and previous[0] != fingerprint:
                return Refused(rule='idempotency_key', reason='Idempotency key was used for a different request')
            if previous:
                if nonce:
                    db.execute('INSERT INTO used_preview_tokens VALUES (?)', (nonce,))
                    return Done(result=json.loads(previous[1]))
                return self.tokens.create(name, actor, inputs, json.loads(previous[1]))
            planned = self._plan(db, verb, values, key, actor)
            if isinstance(planned, (Refused, Ambiguous)):
                return planned
            plan, state, changes = planned
            if not nonce:
                preview = self.tokens.create(name, actor, inputs, plan)
                preview_nonce = self.tokens.verify(preview.token, name, actor, inputs)
                db.execute('INSERT INTO drive_previews VALUES (?, ?)', (preview_nonce, json.dumps({'plan': plan, 'state': state})))
                return preview
            saved = db.execute('SELECT snapshot FROM drive_previews WHERE nonce=?', (nonce,)).fetchone()
            if not saved:
                return Refused(rule='preview', reason='Preview not found')
            snapshot = json.loads(saved[0])
            if snapshot['state'] != state or snapshot['plan'] != plan:
                return Refused(rule='drive_changed', reason='Drive changed since preview; preview again')
            now = self.registry._clock()
            for drive_id, before, after in changes(db):
                audit = db.execute('INSERT INTO audit (project_id, action, actor, at, before, after) VALUES (?, ?, ?, ?, ?, ?)',
                                   (drive_id, 'drive_' + verb, actor, now, json.dumps(before), json.dumps(after)))
                db.execute('INSERT INTO activity (project_id, subject_id, at, actor, action, summary, audit_id) VALUES (?, ?, ?, ?, ?, ?, ?)',
                           (plan['project_id'], drive_id, now, actor, 'drive_' + verb, name, audit.lastrowid))
            db.execute('INSERT INTO used_preview_tokens VALUES (?)', (nonce,))
            db.execute('INSERT INTO verb_requests VALUES (?, ?, ?)', (key, fingerprint, json.dumps(plan)))
            return Done(result=plan)

    def _validate(self, verb: str, values: dict[str, Any]) -> Refused | None:
        drives = values.get('drives') if verb == 'release' else [values.get('drive')]
        if not isinstance(drives, list) or not drives or not all(isinstance(d, str) for d in drives):
            return Refused(rule='input', reason='At least one Drive is required')
        if not isinstance(values.get('project'), str):
            return Refused(rule='input', reason='A Project is required')
        for name in ('on', 'start', 'end'):
            if values.get(name) is not None:
                try:
                    date.fromisoformat(values[name])
                except (TypeError, ValueError):
                    return Refused(rule='input', reason=f'{name} must be a date as YYYY-MM-DD')
        if values.get('party') and values.get('party_kind') not in ('contact', 'studio'):
            return Refused(rule='input', reason='party_kind must be contact or studio')
        if verb == 'release' and not (values.get('party') or '').strip():
            return Refused(rule='input', reason='A release needs the Contact or Studio receiving the Drives')
        if verb == 'retire' and not (values.get('reason') or '').strip():
            return Refused(rule='reason', reason='A retirement needs a reason, such as dead, lost or sold')
        if verb == 'assign':
            if not (values.get('label') or '').strip():
                return Refused(rule='input', reason='An Assignment needs a label')
            if values.get('role') not in {role.value for role in Role}:
                return Refused(rule='input', reason='role must be one of ' + ', '.join(role.value for role in Role))
        return None

    def _plan(self, db: Any, verb: str, values: dict[str, Any], key: str, actor: str) -> Any:
        ids = list(dict.fromkeys(values['drives'])) if verb == 'release' else [values['drive']]
        for drive_id in ids:
            if not db.execute('SELECT 1 FROM drives WHERE id=?', (drive_id,)).fetchone():
                return Refused(rule='drive', reason=f'Drive {drive_id} not found')
        project = values['project']
        try:
            T6Id.parse(project, EntityKind.PROJECT)
        except ValueError:
            return Refused(rule='project', reason='Project must be a t6 Project ID')
        if not db.execute('SELECT 1 FROM projects WHERE id=?', (project,)).fetchone():
            return Refused(rule='project', reason='Project not found')
        state = [[d, db.execute('SELECT COUNT(*) FROM drive_events WHERE drive_id=?', (d,)).fetchone()[0],
                  [r[0] for r in db.execute('SELECT data FROM drive_assignments WHERE drive_id=? ORDER BY id', (d,))]] for d in ids]
        today = self._today()
        plan: dict[str, Any] = {'action': 'drive_' + verb, 'drive_ids': ids, 'project_id': project, 'idempotency_key': key}
        if verb in _KINDS:
            on = values.get('on') or today
            try:
                event = CustodyEvent(kind=_KINDS[verb], on=date.fromisoformat(on), party=_party(values), project=Ref(page_id=project),
                                     logged_by=Ref(page_id=actor), reason=values.get('reason'),
                                     shipment_id='shipment-' + hashlib.sha256(key.encode()).hexdigest()[:32] if verb == 'release' else None)
            except ValueError as error:
                return Refused(rule='input', reason=str(error))
            plan['on'] = on
            if verb == 'release':
                plan['shipment_id'] = event.shipment_id
                if values.get('delivery'):
                    row = db.execute("SELECT c.kind, d.project_id FROM delivery_carriers c JOIN packages p ON p.id=c.package_id JOIN deliverables d ON d.id=p.deliverable_id WHERE c.package_id=?", (values['delivery'],)).fetchone()
                    if not row or row[0] != 'drive':
                        return Refused(rule='delivery', reason='Delivery not found or its carrier is not a drive')
                    if row[1] != project:
                        return Refused(rule='delivery', reason='Delivery belongs to a different Project')
                    plan['delivery_id'] = values['delivery']
            return plan, state, lambda db_: self._append(db_, event, plan, ids)
        if verb == 'assign':
            start = values.get('start') or today
            try:
                assignment = Assignment(label=values['label'], project=Ref(page_id=project), role=Role(values['role']),
                                        start=date.fromisoformat(start), end=date.fromisoformat(values['end']) if values.get('end') else None)
            except ValueError as error:
                return Refused(rule='input', reason=str(error))
            plan.update(label=assignment.label, role=values['role'], start=start, end=values.get('end'))
            return plan, state, lambda db_: self._assign(db_, ids[0], assignment)
        end = values.get('end') or today
        open_rows = [(row[0], _assignment(row[1])) for row in db.execute('SELECT id, data FROM drive_assignments WHERE drive_id=? ORDER BY id', (ids[0],))]
        found = [(row_id, a) for row_id, a in open_rows
                 if a.is_open and a.project.page_id == project and (values.get('label') is None or a.label == values['label'].strip())]
        if not found:
            return Refused(rule='assignment', reason='No open Assignment for this Drive and Project')
        if len(found) > 1:
            return Ambiguous(candidates=[a.label for _, a in found])
        row_id, held = found[0]
        try:
            closed = Assignment(label=held.label, project=held.project, role=held.role, start=held.start,
                                end=date.fromisoformat(end), remarks=held.remarks)
        except ValueError as error:
            return Refused(rule='input', reason=str(error))
        plan.update(label=held.label, end=end)
        state.append(row_id)
        return plan, state, lambda db_: self._close(db_, ids[0], row_id, held, closed)

    @staticmethod
    def _append(db: Any, event: CustodyEvent, plan: dict[str, Any], ids: list[str]) -> list[tuple[str, Any, Any]]:
        changes = []
        for drive_id in ids:
            SqliteDriveStore._append(db, drive_id, event)
            if 'delivery_id' in plan:
                db.execute('INSERT OR IGNORE INTO delivery_drives VALUES (?, ?)', (plan['delivery_id'], drive_id))
            changes.append((drive_id, None, json.loads(_dump(event))))
        return changes

    @staticmethod
    def _assign(db: Any, drive_id: str, assignment: Assignment) -> list[tuple[str, Any, Any]]:
        db.execute('INSERT INTO drive_assignments (drive_id, data) VALUES (?, ?)', (drive_id, _dump(assignment)))
        return [(drive_id, None, json.loads(_dump(assignment)))]

    @staticmethod
    def _close(db: Any, drive_id: str, row_id: int, held: Assignment, closed: Assignment) -> list[tuple[str, Any, Any]]:
        db.execute('UPDATE drive_assignments SET data=? WHERE id=?', (_dump(closed), row_id))
        return [(drive_id, json.loads(_dump(held)), json.loads(_dump(closed)))]
