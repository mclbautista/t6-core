"""Concern records, human decisions, and silence timers."""
from __future__ import annotations

from datetime import date, datetime
from collections.abc import Callable, Mapping
import json
import re
import secrets
import statistics
from typing import Any

from t6.ids import EntityKind, T6Id
from t6.person import AUTO_ACTOR, People
from t6.preview import PreviewTokens
from t6.registry.sqlite import SqliteProjectRegistry, concern_key
from t6.verbs import Done, Outcome, Refused
from t6.domain.studio_time import MANILA

WEEK = 7 * 24 * 60 * 60
DAY = 24 * 60 * 60
CHRONIC_DAYS = 28
SOURCES = ('person', 'pm_notes_scan', 'date_rule', 'flag_rule', 'pm_notes_read', 'leave')
_FIELDS = ('id', 'project_id', 'owner_id', 'waiting_on', 'text', 'source', 'anchor', 'as_of', 'opened_at', 'evidence_at', 'state', 'settles_id', 'closed_at')
# A row the previous release wrote has no text of its own: the line it was raised from is the text.
_SELECT = "SELECT id, project_id, owner_id, waiting_on, COALESCE(NULLIF(text, ''), anchor), source, anchor, as_of, opened_at, evidence_at, state, settles_id, closed_at FROM concerns"
MAX_ATTEMPTS = 3
_WAITING = re.compile(r"\b(?:wait(?:ing)?|await(?:ing)?|pending|hinihintay|hinintay|inaantay|inaabangan|antay|abang)\b", re.IGNORECASE)


PROJECT_DATES = ('playdate', 'submission_deadline', 'archival_deadline')

def settle_linked(db: Any, settles_id: str, now: float) -> None:
    """Close eligible linked Concerns in the settling event's transaction."""
    rows = db.execute("SELECT id, project_id, state FROM concerns WHERE settles_id = ? AND manual_override = 0 AND state != 'closed'", (settles_id,)).fetchall()
    for concern, _, _ in rows:
        settle_concern(db, concern, now, settles_id)


def _midnight(day: str) -> float:
    return datetime.fromisoformat(day).replace(tzinfo=MANILA).timestamp()


def _close(db: Any, concern: str, now: float) -> None:
    db.execute("UPDATE concerns SET state = 'closed', closed_at = ?, revision = revision + 1 WHERE id = ?", (now, concern))


def _open_duplicate(db: Any, project: str, text: str) -> bool:
    """Whether a Concern that is not closed already carries this text in the Project."""
    return db.execute("SELECT 1 FROM concerns WHERE project_id = ? AND text_key = ? AND state != 'closed'", (project, concern_key(text))).fetchone() is not None


def _insert(db: Any, concern: str, project: str, owner: str | None, waiting_on: str | None, anchor: str, as_of: str, opened_at: float, evidence_at: float,
            source: str, text: str | None = None, settles: str | None = None) -> None:
    text = text or anchor
    db.execute("INSERT INTO concerns (id, project_id, owner_id, waiting_on, anchor, as_of, opened_at, evidence_at, state, settles_id, text, source, text_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)",
               (concern, project, owner, waiting_on or '', anchor, as_of, opened_at, evidence_at, settles, text, source, concern_key(text)))


def settle_concern(db: Any, concern: str, now: float, evidence: str) -> None:
    row = db.execute("SELECT project_id, state FROM concerns WHERE id = ? AND manual_override = 0 AND state != 'closed'", (concern,)).fetchone()
    if row:
        _close(db, concern, now)
        ConcernStore._log(db, row[0], concern, AUTO_ACTOR, 'concern_auto_resolve', now, row[1], {'evidence': evidence}, f'Concern resolved by {evidence}')


class ConcernStore:
    def __init__(self, registry: SqliteProjectRegistry, tokens: PreviewTokens) -> None:
        self.registry, self.tokens = registry, tokens

    @staticmethod
    def _record(row: Any, now: float) -> dict[str, Any]:
        record = dict(zip(_FIELDS, row))
        record['age_seconds'] = max(0, now - record['opened_at'])
        return record

    def get(self, concern: str) -> dict[str, Any] | None:
        with self.registry._transaction() as db:
            row = db.execute(_SELECT + ' WHERE id = ?', (concern,)).fetchone()
            if not row:
                return None
            record = self._record(row, self.registry._clock())
            record['notes'] = [dict(zip(('text', 'actor', 'at', 'message_id'), n)) for n in db.execute('SELECT text, actor, at, message_id FROM concern_notes WHERE concern_id = ? ORDER BY id', (concern,))]
            return record

    def open_concerns(self, project: str | None = None) -> list[dict[str, Any]]:
        """Every Concern that is not closed, oldest first."""
        now = self.registry._clock()
        with self.registry._transaction() as db:
            rows = db.execute(_SELECT + " WHERE state != 'closed' AND (? IS NULL OR project_id = ?) ORDER BY opened_at, id", (project, project)).fetchall()
        records = [self._record(row, now) for row in rows]
        for record in records:
            del record['closed_at']
            record['chronic'] = record['age_seconds'] > CHRONIC_DAYS * DAY
        return records

    def raise_at_psr(self, project: str | None = None) -> list[dict[str, Any]]:
        """Concerns that went quiet (no new evidence for 2 weeks). Age alone never lists one; expired ones are shown elsewhere."""
        now = self.registry._clock()
        with self.registry._transaction() as db:
            rows = db.execute("SELECT id, project_id, owner_id, waiting_on, opened_at FROM concerns WHERE state = 'quiet' AND (? IS NULL OR project_id = ?) ORDER BY opened_at, id", (project, project)).fetchall()
        return [{'id': row[0], 'project_id': row[1], 'owner_id': row[2], 'waiting_on': row[3], 'age_seconds': max(0, now - row[4]), 'label': 'no news for 2 weeks'} for row in rows]

    def counts(self, since: str | None = None, until: str | None = None, project: str | None = None) -> Outcome:
        """Counts for each run date the brief listed Concerns on: no text, no names."""
        try:
            for bound in (since, until):
                if bound is not None:
                    date.fromisoformat(bound)
        except ValueError:
            return Refused(rule='input', reason='since and until must be ISO dates')
        found = None if project is None else self.registry.get(project) or self.registry.get_by_page(project)
        if project is not None and found is None:
            return Refused(rule='project', reason='Project not found')
        scope = None if found is None else found.id
        with self.registry._transaction() as db:
            dates = [d for (d,) in db.execute('SELECT DISTINCT run_date FROM concern_listings ORDER BY run_date')]
            before = dict(zip(dates[1:], dates))
            chosen = [d for d in dates if (since is None or d >= since) and (until is None or d <= until)]
            chosen = chosen if since is not None else chosen[-12:]
            runs = []
            for day in chosen:
                start = _midnight(before[day]) + DAY if day in before else _midnight(day) - 7 * DAY
                rows = db.execute('SELECT c.opened_at, c.source, EXISTS (SELECT 1 FROM concern_brief_messages m WHERE m.concern_id = c.id AND m.brief_date = l.run_date) FROM concern_listings l JOIN concerns c ON c.id = l.concern_id '
                                  'WHERE l.run_date = ? AND (? IS NULL OR c.project_id = ?)', (day, scope, scope)).fetchall()
                ages = [(date.fromisoformat(day) - datetime.fromtimestamp(opened, MANILA).date()).days for opened, _, _ in rows]
                runs.append({'run_date': day, 'listed': len(rows), 'sent': sum(sent for _, _, sent in rows), 'new': sum(age == 0 for age in ages),
                             'closed': db.execute("SELECT COUNT(*) FROM concerns WHERE state = 'closed' AND closed_at >= ? AND closed_at < ? AND (? IS NULL OR project_id = ?)", (start, _midnight(day) + DAY, scope, scope)).fetchone()[0],
                             'age_buckets': {'under_2_weeks': sum(age < 14 for age in ages), '2_to_4_weeks': sum(14 <= age <= CHRONIC_DAYS for age in ages), 'over_4_weeks': sum(age > CHRONIC_DAYS for age in ages)},
                             'by_source': {source: sum(s == source for _, s, _ in rows) for source in SOURCES}})
            first, last = since or (chosen[0] if chosen else None), until or (chosen[-1] if chosen else None)
            spent = [] if first is None or last is None else [(closed - opened) / DAY for opened, closed in db.execute(
                "SELECT opened_at, closed_at FROM concerns WHERE state = 'closed' AND closed_at >= ? AND closed_at < ? AND (? IS NULL OR project_id = ?)", (_midnight(first), _midnight(last) + DAY, scope, scope))]
            still = db.execute("SELECT COUNT(*) FROM concerns WHERE state != 'closed' AND (? IS NULL OR project_id = ?)", (scope, scope)).fetchone()[0]
        return Done(result={'runs': runs, 'closed_in_range': len(spent), 'median_days_open': round(statistics.median(spent), 2) if spent else None, 'still_open': still})

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
                    self._schedule(db, concern, now, 1)
                    self._pm_log(db, project, concern, 'concern_auto_raise', quote, now, None)
                    done['raised'] += 1
                elif choice == 'continues':
                    assert isinstance(concern, str)
                    state, revision = row[0], row[1]
                    db.execute("UPDATE concerns SET as_of = ?, evidence_at = ?, state = 'open', revision = revision + 1 WHERE id = ?", (as_of, now, concern))
                    db.execute('DELETE FROM concern_pm_pending WHERE concern_id = ?', (concern,))
                    self._schedule(db, concern, now, revision + 1)
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

    def for_message(self, chat: str, message_id: str) -> list[str]:
        with self.registry._transaction() as db:
            return [row[0] for row in db.execute('SELECT concern_id FROM concern_brief_messages WHERE chat = ? AND message_id = ? ORDER BY concern_id', (chat, message_id))]

    def settle_dates(self, project: str, dates: Mapping[str, str | None]) -> None:
        """Close the Concerns that waited for a Project date which is now filled."""
        now = self.registry._clock()
        with self.registry._transaction() as db:
            for concern, field in db.execute('SELECT concern_id, field FROM concern_dates WHERE project_id = ?', (project,)).fetchall():
                if dates.get(field):
                    settle_concern(db, concern, now, f'{field} ({dates[field]})')

    def reconcile_dates(self, project: str, owner: str, dates: Mapping[str, str | None]) -> dict[str, str]:
        """Apply date-order rules only when both precise dates are available."""
        parsed: dict[str, date | None] = {}
        for field in PROJECT_DATES:
            value = dates.get(field)
            try:
                parsed[field] = date.fromisoformat(value) if value else None
            except ValueError:
                parsed[field] = None
        now = self.registry._clock()
        today = datetime.fromtimestamp(now, MANILA).date().isoformat()
        with self.registry._transaction() as db:
            if not db.execute('SELECT 1 FROM projects WHERE id = ?', (project,)).fetchone() or not db.execute('SELECT 1 FROM persons WHERE id = ? AND active = 1', (owner,)).fetchone():
                raise ValueError('Project and active coordinator are required')
            for rule, first, second in (('submission_before_playdate', 'submission_deadline', 'playdate'), ('archival_before_submission', 'archival_deadline', 'submission_deadline')):
                a, b = parsed[first], parsed[second]
                if a is None or b is None:
                    continue
                row = db.execute('SELECT c.id, c.state FROM concern_date_rules r JOIN concerns c ON c.id = r.concern_id WHERE r.project_id = ? AND r.rule = ?', (project, rule)).fetchone()
                evidence = f'{first} ({a}) is before {second} ({b})'
                if a < b and row is None:
                    if _open_duplicate(db, project, evidence):
                        continue
                    concern = str(T6Id.mint(EntityKind.CONCERN))
                    _insert(db, concern, project, owner, 'data', evidence, today, now, now, 'date_rule')
                    db.execute('INSERT INTO concern_date_rules VALUES (?, ?, ?)', (project, rule, concern))
                    self._log(db, project, concern, AUTO_ACTOR, 'concern_auto_raise', now, None, evidence, evidence)
                elif a < b and row and row[1] == 'closed':
                    db.execute("UPDATE concerns SET state = 'open', closed_at = NULL, revision = revision + 1 WHERE id = ?", (row[0],))
                    self._log(db, project, row[0], AUTO_ACTOR, 'concern_auto_reopen', now, 'closed', evidence, evidence)
                elif a >= b and row:
                    settle_concern(db, row[0], now, f'{first} ({a}), {second} ({b})')
            return {r[0]: r[1] for r in db.execute('SELECT rule, concern_id FROM concern_date_rules WHERE project_id = ?', (project,))}

    def write(self, verb: str, values: dict[str, Any], *, actor: str, idempotency_key: str | None = None, confirm: str | None = None) -> Outcome:
        if verb not in ('raise', 'note', 'resolve', 'reopen', 'record-message') or not actor.strip():
            return Refused(rule='input', reason='A valid verb and actor are required')
        if (idempotency_key is not None and not idempotency_key.strip()) or (confirm and not idempotency_key):
            return Refused(rule='idempotency_key', reason='A nonblank idempotency key is required')
        key = idempotency_key or secrets.token_hex(16)
        inputs = {**values, 'idempotency_key': key}
        fingerprint = json.dumps({'verb': verb, 'actor': actor, **inputs}, sort_keys=True, separators=(',', ':'))
        nonce = self.tokens.verify(confirm, f'concern {verb}', actor, inputs) if confirm else None
        if isinstance(nonce, Refused):
            return nonce
        with self.registry._transaction() as db:
            if verb == 'note' and values.get('from_reply') is not None:
                reference = values['from_reply']
                chat, separator, message = reference.rpartition(':') if isinstance(reference, str) else ('', '', '')
                if not separator or not chat or not message or not isinstance(values.get('message_id'), str) or not values['message_id'].strip():
                    return Refused(rule='input', reason='A brief reference and reply message ID are required')
                if not db.execute('SELECT 1 FROM concern_brief_messages WHERE chat = ? AND message_id = ? AND concern_id = ?', (chat, message, values.get('concern'))).fetchone():
                    return Done(result={'ignored': True})
            elif verb == 'note' and values.get('message_id') is not None:
                return Refused(rule='input', reason='A reply message ID requires a brief reference')
            if verb == 'record-message' and any(not isinstance(values.get(k), str) or not values[k].strip() for k in ('chat', 'message_id')):
                return Refused(rule='input', reason='Chat and message ID are required')
            if verb == 'record-message' and values.get('brief_date') is not None:
                try:
                    date.fromisoformat(values['brief_date'])
                except (TypeError, ValueError):
                    return Refused(rule='input', reason='The brief date must be an ISO date')
            if nonce and db.execute('SELECT 1 FROM used_preview_tokens WHERE nonce = ?', (nonce,)).fetchone():
                return Refused(rule='token_used', reason='Preview token has already been used')
            previous = db.execute('SELECT fingerprint, result FROM verb_requests WHERE idempotency_key = ?', (key,)).fetchone()
            if previous and previous[0] != fingerprint:
                return Refused(rule='idempotency_key', reason='Idempotency key was used for a different request')
            if previous:
                if nonce:
                    db.execute('INSERT INTO used_preview_tokens VALUES (?)', (nonce,))
                    return Done(result=json.loads(previous[1]))
                return self.tokens.create(f'concern {verb}', actor, inputs, json.loads(previous[1]))
            if verb == 'raise':
                source, waiting, text = values.get('source') or 'person', values.get('waiting_on') or '', values.get('text')
                if source not in SOURCES:
                    return Refused(rule='source', reason='Source must be one of ' + ', '.join(SOURCES))
                if any(not isinstance(values.get(k), str) or not values[k].strip() for k in ('project', 'anchor', 'as_of')) or source == 'person' and (not isinstance(values.get('owner'), str) or not values['owner'].strip()):
                    return Refused(rule='input', reason='Project, line and As of date are required, and a coordinator for a Concern a person raises')
                if not isinstance(waiting, str) or text is not None and (not isinstance(text, str) or not text.strip()):
                    return Refused(rule='input', reason='Waiting party and text must be text')
                try:
                    date.fromisoformat(values['as_of'])
                except ValueError:
                    return Refused(rule='as_of', reason='As of must be an ISO date')
                if waiting.strip() and waiting.strip().casefold() not in values['anchor'].casefold():
                    return Refused(rule='waiting_on', reason='The party it waits on must be named in the line')
                if not db.execute('SELECT 1 FROM projects WHERE id = ?', (values['project'],)).fetchone():
                    return Refused(rule='project', reason='Project not found')
                if values.get('owner') is not None and not db.execute('SELECT 1 FROM persons WHERE id = ? AND active = 1', (values['owner'],)).fetchone():
                    return Refused(rule='owner', reason='Active coordinator Person not found')
                if _open_duplicate(db, values['project'], text or values['anchor']):
                    return Refused(rule='duplicate', reason='A Concern with this text is already open in the Project')
                project, concern, revision, state = values['project'], None, None, None
            else:
                concern = values.get('concern')
                row = db.execute('SELECT project_id, state, revision FROM concerns WHERE id = ?', (concern,)).fetchone()
                if not row:
                    return Refused(rule='concern', reason='Concern not found')
                project, state, revision = row
                if verb in ('resolve', 'reopen') and db.execute('SELECT 1 FROM concern_date_rules WHERE concern_id = ?', (concern,)).fetchone():
                    return Refused(rule='rule_made', reason='Date-sanity Concerns follow data only')
                if verb == 'note' and (not isinstance(values.get('text'), str) or not values['text'].strip()):
                    return Refused(rule='note', reason='A note is required')
                if verb == 'reopen' and state == 'closed' and db.execute("SELECT 1 FROM concerns c JOIN concerns o ON o.project_id = c.project_id AND o.text_key = c.text_key AND o.state != 'closed' WHERE c.id = ?", (concern,)).fetchone():
                    return Refused(rule='duplicate', reason='A Concern with this text is already open in the Project')
                if verb == 'resolve' and state == 'closed' or verb == 'reopen' and state == 'open' and not db.execute('SELECT 1 FROM concern_pm_pending WHERE concern_id = ?', (concern,)).fetchone():
                    return Refused(rule='state', reason='Concern cannot make that transition')
            settles = values.get('settles_id')
            deliverable, milestone = values.get('settles_deliverable'), values.get('settles_milestone')
            if deliverable is not None or milestone is not None:
                from t6.deliverable import MILESTONES
                if verb != 'raise' or settles is not None or milestone not in MILESTONES or not db.execute('SELECT 1 FROM deliverables WHERE id = ? AND project_id = ?', (deliverable, project)).fetchone():
                    return Refused(rule='settlement', reason='A Project Deliverable and valid milestone are required')
            awaited = values.get('settles_date')
            if awaited is not None and (verb != 'raise' or settles is not None or deliverable is not None or awaited not in PROJECT_DATES):
                return Refused(rule='settlement', reason='An awaited date is a Project date and the only settlement of a new Concern')
            if settles is not None:
                try:
                    kind = next(k for k in (EntityKind.JOB_ORDER, EntityKind.APPROVAL, EntityKind.MILESTONE) if isinstance(settles, str) and settles.startswith('t6' + k.value + '_'))
                    T6Id.parse(settles, kind)
                except (StopIteration, ValueError):
                    return Refused(rule='settles_id', reason='Settlement must be a Job Order, Approval or Milestone ID')
            plan = {'action': verb, **values, 'project_id': project, 'idempotency_key': key}
            snapshot = json.dumps([concern, revision])
            if not nonce:
                preview = self.tokens.create(f'concern {verb}', actor, inputs, plan)
                token_nonce = self.tokens.verify(preview.token, f'concern {verb}', actor, inputs)
                assert isinstance(token_nonce, str)
                db.execute('INSERT INTO jo_state_previews VALUES (?, ?)', (token_nonce, snapshot))
                return preview
            observed = db.execute('SELECT state FROM jo_state_previews WHERE nonce = ?', (nonce,)).fetchone()
            if not observed or observed[0] != snapshot:
                return Refused(rule='concern_changed', reason='Concern changed since preview; preview again')
            now = self.registry._clock()
            if verb == 'raise':
                concern = str(T6Id.mint(EntityKind.CONCERN))
                _insert(db, concern, project, values.get('owner'), values.get('waiting_on'), values['anchor'], values['as_of'], now, now, values.get('source') or 'person', values.get('text'), settles)
                self._schedule(db, concern, now, 1)
                if deliverable is not None:
                    db.execute('INSERT INTO concern_milestones VALUES (?, ?, ?)', (concern, deliverable, milestone))
                    if db.execute('SELECT 1 FROM milestones WHERE deliverable_id = ? AND milestone = ?', (deliverable, milestone)).fetchone():
                        settle_concern(db, concern, now, f'{deliverable}: {milestone}')
                if awaited is not None:
                    db.execute('INSERT INTO concern_dates VALUES (?, ?, ?)', (concern, project, awaited))
                if settles and (db.execute("SELECT 1 FROM jo_state WHERE job_order_id = ? AND status = 'Done'", (settles,)).fetchone() or db.execute("SELECT 1 FROM approvals WHERE id = ? AND state = 'granted'", (settles,)).fetchone() or db.execute('SELECT 1 FROM milestones WHERE id = ?', (settles,)).fetchone()):
                    settle_linked(db, settles, now)
            elif verb == 'record-message':
                db.execute('INSERT OR IGNORE INTO concern_brief_messages (chat, message_id, concern_id, brief_date) VALUES (?, ?, ?, ?)', (values['chat'], values['message_id'], concern, values.get('brief_date')))
            elif verb == 'note':
                assert isinstance(concern, str) and revision is not None
                db.execute('INSERT INTO concern_notes (concern_id, text, actor, at, message_id) VALUES (?, ?, ?, ?, ?)', (concern, values['text'], actor, now, values.get('message_id')))
                db.execute("UPDATE concerns SET evidence_at = ?, state = CASE WHEN state = 'quiet' THEN 'open' ELSE state END, revision = revision + 1 WHERE id = ?", (now, concern))
                self._schedule(db, concern, now, revision + 1)
            else:
                db.execute('UPDATE concerns SET state = ?, closed_at = ?, manual_override = 1, settles_id = COALESCE(?, settles_id), revision = revision + 1 WHERE id = ?', ('closed' if verb == 'resolve' else 'open', now if verb == 'resolve' else None, settles, concern))
                db.execute('DELETE FROM concern_pm_pending WHERE concern_id = ?', (concern,))
            assert isinstance(concern, str)
            self._log(db, project, concern, actor, 'concern_' + verb, now, state, values, f'Concern {verb}' + (f": {values['text']}" if verb == 'note' else ''))
            result = {**plan, 'concern_id': concern}
            db.execute('INSERT INTO used_preview_tokens VALUES (?)', (nonce,))
            db.execute('INSERT INTO verb_requests VALUES (?, ?, ?)', (key, fingerprint, json.dumps(result)))
            return Done(result=result)

    def _schedule(self, db: Any, concern: str, now: float, revision: int) -> None:
        from t6.worker.internal_queue import InternalJobQueue
        queue = InternalJobQueue(self.registry, clock=lambda: now)
        for weeks, target in ((2, 'quiet'), (4, 'expired')):
            queue.submit('main', 'concern_silence', f'{concern}:{revision}:{target}', {'concern': concern, 'evidence_at': now, 'target': target}, run_at=now + weeks * WEEK, db=db)

    @staticmethod
    def _log(db: Any, project: str, concern: str, actor: str, action: str, now: float, before: Any, after: Any, summary: str) -> None:
        if actor == AUTO_ACTOR and People.auto_refusal(action.split('_', 1)[0]):
            raise PermissionError(f'{AUTO_ACTOR} may only write Concern changes')
        cursor = db.execute('INSERT INTO audit (project_id, action, actor, at, before, after) VALUES (?, ?, ?, ?, ?, ?)', (concern, action, actor, now, json.dumps(before), json.dumps(after)))
        db.execute('INSERT INTO activity (project_id, subject_id, at, actor, action, summary, audit_id) VALUES (?, ?, ?, ?, ?, ?, ?)', (project, concern, now, actor, action, summary, cursor.lastrowid))

    def silence(self, concern: str, evidence_at: float, target: str) -> bool:
        if target not in ('quiet', 'expired'):
            raise ValueError('Invalid silence target')
        with self.registry._transaction() as db:
            row = db.execute('SELECT project_id, state, evidence_at, manual_override FROM concerns WHERE id = ?', (concern,)).fetchone()
            now = self.registry._clock()
            if not row or row[2] != evidence_at or row[3] or row[1] in ('closed', 'expired') or row[1] == target or now < evidence_at + (2 if target == 'quiet' else 4) * WEEK:
                return False
            db.execute('UPDATE concerns SET state = ?, revision = revision + 1 WHERE id = ?', (target, concern))
            reason = f'Concern {target}: no evidence for {2 if target == "quiet" else 4} weeks'
            self._log(db, row[0], concern, AUTO_ACTOR, 'concern_' + target, now, row[1], target, reason)
            return True
