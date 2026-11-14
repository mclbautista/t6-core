"""T6 identity and history for Job Orders."""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import date
from collections.abc import Iterable
from typing import Any

from t6.ids import EntityKind, T6Id
from t6.preview import PreviewTokens
from t6.registry.core import normalize_page_id
from t6.registry.sqlite import SqliteProjectRegistry, _intend_sync
from t6.verbs import Done, Outcome, Refused


TRANSITIONS = {
    'submit': {'Needs Intake Review': 'In progress', 'Pending': 'In progress'},
    'close': {'In progress': 'Done', 'Needs Completion Review': 'Done'},
    'block': {'In progress': 'In progress', 'Needs Completion Review': 'Needs Completion Review'},
    'unblock': {'In progress': 'In progress', 'Needs Completion Review': 'Needs Completion Review'},
    'delay': {'Needs Intake Review': 'Needs Intake Review', 'Pending': 'Pending', 'In progress': 'In progress', 'Needs Completion Review': 'Needs Completion Review'},
    'cancel': {'Needs Intake Review': 'Cancelled', 'Pending': 'Cancelled', 'In progress': 'Cancelled', 'Needs Completion Review': 'Cancelled'},
}


def _state(db: Any, jo_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    row = db.execute('SELECT status, blocked, block_reason, delayed_until, delay_reason FROM jo_state WHERE job_order_id = ?', (jo_id,)).fetchone()
    return dict(zip(('status', 'blocked', 'block_reason', 'delayed_until', 'delay_reason'), row)) if row else {'status': fields.get('status') or 'Needs Intake Review', 'blocked': False, 'block_reason': None, 'delayed_until': None, 'delay_reason': None}


class JobOrderStore:
    def __init__(self, registry: SqliteProjectRegistry, tokens: PreviewTokens) -> None:
        self.registry = registry
        self.tokens = tokens

    def get(self, ref: str) -> dict[str, Any] | None:
        with self.registry._transaction() as db:
            row = db.execute('SELECT id, project_id, telegram_key, assignee_id, fields FROM job_orders WHERE id = ? OR telegram_key = ?', (ref, ref)).fetchone()
            if row is None:
                row = db.execute("SELECT j.id, j.project_id, j.telegram_key, j.assignee_id, j.fields FROM job_orders j JOIN bindings b ON b.project_id = j.id WHERE b.system = 'notion' AND b.page_id = ?", (normalize_page_id(ref),)).fetchone()
            if row is None:
                return None
            bindings = db.execute("SELECT page_id, active FROM bindings WHERE system = 'notion' AND project_id = ? ORDER BY bound_at, page_id", (row[0],)).fetchall()
            notes = db.execute('SELECT text, actor, at FROM job_order_notes WHERE job_order_id = ? ORDER BY id', (row[0],)).fetchall()
            state = _state(db, row[0], json.loads(row[4]))
            history = db.execute("SELECT action, actor, at, before, after FROM audit WHERE project_id = ? AND action IN ('jo_submit', 'jo_close', 'jo_block', 'jo_unblock', 'jo_delay', 'jo_cancel') ORDER BY id", (row[0],)).fetchall()
            return {'state': state, 'history': [{'action': h[0], 'actor': h[1], 'at': h[2], 'before': json.loads(h[3]), 'after': json.loads(h[4])} for h in history], 'id': row[0], 'project_id': row[1], 'telegram_key': row[2], 'assignee_id': row[3], 'fields': json.loads(row[4]), 'bindings': [{'page_id': b[0], 'active': bool(b[1])} for b in bindings], 'notes': [{'text': n[0], 'actor': n[1], 'at': n[2]} for n in notes]}

    def migrate(self, orders: Iterable[Any], *, actor: str, apply: bool = False, digest: str | None = None) -> dict[str, Any]:
        rows = sorted(set((normalize_page_id(o.page_id), o.telegram_key or '', normalize_page_id(o.project_id or ''), getattr(o, 'notes', None) or '', getattr(o, 'status', None) or 'Needs Intake Review', tuple(sorted(normalize_page_id(p) for p in (getattr(o, 'assigned_ids', ()) or ())))) for o in orders))
        snapshot = json.dumps(rows, ensure_ascii=False)
        with self.registry._transaction() as db:
            entries: list[dict[str, Any]] = []
            observed_pages = {row[0] for row in rows}
            page_counts = {page: sum(1 for p, _, _, _, _, _ in rows if p == page) for page, _, _, _, _, _ in rows}
            key_counts = {key: sum(1 for _, k, _, _, _, _ in rows if k == key) for _, key, _, _, _, _ in rows if key}
            for page, key, project_page, initial_notes, initial_status, assigned_pages in rows:
                bound = db.execute("SELECT project_id FROM bindings WHERE system = 'notion' AND page_id = ?", (page,)).fetchone()
                keyed = db.execute('SELECT id, project_id, assignee_id FROM job_orders WHERE telegram_key = ?', (key,)).fetchone() if key else None
                existing = db.execute('SELECT id, telegram_key, project_id, assignee_id FROM job_orders WHERE id = ?', (bound[0],)).fetchone() if bound else None
                project = db.execute("SELECT b.project_id FROM bindings b JOIN projects p ON p.id = b.project_id WHERE b.system = 'notion' AND b.page_id = ?", (project_page,)).fetchone() if project_page else None
                if page_counts[page] > 1 or (bound and not existing) or (existing and key and existing[1] and existing[1] != key) or (existing and keyed and existing[0] != keyed[0]) or (project and keyed and keyed[1] and keyed[1] != project[0]) or (project and existing and existing[2] and existing[2] != project[0]) or (key and key_counts[key] > 1 and not keyed and not existing):
                    action, target = 'review', None
                elif existing:
                    action, target = 'keep', existing[0]
                elif keyed:
                    action, target = 'bind', keyed[0]
                else:
                    action, target = 'register', None
                active_page = db.execute("SELECT page_id FROM bindings WHERE system = 'notion' AND project_id = ? AND active = 1", (target,)).fetchone() if target else None
                activate = bool(action == 'bind' and (not active_page or active_page[0] not in observed_pages) and key_counts.get(key, 0) == 1)
                if action == 'bind' and active_page and active_page[0] not in observed_pages and key_counts.get(key, 0) > 1:
                    action, target = 'review', None
                assignee = None
                if len(assigned_pages) == 1:
                    assignee = db.execute("SELECT p.id FROM persons p JOIN bindings b ON b.project_id = p.id WHERE b.system = 'notion' AND b.page_id = ?", (assigned_pages[0],)).fetchone()
                fill_project = bool(project and ((existing and existing[2] is None) or (not existing and keyed and keyed[1] is None)))
                fill_assignee = bool(assignee and ((existing and existing[3] is None) or (not existing and keyed and keyed[2] is None)))
                if action == 'keep' and (fill_project or fill_assignee):
                    action = 'update'
                problem = ('Multiple or unbound assigned Contacts need review' if assigned_pages and assignee is None else
                           'Project page is not bound' if project_page and project is None else None)
                entries.append({'page_id': page, 'telegram_key': key or None, 'project_id': project[0] if project else None, 'action': action, 't6_id': target, 'initial_notes': initial_notes, 'initial_status': initial_status, 'assignee_id': assignee[0] if assignee else None, 'activate': activate, 'fill_project': fill_project, 'fill_assignee': fill_assignee, 'problem': problem})
            fingerprint = hashlib.sha256(json.dumps(entries, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if apply and digest != fingerprint:
                receipt = db.execute('SELECT snapshot, entries FROM jo_migration_receipts WHERE digest = ?', (digest,)).fetchone()
                if not receipt or receipt[0] != snapshot:
                    raise ValueError('Job Orders changed since preview; preview again')
                prior = json.loads(receipt[1])
                for old, current in zip(prior, entries):
                    if old['t6_id']:
                        if current['action'] != 'keep' or current['t6_id'] != old['t6_id']:
                            raise ValueError('Bindings changed since preview; preview again')
                    elif current['action'] != 'review':
                        raise ValueError('Bindings changed since preview; preview again')
                return {'digest': digest, 'entries': entries, 'review': [e for e in entries if e['action'] == 'review' or e['problem']]}
            if apply:
                for entry in entries:
                    if entry['action'] == 'register':
                        entry['t6_id'] = str(T6Id.mint(EntityKind.JOB_ORDER))
                        db.execute('INSERT INTO job_orders VALUES (?, ?, ?, ?, ?)', (entry['t6_id'], entry['project_id'], entry['telegram_key'], entry['assignee_id'], json.dumps({'notes': entry['initial_notes'], 'status': entry['initial_status']})))
                    if entry['action'] in ('bind', 'update'):
                        if entry['fill_project']:
                            db.execute('UPDATE job_orders SET project_id = ? WHERE id = ?', (entry['project_id'], entry['t6_id']))
                        if entry['fill_assignee']:
                            db.execute('UPDATE job_orders SET assignee_id = ? WHERE id = ?', (entry['assignee_id'], entry['t6_id']))
                    if entry['action'] in ('register', 'bind', 'update'):
                        now = self.registry._clock()
                        if entry['action'] != 'update':
                            if entry['activate']:
                                db.execute("UPDATE bindings SET active = 0 WHERE system = 'notion' AND project_id = ?", (entry['t6_id'],))
                            active = not db.execute("SELECT 1 FROM bindings WHERE system = 'notion' AND project_id = ? AND active = 1", (entry['t6_id'],)).fetchone()
                            db.execute("INSERT INTO bindings (system, page_id, project_id, bound_at, active) VALUES ('notion', ?, ?, ?, ?)", (entry['page_id'], entry['t6_id'], now, int(active)))
                        db.execute('INSERT INTO audit (project_id, action, actor, at, before, after) VALUES (?, ?, ?, ?, NULL, ?)', (entry['t6_id'], 'jo_migrated', actor, now, json.dumps({'page_id': entry['page_id']})))
                        revision = db.execute('SELECT COALESCE(MAX(revision), 0) + 1 FROM sync_intents WHERE project_id = ?', (entry['t6_id'],)).fetchone()[0]
                        _intend_sync(db, entry['t6_id'], revision, now)
                        db.execute("INSERT INTO jo_sync_intents (intent_id, operation, payload) VALUES (last_insert_rowid(), 'update', '{}')")
                        entry['action'] = {'register': 'registered', 'bind': 'bound', 'update': 'updated'}[entry['action']]
                db.execute('INSERT OR IGNORE INTO jo_migration_receipts VALUES (?, ?, ?)', (fingerprint, snapshot, json.dumps(entries)))
            return {'digest': fingerprint, 'entries': entries, 'review': [e for e in entries if e['action'] == 'review' or e['problem']]}


    def write(self, verb: str, values: dict[str, Any], *, actor: str,
              idempotency_key: str | None = None, confirm: str | None = None,
              notion_editor: str | None = None) -> Outcome:
        if verb not in ('raise', 'assign', 'reassign', 'note', *TRANSITIONS) or not actor.strip():
            return Refused(rule='input', reason='A valid verb and actor are required')
        if (idempotency_key is not None and not idempotency_key.strip()) or (confirm and not idempotency_key):
            return Refused(rule='idempotency_key', reason='A nonblank idempotency key is required')
        key = idempotency_key or secrets.token_hex(16)
        inputs = {**values, 'idempotency_key': key}
        fingerprint = json.dumps({'verb': verb, 'actor': actor, **inputs}, sort_keys=True, separators=(',', ':'))
        nonce = self.tokens.verify(confirm, f'jo {verb}', actor, inputs) if confirm else None
        if isinstance(nonce, Refused):
            return nonce
        with self.registry._transaction() as db:
            if nonce and db.execute('SELECT 1 FROM used_preview_tokens WHERE nonce = ?', (nonce,)).fetchone():
                return Refused(rule='token_used', reason='Preview token has already been used')
            previous = db.execute('SELECT fingerprint, result FROM verb_requests WHERE idempotency_key = ?', (key,)).fetchone()
            if previous and previous[0] != fingerprint:
                return Refused(rule='idempotency_key', reason='Idempotency key was used for a different request')
            if previous and nonce:
                db.execute('INSERT INTO used_preview_tokens VALUES (?)', (nonce,))
                result = json.loads(previous[1])
                if verb == 'raise':
                    bound = db.execute("SELECT page_id FROM bindings WHERE system = 'notion' AND project_id = ? AND active = 1", (result['jo_id'],)).fetchone()
                    if bound:
                        result['page_id'] = bound[0]
                return Done(result=result)
            if previous and nonce is None:
                return self.tokens.create(f'jo {verb}', actor, inputs, json.loads(previous[1]))
            if verb == 'raise':
                fields = values.get('fields')
                if not isinstance(fields, dict):
                    return Refused(rule='input', reason='Job Order fields are required')
                project_ref = fields.get('project_id')
                project = db.execute('SELECT id FROM projects WHERE id = ?', (project_ref,)).fetchone()
                if not project and isinstance(project_ref, str):
                    project = db.execute("SELECT p.id FROM projects p JOIN bindings b ON b.project_id = p.id WHERE b.system = 'notion' AND b.page_id = ?", (normalize_page_id(project_ref),)).fetchone()
                if not project:
                    return Refused(rule='project', reason='Project not found')
                telegram_key = fields.get('telegram_key') or None
                if not previous and telegram_key and db.execute('SELECT 1 FROM job_orders WHERE telegram_key = ?', (telegram_key,)).fetchone():
                    return Refused(rule='telegram_key', reason='Telegram Key is already held')
                assigned_pages = fields.get('assigned_ids') or []
                initial_assignee = None
                if isinstance(assigned_pages, (list, tuple)) and len(assigned_pages) == 1 and isinstance(assigned_pages[0], str):
                    initial_assignee = db.execute("SELECT p.id FROM persons p JOIN bindings b ON b.project_id = p.id WHERE b.system = 'notion' AND b.page_id = ?", (normalize_page_id(assigned_pages[0]),)).fetchone()
                jo_id, old_assignee = None, None
            else:
                ref = values.get('jo')
                if not isinstance(ref, str):
                    return Refused(rule='jo', reason='Job Order not found')
                row = db.execute('SELECT id, project_id, assignee_id, fields FROM job_orders WHERE id = ? OR telegram_key = ?', (ref, ref)).fetchone()
                if not row:
                    row = db.execute("SELECT j.id, j.project_id, j.assignee_id, j.fields FROM job_orders j JOIN bindings b ON b.project_id = j.id WHERE b.system = 'notion' AND b.page_id = ?", (normalize_page_id(ref),)).fetchone()
                if not row:
                    return Refused(rule='jo', reason='Job Order not found')
                jo_id, project_id, old_assignee, stored_fields = row
                if not project_id:
                    return Refused(rule='project', reason='Job Order has no bound Project')
                project = (project_id,)
                if verb in TRANSITIONS:
                    state = _state(db, jo_id, json.loads(stored_fields))
                    if state['status'] not in TRANSITIONS[verb]:
                        return Refused(rule='jo_transition', reason=f"Cannot {verb} a JO in {state['status']}")
                    if verb == 'block' and state['blocked']:
                        return Refused(rule='jo_blocked', reason='JO is already blocked')
                    if verb == 'unblock' and not state['blocked']:
                        return Refused(rule='jo_blocked', reason='JO is not blocked')
                    if verb in ('submit', 'close') and state['blocked']:
                        return Refused(rule='jo_blocked', reason='Unblock the JO first')
                    if verb in ('block', 'delay', 'cancel') and not str(values.get('reason') or '').strip():
                        return Refused(rule='reason', reason='A reason is required')
                    if verb == 'delay':
                        try:
                            date.fromisoformat(values.get('until', ''))
                        except (ValueError, TypeError):
                            return Refused(rule='until', reason='An ISO date is required')
                    new_state = dict(state)
                    new_state['status'] = TRANSITIONS[verb][state['status']]
                    if verb == 'block':
                        new_state.update(blocked=True, block_reason=values['reason'])
                    elif verb == 'unblock':
                        new_state.update(blocked=False, block_reason=None)
                    elif verb == 'delay':
                        new_state.update(delayed_until=values['until'], delay_reason=values['reason'])
                    elif verb == 'cancel':
                        new_state.update(blocked=False, block_reason=None)
                if verb in ('assign', 'reassign'):
                    person = values.get('person')
                    if not db.execute('SELECT 1 FROM persons WHERE id = ? AND active = 1', (person,)).fetchone():
                        return Refused(rule='person', reason='Active Person not found')
                    if not previous and person == old_assignee:
                        return Refused(rule='assignee', reason='Person is already assigned')
                    if verb == 'reassign' and not str(values.get('reason') or '').strip():
                        return Refused(rule='reason', reason='A reason is required')
                if verb == 'note' and not str(values.get('text') or '').strip():
                    return Refused(rule='note', reason='A note is required')
            plan = {'action': verb, **values, 'project_id': project[0], 'idempotency_key': key}
            if verb in TRANSITIONS:
                plan.update(before=state, after=new_state)
            if verb in ('assign', 'reassign'):
                plan['before_assignee'] = old_assignee
            if nonce is None:
                preview = self.tokens.create(f'jo {verb}', actor, inputs, plan)
                if verb in TRANSITIONS or verb in ('assign', 'reassign'):
                    preview_nonce = self.tokens.verify(preview.token, f'jo {verb}', actor, inputs)
                    assert isinstance(preview_nonce, str)
                    db.execute('INSERT INTO jo_state_previews VALUES (?, ?)', (preview_nonce, json.dumps(state if verb in TRANSITIONS else [jo_id, old_assignee], sort_keys=True)))
                return preview
            if verb in TRANSITIONS:
                observed = db.execute('SELECT state FROM jo_state_previews WHERE nonce = ?', (nonce,)).fetchone()
                if not observed or observed[0] != json.dumps(state, sort_keys=True):
                    return Refused(rule='jo_state_changed', reason='JO state changed since preview; preview again')
            if verb in ('assign', 'reassign'):
                observed = db.execute('SELECT state FROM jo_state_previews WHERE nonce = ?', (nonce,)).fetchone()
                if not observed or observed[0] != json.dumps([jo_id, old_assignee], sort_keys=True):
                    return Refused(rule='assignee_changed', reason='Assignee changed since preview; preview again')
            now = self.registry._clock()
            if verb == 'raise':
                jo_id = str(T6Id.mint(EntityKind.JOB_ORDER))
                db.execute('INSERT INTO job_orders VALUES (?, ?, ?, ?, ?)', (jo_id, project[0], telegram_key, initial_assignee[0] if initial_assignee else None, json.dumps(fields)))
            elif verb in ('assign', 'reassign'):
                db.execute('UPDATE job_orders SET assignee_id = ? WHERE id = ?', (values['person'], jo_id))
            elif verb in TRANSITIONS:
                db.execute('INSERT OR REPLACE INTO jo_state VALUES (?, ?, ?, ?, ?, ?)', (jo_id, new_state['status'], int(new_state['blocked']), new_state['block_reason'], new_state['delayed_until'], new_state['delay_reason']))
            else:
                db.execute('INSERT INTO job_order_notes (job_order_id, text, actor, at) VALUES (?, ?, ?, ?)', (jo_id, values['text'], actor, now))
            assert jo_id is not None
            revision = db.execute('SELECT COALESCE(MAX(revision), 0) + 1 FROM sync_intents WHERE project_id = ?', (jo_id,)).fetchone()[0]
            _intend_sync(db, jo_id, revision, now)
            intent_id = db.execute('SELECT last_insert_rowid()').fetchone()[0]
            db.execute('INSERT INTO jo_sync_intents (intent_id, operation, payload) VALUES (last_insert_rowid(), ?, ?)', ('create' if verb == 'raise' else 'update', json.dumps({'status': new_state['status']} if verb in TRANSITIONS else values)))
            cursor = db.execute('INSERT INTO audit (project_id, action, actor, at, before, after) VALUES (?, ?, ?, ?, ?, ?)', (jo_id, 'jo_' + verb, actor, now, None if verb == 'raise' else json.dumps(state if verb in TRANSITIONS else {'assignee_id': old_assignee}), json.dumps({'state': new_state, 'inputs': values} if verb in TRANSITIONS else values)))
            summary = {'raise': 'JO raised', 'assign': f"JO assigned to {values.get('person')}",
                       'reassign': f"JO reassigned to {values.get('person')}: {values.get('reason')}",
                       'note': f"JO note: {values.get('text')}", **{name: f"JO {name}: {values.get('reason') or ''}" for name in TRANSITIONS}}[verb]
            if notion_editor is not None:
                summary = f'JO Status set in Notion by {notion_editor}'
            db.execute('INSERT INTO activity (project_id, subject_id, at, actor, action, summary, audit_id) VALUES (?, ?, ?, ?, ?, ?, ?)', (project[0], jo_id, now, actor, 'jo_' + verb, summary, cursor.lastrowid))
            result = {**plan, 'jo_id': jo_id}
            if verb == 'raise':
                result['page_id'] = None
                result['sync_intent_id'] = intent_id
            db.execute('INSERT INTO used_preview_tokens VALUES (?)', (nonce,))
            db.execute('INSERT INTO verb_requests VALUES (?, ?, ?)', (key, fingerprint, json.dumps(result)))
            return Done(result=result)
