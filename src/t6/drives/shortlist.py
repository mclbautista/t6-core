"""Plain-code, direction-filtered candidates for a recording Item."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from t6.drives import CustodyKind, DriveStatus, Ref
from t6.drives.movements import active_movements
from t6.drives.sqlite import SqliteDriveStore, _event
from t6.lookup import Normalizers
from t6.lookup.scoring import wratio
from t6.notion.generated import ContactsRow, ProjectsRow
from t6.notion.source import NotionSource, as_source
from t6.registry.core import normalize_page_id


def guesses(db: sqlite3.Connection, store: SqliteDriveStore, text: str, direction: str,
            tags: list[str], source: NotionSource | None, sender: int) -> list[dict[str, Any]]:
    normal = Normalizers()
    wanted = normal.text(text)
    uses = {d.id: [a for a in store.list_assignments(d.id) if a.end is None] for d in store.list_drives()}
    names = {a.project: [a.project.text] for assignments in uses.values() for a in assignments if a.project and a.project.text}
    coordinated: dict[str, set[Ref]] = {}
    tagged: set[str] = set()
    poster = db.execute('SELECT id FROM persons WHERE telegram_id=? AND active=1', (sender,)).fetchone()
    holders = {normal.text(poster[0]), normal.text(f'telegram:{sender}')} if poster else {normal.text(f'telegram:{sender}')}
    if source is not None:
        notion = as_source(source)
        for person, page in db.execute("SELECT p.id, b.page_id FROM persons p JOIN bindings b ON b.project_id=p.id WHERE p.active=1 AND b.system='notion' AND b.active=1"):
            try:
                contact_row = ContactsRow.from_page(notion.retrieve_page(page, freshness='mirror'))
                handle = contact_row.telegram_handle
            except Exception:  # unavailable names never stop capture resolution
                continue
            if handle and normal.handle(handle) in {normal.handle(tag) for tag in tags}:
                tagged.add(person)
            if person in tagged or poster and person == poster[0]:
                holders.update(normal.text(value) for value in (person, page, contact_row.name, handle) if value)
    for project, code, rotation, page in db.execute("SELECT p.id, p.code, p.rotation, b.page_id FROM projects p LEFT JOIN bindings b ON b.project_id=p.id AND b.system='notion' AND b.active=1"):
        ref = Ref(page_id=project)
        forms = [code] if code else []
        if page and source is not None:
            notion = as_source(source)
            try:
                row = ProjectsRow.from_page(notion.retrieve_page(page, freshness='mirror'))
                if row.name:
                    forms.append(row.name)
                if rotation == 'in':
                    for contact in row.t6_post_coordinator:
                        bound = db.execute("SELECT project_id FROM bindings WHERE system='notion' AND page_id=?", (normalize_page_id(contact),)).fetchone()
                        if bound:
                            coordinated.setdefault(bound[0], set()).add(ref)
            except Exception:
                pass
        names[ref] = forms
    short_names: dict[str, list[tuple[str, int]]] = {}
    for name, drive, project, count in db.execute('SELECT text, drive_id, project, confirmations FROM drive_short_names'):
        short_names.setdefault(drive, []).append((name, count))
        if project:
            names.setdefault(Ref(**json.loads(project)), []).append(name)
    scores = {ref: max((wratio(wanted, normal.text(name)) for name in forms), default=0.0) for ref, forms in names.items()}
    strong = {ref for person in tagged if len(coordinated.get(person, set())) == 1 for ref in coordinated[person]}
    projects = {ref: 1.0 for ref in strong} if strong else {ref: score for ref, score in scores.items() if score >= 0.6}
    weak = {ref for person in tagged for ref in coordinated.get(person, set())}
    status = DriveStatus.RELEASED if direction == 'IN' else DriveStatus.IN_CUSTODY
    active = {m.id for m in active_movements(db)}
    ranked: list[tuple[float, bool, bool, float, dict[str, Any]]] = []
    for drive in store.list_drives():
        if store.status(drive.id) != status:
            continue
        project_score = max((projects.get(a.project, 0.0) for a in uses[drive.id] if a.project), default=0.0)
        label_score = max(wratio(wanted, normal.text(name)) for name in [drive.label or '', *[name for name, _ in short_names.get(drive.id, [])]])
        if projects and not project_score or not projects and label_score < 0.6:
            continue
        score = project_score if projects else label_score
        events = [_event(data) for data, movement in db.execute(
            'SELECT e.data, m.movement_id FROM drive_effective_events e LEFT JOIN drive_movement_events m ON m.event_id=e.id WHERE e.drive_id=? ORDER BY e.id DESC',
            (drive.id,)) if movement is None or movement in active]
        released = next((e for e in events if e.kind is CustodyKind.RELEASED), None)
        holder = bool(released and released.party and any(normal.text(value) in holders for value in (released.party.ref.page_id, released.party.ref.text) if value))
        reason = 'Coordinator tag selects Project' if strong else 'Similar Project name or code; open use' if projects else 'Similar Drive label'
        reason += '; direction fits'
        if holder:
            reason += '; last released to poster or tagged Person'
        ranked.append((score, any(a.project in weak for a in uses[drive.id]), holder, label_score,
                       {'drive': drive.id, 'confidence': min(score, 0.9), 'reason': reason, 'exact': False}))
    ranked.sort(key=lambda g: (-g[0], -g[1], -g[2], -g[3]))
    shortlist = ranked[:6]
    shared = [g for g in shortlist if (drive := store.get_drive(g[4]['drive'])) is not None
              and (text == drive.label or any(text == name for name, _ in short_names.get(drive.id, [])))]
    if len(shared) == 1:
        guess = shared[0][4]
        if any(name == text and count >= 3 for name, count in short_names.get(guess['drive'], [])) and db.execute(
                'SELECT 1 FROM drive_reference_photos WHERE drive_id=?', (guess['drive'],)).fetchone():
            guess.update(confidence=0.95, exact=True, reason='Confirmed exact match; unique in shortlist; direction fits; Reference photo')
    shortlist.sort(key=lambda g: not g[4]['exact'])
    return [g[4] for g in shortlist[:3]]
