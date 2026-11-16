"""`concern observe`: an app hands over the findings of one run; the API decides what is new, known or not seen."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date, datetime
import hashlib
import json
from typing import Any

from t6.concern import CHRONIC_DAYS, SOURCES, ConcernStore, _close, _insert
from t6.domain.studio_time import MANILA
from t6.person import AUTO_ACTOR
from t6.registry.sqlite import _audit, concern_key
from t6.verbs import Done, Outcome, Refused

OBSERVED = ('flag_rule', 'pm_notes_read', 'leave')
MAX_FINDINGS, MAX_UNAVAILABLE = 500, 100


class _DryRun(Exception):
    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result


def _concern_id(db: Any, project: str, key: str, run_date: str) -> str:
    """The ID a new Concern gets: derived from what it is, so a dry run names the Concerns the real run will make."""
    for attempt in range(1000):
        concern = 't6c_' + hashlib.sha256(f'{project}|{key}|{run_date}|{attempt}'.encode()).hexdigest()[:32]
        if not db.execute('SELECT 1 FROM concerns WHERE id = ?', (concern,)).fetchone():
            return concern
    raise RuntimeError('No free Concern ID')


def _previous_start(db: Any, run_date: str) -> float:
    """Midnight in Manila of the latest earlier run_date the audit rows know; with none, 7 days before this one."""
    earlier = [d for (after,) in db.execute("SELECT after FROM audit WHERE action = 'concern_observe'") if (d := json.loads(after).get('run_date', '')) < run_date]
    day = date.fromisoformat(max(earlier)) if earlier else date.fromordinal(date.fromisoformat(run_date).toordinal() - 7)
    return datetime(day.year, day.month, day.day, tzinfo=MANILA).timestamp()


def observe(store: ConcernStore, values: Mapping[str, Any], *, actor: str, coordinators: Callable[[str], set[str]], closes: bool = False) -> Outcome:
    registry = store.registry
    run_date, findings, unavailable = values.get('run_date'), values.get('findings'), values.get('unavailable') or []
    key, dry = values.get('idempotency_key'), bool(values.get('dry_run'))
    try:
        date.fromisoformat(run_date if isinstance(run_date, str) else '')
    except ValueError:
        return Refused(rule='input', reason='run_date must be an ISO date')
    assert isinstance(run_date, str)
    if not isinstance(key, str) or not key.strip():
        return Refused(rule='idempotency_key', reason='A nonblank idempotency key is required')
    if not isinstance(findings, list) or not isinstance(unavailable, list) or len(findings) > MAX_FINDINGS or len(unavailable) > MAX_UNAVAILABLE:
        return Refused(rule='input', reason=f'At most {MAX_FINDINGS} findings and {MAX_UNAVAILABLE} unavailable pairs')
    if any(f.get('source') not in OBSERVED for f in findings) or any(u.get('source') not in OBSERVED for u in unavailable):
        return Refused(rule='source', reason='Source must be one of ' + ', '.join(OBSERVED))
    fingerprint = json.dumps({'verb': 'concern observe', 'actor': actor, 'run_date': run_date, 'findings': findings, 'unavailable': unavailable}, sort_keys=True, separators=(',', ':'))

    def project_id(identifier: str) -> str | None:
        try:
            found = registry.get(identifier) or registry.get_by_page(identifier)
        except ValueError:
            return None
        return found.id if found else None

    resolved: dict[str, str | None] = {}
    for item in [*findings, *unavailable]:
        resolved.setdefault(item['project'], project_id(item['project']))
    coordinating: dict[str, set[str]] = {}
    accepted: list[dict[str, Any]] = []
    skipped = 0
    for finding in findings:
        project, text, owner = resolved[finding['project']], finding.get('text'), finding.get('owner')
        if project is None or not isinstance(text, str) or not text.strip():
            skipped += 1
            continue
        if owner is not None:
            if project not in coordinating:
                coordinating[project] = coordinators(project)
            with registry._transaction() as db:
                active = db.execute('SELECT 1 FROM persons WHERE id = ? AND active = 1', (owner,)).fetchone()
            if not active or owner not in coordinating[project]:
                skipped += 1
                continue
        accepted.append({**finding, 'project': project})
    missing = {(resolved[u['project']], u['source']) for u in unavailable}

    try:
        with registry._transaction() as db:
            previous = db.execute('SELECT fingerprint, result FROM verb_requests WHERE idempotency_key = ?', (key,)).fetchone() if not dry else None
            if previous:
                return Done(result=json.loads(previous[1])) if previous[0] == fingerprint else Refused(rule='idempotency_key', reason='Idempotency key was used for a different request')
            now = registry._clock()
            result = _apply(db, store, run_date, accepted, missing, skipped, closes, now)
            if dry:
                raise _DryRun(result)
            _audit(db, 'observe:' + run_date, 'concern_observe', actor, now, None, {'run_date': run_date, 'counts': result['counts']}, summary='Concern observe run')
            db.execute('INSERT INTO verb_requests VALUES (?, ?, ?)', (key, fingerprint, json.dumps(result)))
            return Done(result=result)
    except _DryRun as dry_run:
        return Done(result=dry_run.result)


def _apply(db: Any, store: ConcernStore, run_date: str, findings: list[dict[str, Any]], unavailable: set[tuple[str | None, str]], skipped: int, closes: bool, now: float) -> dict[str, Any]:
    counts = {'new': 0, 'known': 0, 'not_seen': 0, 'unavailable': 0, 'skipped': skipped, 'projects_with_both': 0, 'closed': 0}
    start = _previous_start(db, run_date)
    opened_at = datetime.fromisoformat(run_date).replace(tzinfo=MANILA).timestamp()
    seen: set[str] = set()
    for finding in findings:
        project, text, source = finding['project'], finding['text'], finding['source']
        key = concern_key(text)
        row = db.execute("SELECT id, source, state, revision FROM concerns WHERE project_id = ? AND text_key = ? AND state != 'closed'", (project, key)).fetchone()
        if row is None:
            concern = _concern_id(db, project, key, run_date)
            _insert(db, concern, project, finding.get('owner'), finding.get('waiting_on'), text, run_date, opened_at, now, source)
            store._schedule(db, concern, now, 1)
            store._log(db, project, concern, AUTO_ACTOR, 'concern_auto_raise', now, None, {'source': source, 'run_date': run_date}, f'Concern raised from the {source} findings')
            counts['new'] += 1
        else:
            concern, existing, state, revision = row
            counts['known'] += 1
            if existing in OBSERVED:  # a Concern of a person, the scan or a date rule is left alone
                label = source if SOURCES.index(source) < SOURCES.index(existing) else existing
                db.execute("UPDATE concerns SET text = ?, source = ?, evidence_at = ?, state = 'open', revision = revision + 1 WHERE id = ?", (text, label, now, concern))
                store._schedule(db, concern, now, revision + 1)
                if state != 'open':
                    store._log(db, project, concern, AUTO_ACTOR, 'concern_auto_continue', now, state, {'run_date': run_date}, 'Concern found again in the findings')
        seen.add(concern)
    for concern, project, source, override in db.execute("SELECT id, project_id, source, manual_override FROM concerns WHERE state != 'closed' AND source IN (?, ?, ?)", OBSERVED).fetchall():
        if concern in seen:
            continue
        if (project, source) in unavailable:
            counts['unavailable'] += 1
        else:
            counts['not_seen'] += 1
            if closes and not override:
                _close(db, concern, now)
                store._log(db, project, concern, AUTO_ACTOR, 'concern_auto_resolve', now, 'open', {'run_date': run_date}, 'Concern not found in the findings')
                counts['closed'] += 1
    scanned = {p for (p,) in db.execute("SELECT project_id FROM concerns WHERE state != 'closed' AND source = 'pm_notes_scan'")}
    counts['projects_with_both'] = len(scanned & {f['project'] for f in findings if f['source'] == 'pm_notes_read'})
    today = date.fromisoformat(run_date)
    projects: dict[str, dict[str, Any]] = {}
    for concern, project, text, source, opened, state in db.execute("SELECT id, project_id, COALESCE(NULLIF(text, ''), anchor), source, opened_at, state FROM concerns WHERE state != 'closed' ORDER BY opened_at, id").fetchall():
        on = datetime.fromtimestamp(opened, MANILA).date()
        age = max(0, (today - on).days)
        projects.setdefault(project, {'project_id': project, 'open': [], 'closed_since_last_run': []})['open'].append(
            {'id': concern, 'text': text, 'source': source, 'opened_on': on.isoformat(), 'age_days': age, 'weeks': age // 7, 'chronic': age > CHRONIC_DAYS, 'state': state, 'seen_this_run': concern in seen})
    for concern, project, text in db.execute("SELECT id, project_id, COALESCE(NULLIF(text, ''), anchor) FROM concerns WHERE state = 'closed' AND closed_at >= ? ORDER BY closed_at, id", (start,)).fetchall():
        projects.setdefault(project, {'project_id': project, 'open': [], 'closed_since_last_run': []})['closed_since_last_run'].append({'id': concern, 'text': text})
    return {'run_date': run_date, 'projects': [projects[p] for p in sorted(projects)], 'counts': counts}
