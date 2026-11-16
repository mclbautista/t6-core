"""Drive custody, assignment, swing and fact verbs on the t6 database.

Custody is an append-only event log and status is derived from it. An
Assignment carries its label, Project (none for a swing held by a Person alone), role and dates; unassign ends it and
keeps it. A swing is a Drive with a `swing` Assignment that may carry a return
date; `swing return` ends it and clears the date, and overdue returns are read
from the store. A shipment groups the releases of one handover. Every Drive fact is
read and written through the DriveStore interface; this module keeps only the
verb flow (preview, confirm, idempotency, audit and activity).
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime
from typing import Any, NamedTuple

from t6 import verbflow
from t6.domain.studio_time import MANILA
from t6.drives import Assignment, CustodyEvent, CustodyKind, Drive, DriveStatus, DriveStore, FormFactor, Party, PartyKind, Ref, Role, open_duplicate
from t6.drives.sqlite import SqliteDriveStore, audit_json
from t6.preview import PreviewTokens
from t6.registry.core import normalize_page_id
from t6.registry.sqlite import SqliteProjectRegistry
from t6.verbs import Ambiguous, Outcome, Refused

FACT_VERBS = ('add', 'edit', 'promote_serial')
WRITE_VERBS = ('receive', 'release', 'return', 'retire', 'assign', 'unassign', 'swing_assign', 'swing_unassign', 'swing_return', *FACT_VERBS)
_TEXT_FACTS = ('label', 'serial', 'suspected_serial', 'review_note', 'description', 'brand', 'model', 'format', 'owner_studio_id', 'warranty', 'remarks')
_EDITABLE = (*_TEXT_FACTS, 'form_factor', 'needs_review', 'capacity_tb', 'ownership_known', 'purchase_date')
_KINDS = {'receive': CustodyKind.RECEIVED, 'release': CustodyKind.RELEASED, 'return': CustodyKind.RETURNED, 'retire': CustodyKind.RETIRED}
_CANNOT_FOLLOW: dict[str, set[DriveStatus | None]] = {'receive': {DriveStatus.IN_CUSTODY, DriveStatus.RETIRED}, 'release': {DriveStatus.RELEASED, DriveStatus.RETIRED},
                  'return': {None, DriveStatus.IN_CUSTODY, DriveStatus.RETIRED}, 'retire': {DriveStatus.RETIRED}}
_PAGE = re.compile(r'[0-9a-f]{32}')

Change = tuple[str, dict[str, Any] | None, dict[str, Any] | None]
"""One Drive changed by a verb: its ID, what it was and what it is now (for the audit entry)."""


def verb_name(verb: str) -> str:
    """The registered verb: `drive assign`, `drive promote-serial`, or `swing assign` for the swing verbs."""
    return verb.replace('_', ' ') if verb.startswith('swing_') else 'drive ' + verb.replace('_', '-')


class Plan(NamedTuple):
    """What a verb would do: the preview, the Drive state it was planned on, and the change itself."""
    plan: dict[str, Any]
    state: list[Any]
    apply: Callable[[DriveStore], list[Change]]


def _party(values: dict[str, Any]) -> Party | None:
    text = (values.get('party') or '').strip()
    if not text:
        return None
    page = normalize_page_id(text)
    return Party(PartyKind(values['party_kind']), Ref(page_id=page) if _PAGE.fullmatch(page) else Ref(text=text))


class DriveVerbs:
    def __init__(self, registry: SqliteProjectRegistry, tokens: PreviewTokens) -> None:
        self.registry, self.tokens = registry, tokens
        self.store = SqliteDriveStore(registry)

    def _today(self) -> str:
        return datetime.fromtimestamp(self.registry._clock(), MANILA).date().isoformat()

    def write(self, verb: str, values: dict[str, Any], *, actor: str, idempotency_key: str | None = None,
              confirm: str | None = None) -> Outcome:
        if not actor.strip() or verb not in WRITE_VERBS:
            return Refused(rule='input', reason='A valid actor and verb are required')
        refusal = verbflow.check_key(idempotency_key, confirm)
        if refusal:
            return refusal
        invalid = self._validate(verb, values)
        if invalid:
            return invalid
        name = verb_name(verb)
        action = name.replace(' ', '_').replace('-', '_')
        request = verbflow.begin(self.tokens, name, actor, values, idempotency_key, confirm)
        if isinstance(request, Refused):
            return request
        key, nonce = request.key, request.nonce
        with self.registry._transaction() as db:
            store = SqliteDriveStore(self.registry, within=db)
            settled = verbflow.settle(db, self.tokens, request)
            if settled:
                return settled
            planned = self._plan(store, verb, values, key, actor)
            if isinstance(planned, (Refused, Ambiguous)):
                return planned
            plan, state, apply = planned
            if not nonce:
                preview, preview_nonce = verbflow.issue(self.tokens, request, plan)
                store.save_preview(str(preview_nonce), json.dumps({'plan': plan, 'state': state}))
                return preview
            saved = store.preview(nonce)
            if not saved:
                return Refused(rule='preview', reason='Preview not found')
            snapshot = json.loads(saved)
            if snapshot['state'] != state or snapshot['plan'] != plan:
                return Refused(rule='drive_changed', reason='Drive changed since preview; preview again')
            now = self.registry._clock()
            for drive_id, before, after in apply(store):
                audit = db.execute('INSERT INTO audit (project_id, action, actor, at, before, after) VALUES (?, ?, ?, ?, ?, ?)',
                                   (drive_id, action, actor, now, json.dumps(before), json.dumps(after)))
                db.execute('INSERT INTO activity (project_id, subject_id, at, actor, action, summary, audit_id) VALUES (?, ?, ?, ?, ?, ?, ?)',
                           (plan.get('project_id') or drive_id, drive_id, now, actor, action, name, audit.lastrowid))
            return verbflow.finish(db, request, plan)

    def _validate(self, verb: str, values: dict[str, Any]) -> Refused | None:
        if verb.startswith('swing_'):
            return self._validate_swing(verb, values)
        if verb in FACT_VERBS:
            return self._validate_fact(verb, values)
        drives = values.get('drives') if verb == 'release' else [values.get('drive')]
        if not isinstance(drives, list) or not drives or not all(isinstance(d, str) for d in drives):
            return Refused(rule='input', reason='At least one Drive is required')
        if not isinstance(values.get('project'), str) and not (verb == 'unassign' and values.get('assignment') is not None):
            return Refused(rule='input', reason='A Project is required unless the Assignment number is given')
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
        if verb == 'unassign':
            if values.get('role') is not None and values['role'] not in {role.value for role in Role}:
                return Refused(rule='input', reason='role must be one of ' + ', '.join(role.value for role in Role))
            number = values.get('assignment')
            if number is not None and (not isinstance(number, int) or isinstance(number, bool) or number < 1):
                return Refused(rule='input', reason='assignment must be a number from 1')
        if verb == 'assign':
            if not (values.get('label') or '').strip():
                return Refused(rule='input', reason='An Assignment needs a label')
            if values.get('role') not in {role.value for role in Role}:
                return Refused(rule='input', reason='role must be one of ' + ', '.join(role.value for role in Role))
        return None

    @staticmethod
    def _validate_fact(verb: str, values: dict[str, Any]) -> Refused | None:
        if verb == 'add':
            if not (values.get('label') or '').strip():
                return Refused(rule='input', reason='A Drive needs a label')
        elif not isinstance(values.get('drive'), str):
            return Refused(rule='input', reason='A Drive is required')
        elif verb == 'edit' and not any(name in values for name in _EDITABLE):
            return Refused(rule='input', reason='Name at least one fact to change: ' + ', '.join(_EDITABLE))
        return None

    @staticmethod
    def _validate_swing(verb: str, values: dict[str, Any]) -> Refused | None:
        if not isinstance(values.get('drive'), str):
            return Refused(rule='input', reason='A Drive is required')
        if verb == 'swing_assign' and not isinstance(values.get('target'), str):
            return Refused(rule='input', reason='A Person or Project to assign the swing to is required')
        for name in ('start', 'end', 'return_date'):
            if values.get(name) is not None:
                try:
                    date.fromisoformat(values[name])
                except (TypeError, ValueError):
                    return Refused(rule='input', reason=f'{name} must be a date as YYYY-MM-DD')
        number = values.get('assignment')
        if number is not None and (not isinstance(number, int) or isinstance(number, bool) or number < 1):
            return Refused(rule='input', reason='assignment must be a number from 1')
        if values.get('person') is not None and not (isinstance(values['person'], str) and values['person'].startswith('t6n_')):
            return Refused(rule='input', reason='person must be a T6 Person ID')
        return None

    @staticmethod
    def _state(store: DriveStore, ids: list[str]) -> list[Any]:
        state: list[Any] = []
        for drive_id in ids:
            listed = store.list_assignments(drive_id)
            state.append([drive_id, len(store.list_events(drive_id)), len(listed), sum(a.is_open for a in listed)])
        return state

    def overdue(self) -> dict[str, Any]:
        """The swings still out past their return date, as of today in Manila."""
        today = date.fromisoformat(self._today())
        found = self.store.overdue_returns(today)
        return {'today': today.isoformat(), 'overdue': [
            {'drive_id': drive.id, 'serial': drive.serial, 'label': a.label, 'project_id': a.project.page_id if a.project else None, 'holder': a.holder,
             'start': a.start.isoformat() if a.start else None, 'return_date': a.return_date.isoformat() if a.return_date else None,
             'days_overdue': (today - a.return_date).days if a.return_date else 0} for drive, a in found]}

    @staticmethod
    def _serial_clash(store: DriveStore, drive_id: str, serial: str | None) -> Refused | None:
        held = store.find_by_serial(serial) if serial else None
        if held is not None and held.id != drive_id:
            return Refused(rule='serial', reason=f'Serial {held.serial} is already recorded on Drive {held.id}')
        return None

    def _plan_fact(self, store: DriveStore, verb: str, values: dict[str, Any], key: str) -> Plan | Refused:
        plan: dict[str, Any] = {'action': 'drive_' + verb, 'idempotency_key': key}
        if verb == 'add':
            facts = {**values, 'id': 't6d_' + hashlib.sha256(key.encode()).hexdigest()[:32]}
            try:
                if 'form_factor' in facts:
                    facts['form_factor'] = FormFactor(facts['form_factor'])
                if 'purchase_date' in facts:
                    facts['purchase_date'] = date.fromisoformat(facts['purchase_date'])
                added = Drive(**facts)
            except ValueError as error:
                return Refused(rule='input', reason=str(error))
            clash = self._serial_clash(store, added.id, added.serial)
            if clash is not None:
                return clash
            plan.update(drive_ids=[added.id], facts=audit_json(added))
            return Plan(plan, [], lambda store_: self._add(store_, added))
        drive = store.get_drive(values['drive'])
        if drive is None:
            return Refused(rule='drive', reason=f"Drive {values['drive']} not found")
        if verb == 'promote_serial':
            if drive.suspected_serial is None:
                return Refused(rule='serial', reason='The Drive has no suspected serial to promote')
            clash = self._serial_clash(store, drive.id, drive.suspected_serial)
            if clash is not None:
                return clash
            plan.update(drive_ids=[drive.id], facts=audit_json(replace(drive, serial=drive.suspected_serial, suspected_serial=None)))
            return Plan(plan, [audit_json(drive)], lambda store_: self._promote(store_, drive))
        expected = values.get('expected_version')
        if expected is not None and expected != drive.version:
            return Refused(rule='drive_changed', reason=f'Drive {drive.id} changed since its version {expected}; read it again')
        changes: dict[str, Any] = {name: values[name].strip() or None for name in _TEXT_FACTS if name in values}
        try:
            if 'form_factor' in values:
                changes['form_factor'] = FormFactor(values['form_factor']) if values['form_factor'] else None
            if 'capacity_tb' in values:
                changes['capacity_tb'] = float(values['capacity_tb']) if values['capacity_tb'].strip() else None
            if 'purchase_date' in values:
                changes['purchase_date'] = date.fromisoformat(values['purchase_date']) if values['purchase_date'].strip() else None
            for name in ('needs_review', 'ownership_known'):
                if name in values:
                    changes[name] = values[name]
            edited = replace(drive, **changes)
        except ValueError as error:
            return Refused(rule='input', reason=str(error))
        clash = self._serial_clash(store, drive.id, edited.serial) if 'serial' in changes else None
        if clash is not None:
            return clash
        plan.update(drive_ids=[drive.id], facts=audit_json(edited))
        return Plan(plan, [audit_json(drive)], lambda store_: self._edit(store_, drive, changes))

    @staticmethod
    def _add(store: DriveStore, drive: Drive) -> list[Change]:
        store.add_drive(drive)
        return [(drive.id, None, audit_json(drive))]

    @staticmethod
    def _promote(store: DriveStore, drive: Drive) -> list[Change]:
        return [(drive.id, audit_json(drive), audit_json(store.promote_suspected_serial(drive.id)))]

    @staticmethod
    def _edit(store: DriveStore, drive: Drive, changes: dict[str, Any]) -> list[Change]:
        return [(drive.id, audit_json(drive), audit_json(store.set_facts(drive.id, changes)))]

    def _plan_swing(self, store: DriveStore, verb: str, values: dict[str, Any], key: str) -> Plan | Refused | Ambiguous:
        drive_id = values['drive']
        if store.get_drive(drive_id) is None:
            return Refused(rule='drive', reason=f'Drive {drive_id} not found')
        state = self._state(store, [drive_id])
        today = self._today()
        plan: dict[str, Any] = {'action': verb, 'drive_ids': [drive_id], 'idempotency_key': key}
        if verb == 'swing_assign':
            target, project, holder = values['target'], values.get('project'), None
            if target.startswith('t6n_'):
                holder = target
            elif target.startswith('t6p_'):
                if project and project != target:
                    return Refused(rule='project', reason='The target is a Project; do not name a different one')
                project = target
            else:
                return Refused(rule='target', reason='The target must be a T6 Person ID or a T6 Project ID')
            try:
                if project:
                    store.check_project(Ref(page_id=project))
            except ValueError as error:
                return Refused(rule='project', reason=str(error))
            try:
                if holder:
                    store.check_person(holder)
            except ValueError as error:
                return Refused(rule='person', reason=str(error))
            start = values.get('start') or today
            try:
                assignment = Assignment(label=values.get('label'), project=Ref(page_id=project) if project else None, role=Role.SWING, start=date.fromisoformat(start),
                                        return_date=date.fromisoformat(values['return_date']) if values.get('return_date') else None, holder=holder)
            except ValueError as error:
                return Refused(rule='input', reason=str(error))
            clash = open_duplicate(store.list_assignments(drive_id), assignment)
            if clash is not None:
                return Refused(rule='assignment', reason=f'The Drive already has an open swing ({clash.label}) for this Project or holder; unassign or return it first')
            plan.update(project_id=project, label=assignment.label, role='swing', start=start, return_date=values.get('return_date'), holder=holder)
            return Plan(plan, state, lambda store_: self._assign(store_, drive_id, assignment))
        project, person = values.get('project'), values.get('person')
        found = [(number, a) for number, a in enumerate(store.list_assignments(drive_id), 1)
                 if a.is_open and a.role is Role.SWING and (not project or (a.project is not None and a.project.page_id == project))
                 and (not person or a.holder == person)
                 and (values.get('assignment') is None or number == values['assignment'])]
        if not found:
            return Refused(rule='assignment', reason='No open swing Assignment for this Drive')
        if len(found) > 1:
            return Ambiguous(candidates=[{'assignment': number, 'label': a.label, 'project_id': a.project.page_id if a.project else None, 'holder': a.holder,
                                          'start': a.start.isoformat() if a.start else None} for number, a in found])
        number, held = found[0]
        end = values.get('end') or today
        if held.start is not None and date.fromisoformat(end) < held.start:
            return Refused(rule='input', reason=f'an Assignment cannot end ({end}) before it starts ({held.start})')
        clears = verb == 'swing_return'
        plan.update(project_id=held.project.page_id if held.project else None, label=held.label, role='swing', end=end, assignment=number, holder=held.holder,
                    return_date=None if clears else held.return_date.isoformat() if held.return_date else None)
        state.append(number)
        return Plan(plan, state, lambda store_: self._close(store_, drive_id, number, held, date.fromisoformat(end), drop_return_date=clears))

    def _plan(self, store: DriveStore, verb: str, values: dict[str, Any], key: str, actor: str) -> Plan | Refused | Ambiguous:
        if verb.startswith('swing_'):
            return self._plan_swing(store, verb, values, key)
        if verb in FACT_VERBS:
            return self._plan_fact(store, verb, values, key)
        ids = list(dict.fromkeys(values['drives'])) if verb == 'release' else [values['drive']]
        for drive_id in ids:
            if store.get_drive(drive_id) is None:
                return Refused(rule='drive', reason=f'Drive {drive_id} not found')
        project = values.get('project')
        try:
            if project is not None:
                store.check_project(Ref(page_id=project))
        except ValueError as error:
            return Refused(rule='project', reason=str(error))
        blocked = [f"{drive_id} ({status.value if status else 'no custody events'})" for drive_id in ids
                   if (status := store.status(drive_id)) in _CANNOT_FOLLOW.get(verb, ())]
        if blocked:
            return Refused(rule='custody', reason=f"Cannot {verb} Drive {', '.join(blocked)}: the last custody step does not allow it")
        state = self._state(store, ids)
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
            delivery = None
            recipient = None
            if verb == 'release':
                plan['shipment_id'] = event.shipment_id
                recipient = plan['recipient'] = (values.get('recipient') or '').strip() or None
                if values.get('delivery'):
                    carrier = store.delivery_carrier(values['delivery'])
                    if not carrier or carrier[0] != 'drive':
                        return Refused(rule='delivery', reason='Delivery not found or its carrier is not a drive')
                    if carrier[1] != project:
                        return Refused(rule='delivery', reason='Delivery belongs to a different Project')
                    delivery = plan['delivery_id'] = values['delivery']
            return Plan(plan, state, lambda store_: self._custody(store_, verb, event, ids, delivery, recipient))
        if verb == 'assign':
            start = values.get('start') or today
            try:
                assignment = Assignment(label=values['label'], project=Ref(page_id=project), role=Role(values['role']),
                                        start=date.fromisoformat(start), end=date.fromisoformat(values['end']) if values.get('end') else None,
                                        remarks=values.get('remarks'))
            except ValueError as error:
                return Refused(rule='input', reason=str(error))
            clash = open_duplicate(store.list_assignments(ids[0]), assignment)
            if clash is not None:
                return Refused(rule='assignment', reason=f'The Drive already has an open Assignment ({clash.label}) for this Project and role; unassign it first')
            plan.update(label=assignment.label, role=values['role'], start=start, end=values.get('end'), remarks=assignment.remarks)
            return Plan(plan, state, lambda store_: self._assign(store_, ids[0], assignment))
        end = values.get('end') or today
        label = values['label'].strip() if values.get('label') is not None else None
        found = [(number, a) for number, a in enumerate(store.list_assignments(ids[0]), 1)
                 if a.is_open and (project is None or (a.project is not None and a.project.page_id == project))
                 and (label is None or a.label == label)
                 and (values.get('role') is None or (a.role is not None and a.role.value == values['role']))
                 and (values.get('assignment') is None or number == values['assignment'])]
        if not found:
            return Refused(rule='assignment', reason='No open Assignment for this Drive and Project')
        if len(found) > 1:
            return Ambiguous(candidates=[{'assignment': number, 'label': a.label, 'role': a.role.value if a.role else None,
                                          'start': a.start.isoformat() if a.start else None} for number, a in found])
        number, held = found[0]
        try:
            date.fromisoformat(end)
            if held.start is not None and date.fromisoformat(end) < held.start:
                raise ValueError(f'an Assignment cannot end ({end}) before it starts ({held.start})')
        except ValueError as error:
            return Refused(rule='input', reason=str(error))
        plan.update(label=held.label, end=end, assignment=number)
        if project is None:
            plan['project_id'] = held.project.page_id if held.project else None
        state.append(number)
        return Plan(plan, state, lambda store_: self._close(store_, ids[0], number, held, date.fromisoformat(end)))

    @staticmethod
    def _custody(store: DriveStore, verb: str, event: CustodyEvent, ids: list[str], delivery: str | None, recipient: str | None) -> list[Change]:
        after = audit_json(event)
        if delivery is not None:
            after['delivery_id'] = delivery
        if verb == 'release':
            assert event.party is not None and event.on is not None
            store.release_shipment(ids, on=event.on, party=event.party, project=event.project, logged_by=event.logged_by, shipment_id=event.shipment_id,
                                   reason=event.reason, recipient=recipient)
            if delivery is not None:
                store.link_delivery(delivery, ids)
        else:
            for drive_id in ids:
                store.append_event(drive_id, event)
        return [(drive_id, None, after) for drive_id in ids]

    @staticmethod
    def _assign(store: DriveStore, drive_id: str, assignment: Assignment) -> list[Change]:
        store.add_assignment(drive_id, assignment)
        return [(drive_id, None, audit_json(assignment))]

    @staticmethod
    def _close(store: DriveStore, drive_id: str, number: int, held: Assignment, end: date, *, drop_return_date: bool = False) -> list[Change]:
        closed = store.end_assignment(drive_id, number, end)
        if drop_return_date:
            closed = store.drop_return_date(drive_id, number)
        return [(drive_id, audit_json(held), audit_json(closed))]
