"""LLM scanning of PM Notes lines into Concerns; ConcernStore stays deterministic."""
from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
import re
from typing import Any

from t6.concern import WEEK, ConcernStore, _close, _insert, _open_duplicate
from t6.ids import EntityKind, T6Id
from t6.person import AUTO_ACTOR
from t6.domain.studio_time import MANILA

MAX_ATTEMPTS = 3
_WAITING = re.compile(r"\b(?:wait(?:ing)?|await(?:ing)?|pending|hinihintay|hinintay|inaantay|inaabangan|antay|abang)\b", re.IGNORECASE)


class ConcernScanner:
    """Matches PM Notes lines to Concerns through a classifier and writes the result through the store's helpers."""

    def __init__(self, store: ConcernStore) -> None:
        self.store = store
        self.registry = store.registry

    def changes(self, project: str, since: str) -> list[dict[str, Any]]:
        start = datetime.fromisoformat(since).replace(tzinfo=MANILA).timestamp()
        with self.registry._transaction() as db:
            rows = db.execute('SELECT id, concern_id, action, quote, at FROM concern_pm_changes WHERE project_id = ? AND at >= ? ORDER BY id', (project, start)).fetchall()
            return [dict(zip(('id', 'concern_id', 'action', 'quote', 'at'), row)) for row in rows]

    @staticmethod
    def _pm_log(db: Any, project: str, concern: str, action: str, quote: str, now: float, before: Any) -> None:
        ConcernStore._log(db, project, concern, AUTO_ACTOR, action, now, before, {'quote': quote}, quote)
        db.execute('INSERT INTO concern_pm_changes (project_id, concern_id, action, quote, at) VALUES (?, ?, ?, ?, ?)', (project, concern, action, quote, now))

    def apply_pending(self, project: str) -> None:
        now = self.registry._clock()
        with self.registry._transaction() as db:
            rows = db.execute('SELECT p.concern_id, p.quote, p.revision, c.state, c.revision, c.manual_override FROM concern_pm_pending p JOIN concerns c ON c.id = p.concern_id WHERE c.project_id = ? AND p.due <= ?', (project, now)).fetchall()
            for concern, quote, revision, state, current, override in rows:
                if revision == current and not override and state != 'closed':
                    _close(db, concern, now)
                    self._pm_log(db, project, concern, 'concern_auto_resolve', quote, now, state)
                db.execute('DELETE FROM concern_pm_pending WHERE concern_id = ?', (concern,))

    def _rejected(self, project: str, as_of: str, text: str, db: Any = None) -> None:
        """Count a rejected answer for a line; after MAX_ATTEMPTS the line is recorded and not sent again."""
        if db is None:
            with self.registry._transaction() as own:
                self._rejected(project, as_of, text, own)
            return
        db.execute('INSERT INTO concern_pm_attempts VALUES (?, ?, ?, 1) ON CONFLICT (project_id, as_of, line) DO UPDATE SET attempts = attempts + 1', (project, as_of, text))
        if db.execute('SELECT attempts FROM concern_pm_attempts WHERE project_id = ? AND as_of = ? AND line = ?', (project, as_of, text)).fetchone()[0] >= MAX_ATTEMPTS:
            db.execute('INSERT OR IGNORE INTO concern_pm_lines VALUES (?, ?, ?)', (project, as_of, text))

    def process_notes(self, project: str, owner: str, lines: list[dict[str, str]], classify: Callable[[dict[str, Any]], Any]) -> dict[str, int]:
        """Match the dated lines to Concerns; returns how many Concerns were raised, continued and announced."""
        done = {'raised': 0, 'continued': 0, 'announced': 0}
        for line in sorted(lines, key=lambda item: item['as_of']):
            text, as_of = line['text'], line['as_of']
            try:
                date.fromisoformat(as_of)
            except ValueError:
                continue
            if not text.strip():
                continue
            with self.registry._transaction() as db:
                if not db.execute('SELECT 1 FROM projects WHERE id = ?', (project,)).fetchone() or not db.execute('SELECT 1 FROM persons WHERE id = ? AND active = 1', (owner,)).fetchone():
                    raise ValueError('Project and active coordinator are required')
                if db.execute('SELECT 1 FROM concern_pm_lines WHERE project_id = ? AND as_of = ? AND line = ?', (project, as_of, text)).fetchone():
                    continue
                # The line a Concern was raised from is not new evidence; the same words on a later date are.
                if db.execute('SELECT 1 FROM concerns WHERE project_id = ? AND anchor = ? AND as_of >= ?', (project, text, as_of)).fetchone():
                    continue
                rows = db.execute("SELECT id, anchor, as_of, state, revision, manual_override FROM concerns WHERE project_id = ? AND id NOT IN (SELECT concern_id FROM concern_date_rules)", (project,)).fetchall()
            offered = {r[0]: r for r in rows if as_of > r[2]}
            answer = classify({'line': text, 'as_of': as_of, 'concerns': [{'id': r[0], 'anchor': r[1], 'state': r[3], 'human_decision': bool(r[5])} for r in offered.values()]})
            if not isinstance(answer, dict):
                continue  # No usable answer (the LLM was unavailable): the line is tried again on the next scan.
            choice, quote, concern = answer.get('choice'), answer.get('quote'), answer.get('concern_id')
            waiting = answer.get('waiting_on')
            if choice not in ('new', 'nothing', 'continues', 'settles') or not isinstance(quote, str) or not quote or quote not in text:
                self._rejected(project, as_of, text)
                continue
            repeated = next((r[0] for r in offered.values() if r[1] == text), None)
            if choice == 'new' and repeated is not None:
                # The same words on a later date are fresh evidence for the Concern they opened, never a second one.
                choice, concern = 'continues', repeated
            if choice in ('continues', 'settles') and concern not in offered:
                self._rejected(project, as_of, text)
                continue
            if choice == 'new' and (not isinstance(waiting, str) or not waiting.strip() or waiting not in quote or not _WAITING.search(quote)):
                self._rejected(project, as_of, text)
                continue
            now = self.registry._clock()
            with self.registry._transaction() as db:
                if db.execute('SELECT 1 FROM concern_pm_lines WHERE project_id = ? AND as_of = ? AND line = ?', (project, as_of, text)).fetchone():
                    continue
                if choice in ('continues', 'settles'):
                    row = db.execute('SELECT state, revision, manual_override, as_of FROM concerns WHERE id = ? AND project_id = ?', (concern, project)).fetchone()
                    original = offered[concern]
                    if row and row[2]:
                        # A person's decision wins: there is nothing to do, so the line is never sent again.
                        db.execute('INSERT OR IGNORE INTO concern_pm_lines VALUES (?, ?, ?)', (project, as_of, text))
                        continue
                    if not row or row[1] != original[4] or as_of <= row[3]:
                        self._rejected(project, as_of, text, db)
                        continue
                if choice == 'new':
                    concern = str(T6Id.mint(EntityKind.CONCERN))
                    if _open_duplicate(db, project, quote):
                        self._rejected(project, as_of, text, db)
                        continue
                    _insert(db, concern, project, owner, waiting, quote, as_of, min(now, datetime.fromisoformat(as_of).replace(tzinfo=MANILA).timestamp()), now, 'pm_notes_scan')
                    assert isinstance(concern, str)
                    self._pm_log(db, project, concern, 'concern_auto_raise', quote, now, None)
                    done['raised'] += 1
                elif choice == 'continues':
                    assert isinstance(concern, str)
                    state, revision = row[0], row[1]
                    db.execute("UPDATE concerns SET as_of = ?, evidence_at = ?, state = CASE WHEN state = 'snoozed' THEN state ELSE 'open' END, revision = revision + 1 WHERE id = ?", (as_of, now, concern))
                    db.execute('DELETE FROM concern_pm_pending WHERE concern_id = ?', (concern,))
                    self._pm_log(db, project, concern, 'concern_auto_continue', quote, now, state)
                    done['continued'] += 1
                elif choice == 'settles' and row[0] != 'closed':
                    assert isinstance(concern, str)
                    db.execute('UPDATE concerns SET as_of = ?, revision = revision + 1 WHERE id = ?', (as_of, concern))
                    db.execute('INSERT OR REPLACE INTO concern_pm_pending VALUES (?, ?, ?, ?, ?)', (concern, as_of, quote, now + WEEK, row[1] + 1))
                    self._pm_log(db, project, concern, 'concern_auto_announce', quote, now, row[0])
                    done['announced'] += 1
                    from t6.worker.internal_queue import InternalJobQueue
                    InternalJobQueue(self.registry, clock=lambda: now).submit('main', 'concern_pm_close', f'{concern}:{row[1] + 1}', {'project': project}, run_at=now + WEEK, db=db)
                db.execute('INSERT INTO concern_pm_lines VALUES (?, ?, ?)', (project, as_of, text))
        return done
