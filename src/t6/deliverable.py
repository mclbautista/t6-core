"""T6 Deliverable identity and previewed definition."""
from __future__ import annotations

import json
import builtins
import secrets
from datetime import date, datetime
from t6.domain.studio_time import MANILA
from typing import Any

from t6.ids import EntityKind, T6Id
from t6.preview import PreviewTokens
from t6.registry.core import normalize_page_id, normalize_code
from t6.registry.sqlite import SqliteProjectRegistry, _intend_sync
from t6.verbs import Done, Outcome, Refused


MILESTONES = ("ingested", "synced", "piclock", "conform done", "color delivered",
              "VFX approved", "printmaster in", "master approved", "QC passed", "delivered", "archived")
# Tracks are separate from the main pipeline: completing one never advances another.
FILM_PIPELINE = ("ingested", "synced", "piclock", "conform done", "master approved", "QC passed", "delivered", "archived")
FILM_TRACKS = {"color": "color delivered", "VFX": "VFX approved", "GFX/titles": None,
               "sound": "printmaster in", "music": None, "captions": None}


def film_stage(facts: list[dict[str, Any]]) -> dict[str, Any]:
    recorded = {fact['milestone'] for fact in facts}
    return {"template": "film", "position": next((m for m in reversed(FILM_PIPELINE) if m in recorded), None),
            "tracks": {track: "complete" if milestone and milestone in recorded else "pending"
                       for track, milestone in FILM_TRACKS.items()}}


class DeliverableStore:
    def __init__(self, registry: SqliteProjectRegistry, tokens: PreviewTokens) -> None:
        self.registry, self.tokens = registry, tokens

    def get(self, ref: str) -> dict[str, Any] | None:
        with self.registry._transaction() as db:
            row = db.execute("SELECT id, project_id, code, episode, due, air FROM deliverables WHERE id = ? OR id = (SELECT project_id FROM bindings WHERE system = 'notion' AND page_id = ?)", (ref, normalize_page_id(ref))).fetchone()
            if not row:
                return None
            result = dict(zip(('id', 'project_id', 'code', 'episode', 'due', 'air'), row))
            result['t6_id'] = row[0]
            result['bindings'] = [dict(page_id=b[0], active=bool(b[1])) for b in db.execute("SELECT page_id, active FROM bindings WHERE system = 'notion' AND project_id = ? ORDER BY bound_at, page_id", (row[0],))]
            result['milestones'] = [dict(zip(('id', 'milestone', 'on', 'actor', 'recorded_at'), fact))
                                    for fact in db.execute('SELECT id, milestone, occurred_on, actor, recorded_at FROM milestones WHERE deliverable_id=? ORDER BY rowid', (row[0],))]
            result['stage'] = film_stage(result['milestones'])
            return result

    def list(self, project: str) -> list[dict[str, Any]]:
        with self.registry._transaction() as db:
            project_id = self._project(db, project)
            ids = [r[0] for r in db.execute('SELECT id FROM deliverables WHERE project_id = ? ORDER BY code, id', (project_id,))]
        return [item for identifier in ids if (item := self.get(identifier)) is not None]

    @staticmethod
    def _project(db: Any, ref: str) -> str | None:
        row = db.execute("SELECT id FROM projects WHERE id = ? OR id = (SELECT project_id FROM bindings WHERE system = 'notion' AND page_id = ?)", (ref, normalize_page_id(ref))).fetchone()
        if not row:
            _, form = normalize_code(ref)
            row = db.execute("SELECT id FROM projects WHERE code_form = ? AND rotation = 'in'", (form,)).fetchone()
        return row[0] if row else None

    def define(self, values: dict[str, Any], *, actor: str, idempotency_key: str | None = None, confirm: str | None = None) -> Outcome:
        if not actor.strip() or not str(values.get('code') or '').strip() or not str(values.get('project') or '').strip():
            return Refused(rule='input', reason='Project, code and actor are required')
        if (idempotency_key is not None and not idempotency_key.strip()) or (confirm and not idempotency_key):
            return Refused(rule='idempotency_key', reason='A nonblank idempotency key is required')
        for field in ('due', 'air'):
            if values.get(field):
                try:
                    date.fromisoformat(values[field])
                except (TypeError, ValueError):
                    return Refused(rule=field, reason='An ISO date is required')
        key = idempotency_key or secrets.token_hex(16)
        inputs = {**values, 'idempotency_key': key}
        fingerprint = json.dumps({'verb': 'deliverable define', 'actor': actor, **inputs}, sort_keys=True, separators=(',', ':'))
        nonce = self.tokens.verify(confirm, 'deliverable define', actor, inputs) if confirm else None
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
                return Done(result=json.loads(previous[1]))
            if previous:
                return self.tokens.create('deliverable define', actor, inputs, json.loads(previous[1]))
            project_id = self._project(db, values['project'])
            if not project_id:
                return Refused(rule='project', reason='Project not found')
            plan = {'action': 'define', **values, 'project_id': project_id, 'idempotency_key': key}
            if nonce is None:
                preview = self.tokens.create('deliverable define', actor, inputs, plan)
                preview_nonce = self.tokens.verify(preview.token, 'deliverable define', actor, inputs)
                assert isinstance(preview_nonce, str)
                db.execute('INSERT INTO jo_state_previews VALUES (?, ?)', (preview_nonce, project_id))
                return preview
            observed = db.execute('SELECT state FROM jo_state_previews WHERE nonce = ?', (nonce,)).fetchone()
            if not observed or observed[0] != project_id:
                return Refused(rule='project_changed', reason='Project changed since preview; preview again')
            identifier = str(T6Id.mint(EntityKind.DELIVERABLE))
            now = self.registry._clock()
            db.execute('INSERT INTO deliverables VALUES (?, ?, ?, ?, ?, ?)', (identifier, project_id, values['code'], values.get('episode'), values.get('due'), values.get('air')))
            result = {**plan, 'deliverable_id': identifier}
            cursor = db.execute('INSERT INTO audit (project_id, action, actor, at, before, after) VALUES (?, ?, ?, ?, NULL, ?)', (identifier, 'deliverable_define', actor, now, json.dumps(values)))
            db.execute('INSERT INTO activity (project_id, subject_id, at, actor, action, summary, audit_id) VALUES (?, ?, ?, ?, ?, ?, ?)', (project_id, identifier, now, actor, 'deliverable_define', 'deliverable defined', cursor.lastrowid))
            _intend_sync(db, identifier, 1, now)
            db.execute('INSERT INTO deliverable_sync_intents (intent_id) VALUES (last_insert_rowid())')
            db.execute('INSERT INTO used_preview_tokens VALUES (?)', (nonce,))
            db.execute('INSERT INTO verb_requests VALUES (?, ?, ?)', (key, fingerprint, json.dumps(result)))
            return Done(result=result)

    def record_milestone(self, values: dict[str, Any], *, actor: str, idempotency_key: str | None = None, confirm: str | None = None) -> Outcome:
        name = values.get('milestone')
        if not actor.strip() or not values.get('deliverable'):
            return Refused(rule='input', reason='Deliverable and actor are required')
        if name not in MILESTONES:
            return Refused(rule='milestone', reason='Unknown milestone')
        if (idempotency_key is not None and not idempotency_key.strip()) or (confirm and not idempotency_key):
            return Refused(rule='idempotency_key', reason='A nonblank idempotency key is required')
        if values.get('on'):
            try:
                date.fromisoformat(values['on'])
            except (TypeError, ValueError):
                return Refused(rule='on', reason='An ISO date is required')
        key = idempotency_key or secrets.token_hex(16)
        inputs = {**values, 'idempotency_key': key}
        verb = 'deliverable milestone record'
        fingerprint = json.dumps({'verb': verb, 'actor': actor, **inputs}, sort_keys=True, separators=(',', ':'))
        nonce = self.tokens.verify(confirm, verb, actor, inputs) if confirm else None
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
                return self.tokens.create(verb, actor, inputs, json.loads(previous[1]))
            ref = values['deliverable']
            row = db.execute("SELECT id, project_id FROM deliverables WHERE id=? OR id=(SELECT project_id FROM bindings WHERE system='notion' AND page_id=?)", (ref, normalize_page_id(ref))).fetchone()
            if not row:
                return Refused(rule='deliverable', reason='Deliverable not found')
            if nonce is None:
                plan = {'deliverable_id': row[0], 'project_id': row[1], 'milestone': name,
                        'on': values.get('on') or datetime.fromtimestamp(self.registry._clock(), MANILA).date().isoformat(), 'idempotency_key': key}
                preview = self.tokens.create(verb, actor, inputs, plan)
                preview_nonce = self.tokens.verify(preview.token, verb, actor, inputs)
                assert isinstance(preview_nonce, str)
                db.execute('INSERT INTO jo_state_previews VALUES (?, ?)', (preview_nonce, json.dumps(plan)))
                return preview
            observed = db.execute('SELECT state FROM jo_state_previews WHERE nonce=?', (nonce,)).fetchone()
            if not observed:
                return Refused(rule='preview', reason='Preview not found')
            plan = json.loads(observed[0])
            if plan['deliverable_id'] != row[0]:
                return Refused(rule='deliverable_changed', reason='Deliverable changed since preview')
            now = self.registry._clock()
            identifier = str(T6Id.mint(EntityKind.MILESTONE))
            db.execute('INSERT INTO milestones VALUES (?, ?, ?, ?, ?, ?)', (identifier, row[0], name, plan['on'], actor, now))
            result = {**plan, 'milestone_id': identifier}
            cursor = db.execute('INSERT INTO audit (project_id, action, actor, at, before, after) VALUES (?, ?, ?, ?, NULL, ?)', (row[0], 'milestone_record', actor, now, json.dumps(result)))
            db.execute('INSERT INTO activity (project_id, subject_id, at, actor, action, summary, audit_id) VALUES (?, ?, ?, ?, ?, ?, ?)', (row[1], row[0], now, actor, 'milestone_record', f'milestone recorded: {name}', cursor.lastrowid))
            db.execute('INSERT INTO used_preview_tokens VALUES (?)', (nonce,))
            db.execute('INSERT INTO verb_requests VALUES (?, ?, ?)', (key, fingerprint, json.dumps(result)))
            return Done(result=result)

    def rebind(self, values: dict[str, Any], *, actor: str, idempotency_key: str | None = None,
               confirm: str | None = None) -> Outcome:
        identifier, raw_page = values.get('deliverable_id'), values.get('page_id')
        if not actor.strip() or not isinstance(identifier, str) or not isinstance(raw_page, str) or not raw_page.strip():
            return Refused(rule='input', reason='Deliverable and page ID are required')
        if (idempotency_key is not None and not idempotency_key.strip()) or (confirm and not idempotency_key):
            return Refused(rule='idempotency_key', reason='A nonblank idempotency key is required')
        key = idempotency_key or secrets.token_hex(16)
        inputs = {**values, 'idempotency_key': key}
        fingerprint = json.dumps({'verb': 'deliverable rebind', 'actor': actor, **inputs}, sort_keys=True, separators=(',', ':'))
        nonce = self.tokens.verify(confirm, 'deliverable rebind', actor, inputs) if confirm else None
        if isinstance(nonce, Refused):
            return nonce
        page = normalize_page_id(raw_page)
        with self.registry._transaction() as db:
            if nonce and db.execute('SELECT 1 FROM used_preview_tokens WHERE nonce=?', (nonce,)).fetchone():
                return Refused(rule='token_used', reason='Preview token has already been used')
            previous = db.execute('SELECT fingerprint, result FROM verb_requests WHERE idempotency_key=?', (key,)).fetchone()
            if previous and previous[0] != fingerprint:
                return Refused(rule='idempotency_key', reason='Idempotency key was used for a different request')
            if previous and nonce:
                db.execute('INSERT INTO used_preview_tokens VALUES (?)', (nonce,))
                return Done(result=json.loads(previous[1]))
            if previous:
                return self.tokens.create('deliverable rebind', actor, inputs, json.loads(previous[1]))
            row = db.execute('SELECT project_id FROM deliverables WHERE id=?', (identifier,)).fetchone()
            if not row:
                return Refused(rule='deliverable', reason='Deliverable not found')
            bound = db.execute("SELECT project_id FROM bindings WHERE system='notion' AND page_id=?", (page,)).fetchone()
            if bound and bound[0] != identifier:
                return Refused(rule='binding', reason=f'Page is already bound to {bound[0]}')
            current = db.execute("SELECT page_id FROM bindings WHERE system='notion' AND project_id=? AND active=1", (identifier,)).fetchone()
            old_page = current[0] if current else None
            plan = {'action': 'rebind', 'deliverable_id': identifier, 'project_id': row[0], 'page_id': page, 'idempotency_key': key}
            if nonce is None:
                preview = self.tokens.create('deliverable rebind', actor, inputs, plan)
                preview_nonce = self.tokens.verify(preview.token, 'deliverable rebind', actor, inputs)
                assert isinstance(preview_nonce, str)
                db.execute('INSERT INTO jo_state_previews VALUES (?, ?)', (preview_nonce, json.dumps(old_page)))
                return preview
            observed = db.execute('SELECT state FROM jo_state_previews WHERE nonce=?', (nonce,)).fetchone()
            if not observed or observed[0] != json.dumps(old_page):
                return Refused(rule='binding_changed', reason='Binding changed since preview; preview again')
            now = self.registry._clock()
            db.execute("UPDATE bindings SET active=0 WHERE system='notion' AND project_id=?", (identifier,))
            db.execute("INSERT INTO bindings (system, page_id, project_id, bound_at, active) VALUES ('notion', ?, ?, ?, 1) ON CONFLICT(system, page_id) DO UPDATE SET active=1", (page, identifier, now))
            cursor = db.execute('INSERT INTO audit (project_id, action, actor, at, before, after) VALUES (?, ?, ?, ?, ?, ?)', (identifier, 'deliverable_rebind', actor, now, json.dumps({'page_id': old_page}), json.dumps({'page_id': page})))
            db.execute('INSERT INTO activity (project_id, subject_id, at, actor, action, summary, audit_id) VALUES (?, ?, ?, ?, ?, ?, ?)', (row[0], identifier, now, actor, 'deliverable_rebind', 'deliverable rebound', cursor.lastrowid))
            revision = db.execute('SELECT COALESCE(MAX(revision), 0) + 1 FROM sync_intents WHERE project_id=?', (identifier,)).fetchone()[0]
            _intend_sync(db, identifier, revision, now)
            db.execute('INSERT INTO deliverable_sync_intents (intent_id) VALUES (last_insert_rowid())')
            db.execute('INSERT INTO used_preview_tokens VALUES (?)', (nonce,))
            db.execute('INSERT INTO verb_requests VALUES (?, ?, ?)', (key, fingerprint, json.dumps(plan)))
            return Done(result=plan)

    def migrate(self, rows: builtins.list[dict[str, Any]], *, actor: str, apply: bool = False, digest: str | None = None) -> dict[str, Any]:
        import hashlib
        snapshot = json.dumps(sorted(({k: v for k, v in row.items() if k != 't6_id'} for row in rows), key=lambda r: r['page_id']), sort_keys=True, ensure_ascii=False)
        with self.registry._transaction() as db:
            entries: builtins.list[dict[str, Any]] = []
            observed = {normalize_page_id(r['page_id']) for r in rows}
            page_counts = {page: sum(normalize_page_id(r['page_id']) == page for r in rows) for page in observed}
            projected_counts = {r['t6_id']: sum(other.get('t6_id') == r['t6_id'] for other in rows) for r in rows if r.get('t6_id')}
            for row in sorted(rows, key=lambda r: r['page_id']):
                page = normalize_page_id(row['page_id'])
                binding = db.execute("SELECT project_id FROM bindings WHERE system='notion' AND page_id=?", (page,)).fetchone()
                projected = row.get('t6_id')
                existing = db.execute('SELECT id, project_id FROM deliverables WHERE id=?', (binding[0] if binding else None,)).fetchone()
                project_binding = db.execute("SELECT p.id FROM projects p JOIN bindings b ON b.project_id=p.id WHERE b.system='notion' AND b.page_id=?", (normalize_page_id(row.get('project_page') or ''),)).fetchone()
                project = project_binding[0] if project_binding else None
                if (page_counts[page] > 1 or projected and projected_counts[projected] > 1 or binding and not existing or projected and (not existing or projected != existing[0]) or not project or not row.get('code') or existing and existing[1] != project):
                    action, problem = 'review', 'Unplaceable or conflicting page'
                elif binding:
                    action, problem = 'keep', None
                else:
                    action, problem = 'register', None
                entries.append({'page_id': page, 'project_id': project, 'code': row.get('code'), 'episode': row.get('episode'), 'due': row.get('due'), 'air': row.get('air'), 'action': action, 't6_id': existing[0] if existing else None, 'problem': problem})
            fingerprint = hashlib.sha256(json.dumps(entries, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if apply and digest != fingerprint:
                receipt = db.execute('SELECT snapshot, entries FROM deliverable_migration_receipts WHERE digest=?', (digest,)).fetchone()
                if not receipt or receipt[0] != snapshot:
                    raise ValueError('Deliverables changed since preview; preview again')
                prior = json.loads(receipt[1])
                if any((old['t6_id'] and (new['action'] != 'keep' or old['t6_id'] != new['t6_id'])) or (not old['t6_id'] and new['action'] != 'review') for old, new in zip(prior, entries)):
                    raise ValueError('Bindings changed since preview; preview again')
                return {'digest': digest, 'entries': entries, 'review': [e for e in entries if e['problem']]}
            if apply:
                for entry in entries:
                    action = entry['action']
                    if action != 'register':
                        continue
                    identifier = entry['t6_id'] or str(T6Id.mint(EntityKind.DELIVERABLE))
                    now = self.registry._clock()
                    db.execute('INSERT INTO deliverables VALUES (?, ?, ?, ?, ?, ?)', (identifier, entry['project_id'], entry['code'], entry['episode'], entry['due'], entry['air']))
                    db.execute("INSERT INTO bindings (system, page_id, project_id, bound_at, active) VALUES ('notion', ?, ?, ?, 1)", (entry['page_id'], identifier, now))
                    cursor = db.execute('INSERT INTO audit (project_id, action, actor, at, before, after) VALUES (?, ?, ?, ?, NULL, ?)', (identifier, 'deliverable_migrated', actor, now, json.dumps({'page_id': entry['page_id']})))
                    db.execute('INSERT INTO activity (project_id, subject_id, at, actor, action, summary, audit_id) VALUES (?, ?, ?, ?, ?, ?, ?)', (entry['project_id'], identifier, now, actor, 'deliverable_migrated', 'deliverable migrated', cursor.lastrowid))
                    _intend_sync(db, identifier, 1, now)
                    db.execute('INSERT INTO deliverable_sync_intents (intent_id) VALUES (last_insert_rowid())')
                    entry.update(action='registered', t6_id=identifier)
                db.execute('INSERT OR IGNORE INTO deliverable_migration_receipts VALUES (?, ?, ?)', (fingerprint, snapshot, json.dumps(entries)))
            return {'digest': fingerprint, 'entries': entries, 'review': [e for e in entries if e['problem']]}
