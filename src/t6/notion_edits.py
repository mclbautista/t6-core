"""Reconcile watched Notion values against durable T6 state."""
from __future__ import annotations

import json
from typing import Any

from t6.job_order import JobOrderStore
from t6.preview import PreviewTokens
from t6.person import People
from t6.registry import normalize_page_id
from t6.registry.sqlite import SqliteProjectRegistry, _open_review, _close_review
from t6.verbs import Done, Preview, Refused


class NotionEdits:
    def __init__(self, registry: SqliteProjectRegistry) -> None:
        self.registry = registry
        self.orders = JobOrderStore(registry, PreviewTokens('internal-notion-reconciliation'))

    def job_order(self, page: str, shown: dict[str, Any], changed: list[str], editor: str, event: str, *, editor_id: str | None = None) -> dict[str, Any]:
        page = normalize_page_id(page)
        jo = self.orders.get(page)
        if jo is None:
            return {'outcome': 'unbound'}
        project = jo['project_id']
        with self.registry._transaction() as db:
            person = db.execute("SELECT p.id FROM persons p JOIN bindings b ON b.project_id = p.id WHERE b.system = 'notion_user' AND b.page_id = ?", (normalize_page_id(editor_id),)).fetchone() if editor_id else None
            actor = str(person[0]) if person else editor
        restore: dict[str, str] = {}
        now = self.registry._clock()
        with self.registry._transaction() as db:
            if 't6_id' in changed:
                value = shown.get('t6_id')
                row = db.execute('SELECT value FROM notion_last_seen WHERE page_id = ? AND property = ?', (page, 't6_id')).fetchone()
                previous = json.loads(row[0]) if row else jo['id']
                if value != jo['id']:
                    restore['t6_id'] = jo['id']
                    if value != previous and project:
                        cursor = db.execute('INSERT INTO audit (project_id, action, actor, at, before, after) VALUES (?, ?, ?, ?, ?, ?)', (jo['id'], 'notion_identity_edit', actor, now, json.dumps({'t6_id': previous}), json.dumps({'t6_id': value})))
                        db.execute('INSERT INTO activity (project_id, subject_id, at, actor, action, summary, audit_id) VALUES (?, ?, ?, ?, ?, ?, ?)', (project, jo['id'], now, actor, 'notion_identity_edit', f'T6 ID edited in Notion by {editor}, put back', cursor.lastrowid))
                db.execute('INSERT INTO notion_last_seen VALUES (?, ?, ?) ON CONFLICT(page_id, property) DO UPDATE SET value=excluded.value', (page, 't6_id', json.dumps(value)))
            if 'status' not in changed:
                return {'outcome': 'ignored', 'restore': restore}
            status = shown.get('status')
            row = db.execute('SELECT value FROM notion_last_seen WHERE page_id = ? AND property = ?', (page, 'jo_status')).fetchone()
            old = json.loads(row[0]) if row else jo['state']['status']
            if status == old or status == jo['state']['status']:
                if status == jo['state']['status']:
                    _close_review(db, 'notion_state_edit', page, now)
                db.execute('INSERT INTO notion_last_seen VALUES (?, ?, ?) ON CONFLICT(page_id, property) DO UPDATE SET value=excluded.value', (page, 'jo_status', json.dumps(status)))
                return {'outcome': 'unchanged', 'restore': restore}
        verb = {'In progress': 'submit', 'Done': 'close', 'Cancelled': 'cancel'}.get(status) if isinstance(status, str) else None
        result: Any = Refused(rule='jo_transition', reason='No matching JO status verb')
        if verb:
            values = {'jo': jo['id']}
            key = 'notion:' + event + ':jo-status'
            result = People(self.registry, self.orders.tokens).authorize(actor, 'coordinator') if person else Refused(rule='permission', reason='Notion editor is not bound to a Person')
            if result is None:
                result = self.orders.write(verb, values, actor=actor, idempotency_key=key, notion_editor=editor)
            if isinstance(result, Preview):
                result = self.orders.write(verb, values, actor=actor, idempotency_key=key, confirm=result.token, notion_editor=editor)
        with self.registry._transaction() as db:
            if isinstance(result, Done):
                _close_review(db, 'notion_state_edit', page, now)
                outcome = 'accepted'
            else:
                reason = result.reason if isinstance(result, Refused) else 'Could not apply status'
                _close_review(db, 'notion_state_edit', page, now)
                _open_review(db, 'notion_state_edit', page, project, {'subject_id': jo['id'], 'property': 'Status', 'value': status, 'editor': editor, 'reason': reason}, now)
                if project:
                    cursor = db.execute('INSERT INTO audit (project_id, action, actor, at, before, after) VALUES (?, ?, ?, ?, ?, ?)', (jo['id'], 'notion_state_flagged', actor, now, json.dumps({'status': old}), json.dumps({'status': status, 'reason': reason})))
                    db.execute('INSERT INTO activity (project_id, subject_id, at, actor, action, summary, audit_id) VALUES (?, ?, ?, ?, ?, ?, ?)', (project, jo['id'], now, actor, 'notion_state_flagged', f'JO Status edited in Notion by {editor}, flagged: {reason}', cursor.lastrowid))
                outcome = 'flagged'
            db.execute('INSERT INTO notion_last_seen VALUES (?, ?, ?) ON CONFLICT(page_id, property) DO UPDATE SET value=excluded.value', (page, 'jo_status', json.dumps(status)))
        return {'outcome': outcome, 'restore': restore}

    def projection(self, page: str, values: dict[str, Any], *, record: bool, initialize: bool = False) -> dict[str, Any]:
        page = normalize_page_id(page)
        allowed: dict[str, bool] = {}
        with self.registry._transaction() as db:
            for name, value in values.items():
                row = db.execute('SELECT value FROM notion_last_seen WHERE page_id = ? AND property = ?', (page, name)).fetchone()
                allowed[name] = row is None or json.loads(row[0]) == value
                if name == 'jo_status' and db.execute("SELECT 1 FROM review_items WHERE kind = 'notion_state_edit' AND page_id = ? AND resolved_at IS NULL", (page,)).fetchone():
                    allowed[name] = False
                if record or (initialize and row is None):
                    db.execute('INSERT INTO notion_last_seen VALUES (?, ?, ?) ON CONFLICT(page_id, property) DO UPDATE SET value=excluded.value', (page, name, json.dumps(value)))
        return {'allowed': allowed}

    def project_status(self, page: str, status: str | None, editor: str, event: str, editor_id: str | None, field: str = "status") -> dict[str, Any]:
        from t6.project_writes import ProjectWrites
        page = normalize_page_id(page)
        project = self.registry.get_by_page(page)
        if project is None:
            return {'outcome': 'unbound'}
        now = self.registry._clock()
        property_name = 'Health' if field == 'health' else 'Status'
        cache_key = 'project_' + field
        review_kind = 'notion_project_' + field
        with self.registry._transaction() as db:
            person = db.execute("SELECT p.id FROM persons p JOIN bindings b ON b.project_id = p.id WHERE b.system = 'notion_user' AND b.page_id = ?", (normalize_page_id(editor_id),)).fetchone() if editor_id else None
            actor = str(person[0]) if person else editor
            row = db.execute('SELECT value FROM notion_last_seen WHERE page_id = ? AND property = ?', (page, cache_key)).fetchone()
            old = json.loads(row[0]) if row else None
            if row is not None and old == status:
                return {'outcome': 'unchanged'}
        normalized = ''.join(c for c in (status or '').lower() if c.isalnum())
        verb = ('resume' if normalized in ('inprogress', 'inprogresstvc', 'airing') else 'hold' if normalized in ('standby', 'archiving') else None) if field == 'status' else None
        result: Any = Refused(rule='project_' + field, reason=f'No matching Project {property_name} verb')
        if verb:
            writes = ProjectWrites(self.registry, PreviewTokens('internal-notion-reconciliation'))
            key = 'notion:' + event + ':project-status'
            result = People(self.registry, writes.tokens).authorize(actor, 'operator') if person else Refused(rule='permission', reason='Notion editor is not bound to a Person')
            if result is None:
                result = writes.run(verb, project.id, None, actor=actor, idempotency_key=key, notion_editor=editor)
            if isinstance(result, Preview):
                result = writes.run(verb, project.id, None, actor=actor, idempotency_key=key, confirm=result.token, notion_editor=editor)
        with self.registry._transaction() as db:
            if isinstance(result, Done):
                _close_review(db, review_kind, page, now)
                outcome = 'accepted'
            else:
                reason = result.reason if isinstance(result, Refused) else 'Could not apply status'
                _close_review(db, review_kind, page, now)
                _open_review(db, review_kind, page, project.id, {'property': property_name, 'value': status, 'editor': editor, 'reason': reason}, now)
                cursor = db.execute('INSERT INTO audit (project_id, action, actor, at, before, after) VALUES (?, ?, ?, ?, ?, ?)', (project.id, 'notion_state_flagged', actor, now, json.dumps({'status': old}), json.dumps({'status': status, 'reason': reason})))
                db.execute('INSERT INTO activity (project_id, at, actor, action, summary, audit_id) VALUES (?, ?, ?, ?, ?, ?)', (project.id, now, actor, 'notion_state_flagged', f'Project {property_name} edited in Notion by {editor}, flagged: {reason}', cursor.lastrowid))
                outcome = 'flagged'
            db.execute('INSERT INTO notion_last_seen VALUES (?, ?, ?) ON CONFLICT(page_id, property) DO UPDATE SET value=excluded.value', (page, cache_key, json.dumps(status)))
        return {'outcome': outcome}
