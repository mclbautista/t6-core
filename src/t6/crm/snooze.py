"""CRM snooze: a person pauses their own CRM DMs until a date, and the items go to someone else (#716).

`until` is the last snoozed day, in Manila; the person's items return the day after. Rows are never expired by a job:
a row whose date has passed is simply not active, so nothing can fail to wake someone up.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timedelta

from t6.domain.studio_time import MANILA
from t6.registry.sqlite import SqliteProjectRegistry, _audit

MAX_DAYS = 30
POST_PRODUCERS_VARIABLE = 'T6_CRM_POST_PRODUCER_TELEGRAM_IDS'


def post_producer_telegram_ids(environ: Mapping[str, str]) -> tuple[int, ...]:
    """The Post Producer setting: Telegram user ids, comma-separated (parsed like `T6_CLOSER_TELEGRAM_IDS`)."""
    return tuple(int(value) for value in environ.get(POST_PRODUCERS_VARIABLE, '').split(',') if value.strip())


class Snoozes:
    def __init__(self, registry: SqliteProjectRegistry) -> None:
        self.registry = registry

    def today(self) -> date:
        return datetime.fromtimestamp(self.registry._clock(), MANILA).date()

    def active(self, today: date) -> dict[str, date]:
        """Person ID to the last snoozed day, for everyone snoozed on `today`."""
        with self.registry._transaction() as db:
            rows = db.execute('SELECT person_id, until FROM crm_snooze WHERE until >= ?', (today.isoformat(),)).fetchall()
        return {str(person): date.fromisoformat(str(until)) for person, until in rows}

    def snooze(self, person_id: str, until: str | None) -> date:
        """Snooze through `until`. A ValueError carries the refusal sentence, written to be shown as is."""
        today = self.today()
        latest = today + timedelta(days=MAX_DAYS)
        if until is None or not until.strip():
            raise ValueError(f'Give an end date, like {latest.isoformat()}. It can be at most {MAX_DAYS} days from today.')
        try:
            end = date.fromisoformat(until.strip())
        except ValueError:
            raise ValueError(f'I could not read "{until.strip()}" as a date. Use a date like {latest.isoformat()}.')
        if end < today:
            raise ValueError('That date has already passed. Give today or a later date.')
        if end > latest:
            raise ValueError(f'Snooze is at most {MAX_DAYS} days. The latest end date today is {latest.isoformat()}.')
        now = self.registry._clock()
        with self.registry._transaction() as db:
            before = db.execute('SELECT until FROM crm_snooze WHERE person_id = ?', (person_id,)).fetchone()
            db.execute('INSERT OR REPLACE INTO crm_snooze (person_id, until, set_at) VALUES (?, ?, ?)', (person_id, end.isoformat(), now))
            _audit(db, person_id, 'crm_snooze', person_id, now, None if before is None else {'until': before[0]}, {'until': end.isoformat()},
                   summary=f'CRM reminders snoozed until {end.isoformat()}')
        return end

    def unsnooze(self, person_id: str) -> bool:
        """True when the person was snoozed (even if the end date had passed); only then is anything written."""
        now = self.registry._clock()
        with self.registry._transaction() as db:
            before = db.execute('SELECT until FROM crm_snooze WHERE person_id = ?', (person_id,)).fetchone()
            if before is None:
                return False
            db.execute('DELETE FROM crm_snooze WHERE person_id = ?', (person_id,))
            _audit(db, person_id, 'crm_unsnooze', person_id, now, {'until': before[0]}, {'until': None}, summary='CRM reminders unsnoozed')
        return True
