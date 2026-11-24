"""Daily 10:00 Manila CRM DM: each Lead Owner's cold Leads, in one message through the worker gateway (#714).

Follows `t6.drives.reminder`. The wording is composed here, HTML-escaped. Only T6 Persons are messaged, never a client.
`digest` returns the items per recipient, so later items (quote and billing reminders, snooze rerouting) join the same
DM by adding items or changing the recipient before `compose` runs.
"""
from __future__ import annotations

import html
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time as daytime, timedelta
from typing import Any

from t6.concern import ConcernStore
from t6.crm.config import CrmRoles
from t6.crm.log import CrmLog, StageChange, due_reminders, read_log
from t6.crm.snooze import Snoozes
from t6.domain import CrmStage, Project
from t6.domain.events import JsonValue
from t6.domain.project_rules import crm_stage, is_lead
from t6.domain.studio_time import MANILA
from t6.drives.sheets import SheetSource
from t6.person import People
from t6.registry.sqlite import latest_stages
from t6.worker import WorkerJob
from t6.worker.gateway import GatewayClient
from t6.worker.internal_queue import InternalJobQueue
from t6.worker.runner import Refused

KIND = 'crm.reminder'
HOUR = 10
COLD_DAYS = {CrmStage.LEADS: 28, CrmStage.NEGOTIATIONS: 7, CrmStage.PROPOSAL_MADE: 3, CrmStage.ONBOARDING: 2}
"""A Lead is cold once this many Manila days have passed since its last contact."""
HEADERS = {'cold': 'Cold Leads', 'reminders': 'Reminders'}
"""The DM's sections, in order. Each Item names its section."""
CONCERNS_SHOWN = 3
TELEGRAM_PREFIX = 'tg:'
"""A role holder who is no Person is addressed as `tg:<Telegram ID>`."""


@dataclass(frozen=True)
class LeadFacts:
    project_id: str
    name: str
    stage: CrmStage
    owner_ids: tuple[str, ...]  # Person IDs; empty when the Lead Owner is blank or maps to no Person
    has_lead_contact: bool  # a Delegate Producer, Line Producer or Prod Coor/Supe is set
    has_studio: bool


@dataclass(frozen=True)
class Item:
    section: str
    lines: tuple[str, ...]


Digest = dict[str, list[Item]]
"""Person ID to the items that person gets today."""


def read_leads(registry: Any, projects: Sequence[Project]) -> list[LeadFacts]:
    """The open CRM Leads among `projects`. The stage is the newest stage history row, else the Notion Status."""
    with registry._transaction() as db:
        current = latest_stages(db)
    leads = []
    for project in projects:
        project_id = registry.bound_entity('notion', project.page_id)
        if project_id is None:
            continue
        stage = crm_stage(current[project_id][0] if project_id in current else None, project.status)
        if stage is None or not is_lead(stage):
            continue
        owners = (registry.bound_entity('notion', page) for page in project.lead_owner_ids)
        leads.append(LeadFacts(project_id, project.name or '(untitled)', stage, tuple(o for o in owners if o is not None),
                               bool(project.delegate_producer_ids or project.line_producer_ids or project.prod_coor_supe_ids),
                               bool(project.studio_id)))
    return leads


def stage_changes(registry: Any) -> StageChange:
    """Each Project's last stage change as a Manila day; a Project with none counts from its registration."""
    with registry._transaction() as db:
        changed = {str(row[0]): float(row[1]) for row in db.execute('SELECT project_id, MAX(at) FROM project_stage_history GROUP BY project_id')}
        registered = {str(row[0]): float(row[1]) for row in db.execute('SELECT id, created_at FROM projects WHERE created_at IS NOT NULL')}

    def last(project_id: str) -> date | None:
        moment = changed.get(project_id, registered.get(project_id))
        return None if moment is None else datetime.fromtimestamp(moment, MANILA).date()
    return last


class CrmReminder:
    def __init__(self, people: People, queue: InternalJobQueue, gateway: GatewayClient, source: SheetSource, default_owner: str,
                 leads: Callable[[], Sequence[LeadFacts]], name_of: Callable[[str], str | None], *,
                 roles: CrmRoles = CrmRoles(), project_name: Callable[[str], str | None] = lambda _: None,
                 clock: Callable[[], float] = time.time) -> None:
        self.people, self.queue, self.gateway, self.source, self.default_owner = people, queue, gateway, source, default_owner
        self.leads, self.name_of, self.clock = leads, name_of, clock
        self.roles, self.project_name = roles, project_name
        self.registry = people.registry
        self.concerns = ConcernStore(self.registry, people.tokens)

    def schedule(self, on: date | None = None) -> None:
        day = on or datetime.fromtimestamp(self.clock(), MANILA).date()
        morning = datetime.combine(day, daytime(HOUR), MANILA)
        self.queue.submit('main', KIND, day.isoformat(), run_at=morning.timestamp())

    def _telegram(self, person_id: str) -> int | None:
        if person_id.startswith(TELEGRAM_PREFIX):
            return int(person_id[len(TELEGRAM_PREFIX):])
        person = self.people.get(person_id)
        if person is None or not person['active'] or not person['telegram_id'] or int(person['telegram_id']) <= 0:
            return None
        return int(person['telegram_id'])

    def digest(self, today: date) -> Digest:
        """Cold Leads by recipient. A blank owner, or one who cannot be messaged, is the default owner."""
        leads = self.leads()
        with self.registry._transaction() as db:
            known = {str(row[0]) for row in db.execute('SELECT id FROM projects')}
        log: CrmLog = read_log(self.source, known)
        last_change = stage_changes(self.registry)
        digest: Digest = {}
        for lead in leads:
            last = log.last_contact(lead.project_id, last_change)
            if last is None or (today - last).days < COLD_DAYS[lead.stage]:
                continue
            owners = [o for o in lead.owner_ids if self._telegram(o) is not None] or [self.default_owner]
            item = Item('cold', self._lines(lead, (today - last).days))
            for owner in dict.fromkeys(owners):
                digest.setdefault(owner, []).append(item)
        self._add_reminders(digest, log, today, {lead.project_id: lead.name for lead in leads})
        return self._rerouted(digest, today)

    def _rerouted(self, digest: Digest, today: date) -> Digest:
        """Move a snoozed person's items to the Post Producers who are not snoozed, else to the default owner.

        The default owner keeps the items when nobody else can take them, even if snoozed; a snooze never drops an item.
        """
        snoozed = Snoozes(self.registry).active(today)
        if not any(person in snoozed for person in digest):
            return digest
        producers = [who for who in dict.fromkeys(self._recipient(t) for t in sorted(self.roles.post_producer)) if who not in snoozed]
        rerouted: Digest = {person: items for person, items in digest.items() if person not in snoozed}
        covering: dict[tuple[str, int], tuple[Item, list[str]]] = {}
        for person, items in digest.items():
            if person not in snoozed:
                continue
            note = f'{self.name_of(person) or "a colleague"} (snoozed until {snoozed[person].isoformat()})'
            for target in [p for p in producers if p != person] or [self.default_owner]:
                for item in (i for i in items if target == person or not any(i is held for held in digest.get(target, ()))):
                    covering.setdefault((target, id(item)), (item, []))[1].append(note)
        for (target, _), (item, notes) in covering.items():
            rerouted.setdefault(target, []).append(Item(item.section, (*item.lines, f'  Covering for {", ".join(notes)}')))
        return rerouted

    def _recipient(self, telegram: int) -> str:
        """The active Person holding this Telegram ID, so one DM covers all their items; else `tg:<id>`."""
        return self.people.by_telegram(telegram) or f'{TELEGRAM_PREFIX}{telegram}'

    def _add_reminders(self, digest: Digest, log: CrmLog, today: date, names: dict[str, str]) -> None:
        """Due quote rows go to the Post Producer, billing rows to the Post Producer, Accountant and Finance Head."""
        for row in sorted(due_reminders(log, today), key=lambda r: (r.remind_on or today, r.project_id)):
            if row.project_id not in names:  # no longer a Lead: read its name, else show its ID
                names[row.project_id] = self.project_name(row.project_id) or row.project_id
            holders = self.roles.quote if row.kind == 'quote' else self.roles.billing
            line = f'{row.kind.title()} due {row.remind_on}: {names[row.project_id]}, {row.note or "no note"}'
            item = Item('reminders', (f'{line} {row.link}' if row.link else line,))
            for recipient in dict.fromkeys(self._recipient(t) for t in sorted(holders)):
                digest.setdefault(recipient, []).append(item)

    def _lines(self, lead: LeadFacts, days: int) -> tuple[str, ...]:
        lines = [f'{lead.name} — {lead.stage.value}, {days} days since last contact']
        concerns = self.concerns.open_concerns(lead.project_id)
        lines.extend(f'  Concern: {concern["text"]}' for concern in concerns[:CONCERNS_SHOWN])
        if len(concerns) > CONCERNS_SHOWN:
            lines.append(f'  and {len(concerns) - CONCERNS_SHOWN} more open Concerns')
        missing = [label for label, present in (('Lead contact', lead.has_lead_contact), ('Studio', lead.has_studio)) if not present]
        if missing:
            lines.append(f'  Missing: {", ".join(missing)}')
        return tuple(lines)

    def compose(self, person_id: str, items: Sequence[Item]) -> str:
        name = self.name_of(person_id)
        lines = [f'Hi {name},' if name else 'Hi,']
        for section, header in HEADERS.items():
            chosen = [item for item in items if item.section == section]
            if chosen:
                lines.append(header)
                lines.extend(line for item in chosen for line in item.lines)
        return html.escape('\n'.join(lines), quote=False)

    def handle(self, job: WorkerJob) -> JsonValue:
        today = datetime.fromtimestamp(self.clock(), MANILA).date()
        self.schedule(today + timedelta(days=1))
        if job.dedupe_key != today.isoformat():
            self.schedule(today)
            return {'skipped': 'obsolete date'}
        digest = self.digest(today)
        skipped = []
        for person_id, items in digest.items():
            telegram = self._telegram(person_id)
            if telegram is None:
                skipped.append(person_id)  # the others still get theirs; a retry keeps the same keys
                continue
            self.gateway.notify(telegram, self.compose(person_id, items), [], f'crm-dm:{person_id}:{today.isoformat()}')
        if skipped:
            raise Refused(f'CRM DM not sent to {", ".join(skipped)}: each recipient must be an active Person with a Telegram ID')
        return {'recipients': len(digest), 'items': sum(len(items) for items in digest.values())}
