"""Floor reads: bangko, hand-to-mouth and sabit, derived from Deliverables, Milestones and dates."""
import json
import os
import unittest
from datetime import datetime
from typing import Any
from unittest.mock import patch

from t6.domain.studio_time import MANILA
from t6.ids import EntityKind, T6Id
from t6.person import People
from t6.preview import PreviewTokens
from t6.registry import Registered
from t6.verbs import VERBS, Done, Refused, invoke
from test_person import MemoryRegistry


def stamp(day: str, hour: int = 10) -> float:
    return datetime.fromisoformat(f'{day}T{hour:02d}:00:00').replace(tzinfo=MANILA).timestamp()


class FloorTests(unittest.TestCase):
    def setUp(self) -> None:
        env = patch.dict(os.environ, {'T6_PREVIEW_SIGNING_KEY': 'test'})
        env.start()
        self.addCleanup(env.stop)
        for name in ('T6_BANGKO_DAYS', 'T6_HAND_TO_MOUTH_DAYS'):
            os.environ.pop(name, None)
        self.registry = MemoryRegistry()
        self.addCleanup(self.registry.db.close)
        self.registry._new_id = lambda: str(T6Id.mint(EntityKind.PROJECT))
        first = self.registry.register(actor='setup', idempotency_key='one', notion_page_id='page-one')
        second = self.registry.register(actor='setup', idempotency_key='two', notion_page_id='page-two')
        assert isinstance(first, Registered) and isinstance(second, Registered)
        self.one, self.two = first.project.id, second.project.id
        self.people = People(self.registry, PreviewTokens('test'))
        with self.registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES ('reader', 'staff', '[]', '[]', NULL, 0, 1)")

    def deliverable(self, code: str, air: str | None, *, project: str | None = None, delivered: str | None = None) -> str:
        identifier = str(T6Id.mint(EntityKind.DELIVERABLE))
        with self.registry._transaction() as db:
            db.execute('INSERT INTO deliverables VALUES (?, ?, ?, NULL, NULL, ?)', (identifier, project or self.one, code, air))
            if delivered:
                db.execute("INSERT INTO milestones VALUES (?, ?, 'delivered', ?, 'x', 0)", (str(T6Id.mint(EntityKind.MILESTONE)), identifier, delivered))
        return identifier

    def kasado(self, deliverable: str, ticked: str, *, state: str = 'prepared', open_item: bool = False) -> None:
        checklist = [{'text': 'QC', 'blocking': True, 'checked_by': 'x', 'checked_at': stamp(ticked)},
                     {'text': 'Note', 'blocking': False, 'checked_by': None, 'checked_at': None}]
        if open_item:
            checklist.append({'text': 'Slate', 'blocking': True, 'checked_by': None, 'checked_at': None})
        with self.registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES ('to', 'staff', '[]', '[]', NULL, 0, 1) ON CONFLICT DO NOTHING")
            db.execute("INSERT INTO packages VALUES (?, ?, 'delivery', 'reader', 'nas', 'to', NULL, '{}', ?, ?, NULL, 1)",
                       (str(T6Id.mint(EntityKind.PACKAGE)), deliverable, json.dumps(checklist), state))

    def read(self, verb: str, values: dict[str, Any] | None = None, actor: str = 'reader') -> Any:
        entry = next(v for v in VERBS if (v.noun, v.verb) == ('deliverable', verb))
        return invoke(entry, entry.input_model.model_validate(values or {}), actor=actor, people=self.people, open_notion=lambda *_: None)

    def codes(self, verb: str, values: dict[str, Any] | None = None) -> list[str]:
        outcome = self.read(verb, values)
        assert isinstance(outcome, Done), outcome
        return sorted(item['code'] for item in outcome.result['deliverables'])

    def test_bangko_at_below_and_above_threshold(self) -> None:
        self.deliverable('above', '2026-12-10', delivered='2026-12-05')
        self.deliverable('at', '2026-12-10', delivered='2026-12-07')
        self.deliverable('below', '2026-12-10', delivered='2026-12-08')
        self.deliverable('hand', '2026-12-10', delivered='2026-12-09')
        self.deliverable('unfinished', '2026-12-10')
        self.deliverable('no-air', None, delivered='2026-12-01')
        self.assertEqual(self.codes('bangko'), ['above', 'at'])

    def test_hand_to_mouth_at_below_and_above_threshold(self) -> None:
        self.deliverable('same-day', '2026-12-10', delivered='2026-12-10')
        self.deliverable('one-day', '2026-12-10', delivered='2026-12-09')
        self.deliverable('two-days', '2026-12-10', delivered='2026-12-08')
        self.deliverable('late', '2026-12-10', delivered='2026-12-11')
        self.deliverable('unfinished', '2026-12-10')
        self.assertEqual(self.codes('hand-to-mouth'), ['one-day', 'same-day'])

    def test_kasado_counts_as_ready_on_the_day_the_last_blocking_item_was_ticked(self) -> None:
        early = self.deliverable('kasado-early', '2026-12-10')
        self.kasado(early, '2026-12-06')
        close = self.deliverable('kasado-close', '2026-12-10')
        self.kasado(close, '2026-12-09', state='handed_off')
        waiting = self.deliverable('not-kasado', '2026-12-10')
        self.kasado(waiting, '2026-12-01', open_item=True)
        rejected = self.deliverable('rejected', '2026-12-10')
        self.kasado(rejected, '2026-12-01', state='rejected')
        self.assertEqual(self.codes('bangko'), ['kasado-early'])
        self.assertEqual(self.codes('hand-to-mouth'), ['kasado-close'])

    def package(self, deliverable: str, checklist: list[dict[str, Any]], *, kind: str = 'delivery', prepared_on: str | None = None) -> str:
        identifier = str(T6Id.mint(EntityKind.PACKAGE))
        with self.registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES ('to', 'staff', '[]', '[]', NULL, 0, 1) ON CONFLICT DO NOTHING")
            db.execute("INSERT INTO packages VALUES (?, ?, ?, 'reader', 'nas', 'to', NULL, '{}', ?, 'prepared', NULL, 1)", (identifier, deliverable, kind, json.dumps(checklist)))
            if prepared_on:
                db.execute("INSERT INTO audit (project_id, action, actor, at, before, after) VALUES (?, 'package_prepare', 'reader', ?, NULL, '{}')", (identifier, stamp(prepared_on)))
        return identifier

    def test_kasado_is_one_rule_for_package_get_and_the_floor_reads(self) -> None:
        """#178: every spec starts with zero blocking items, so a Package with none is kasado from the day it was prepared."""
        from t6.package import PackageStore
        advisory = [{'text': 'Note', 'blocking': False, 'checked_by': None, 'checked_at': None}]
        plain = self.deliverable('no-blocking', '2026-12-10')
        identifier = self.package(plain, advisory, prepared_on='2026-12-04')
        with self.registry._transaction() as db:
            record = PackageStore._record(db.execute('SELECT * FROM packages WHERE id = ?', (identifier,)).fetchone())
        self.assertTrue(record['kasado'])
        self.assertEqual(self.codes('bangko'), ['no-blocking'])

    def test_bangko_counts_only_delivery_packages(self) -> None:
        """Ready for the air date means ready to deliver to the client: a Color or Sound handoff ticked early is not."""
        internal = self.deliverable('internal', '2026-12-10')
        self.package(internal, [{'text': 'QC', 'blocking': True, 'checked_by': 'x', 'checked_at': stamp('2026-12-01')}], kind='Color')
        self.assertEqual(self.codes('bangko'), [])

    def test_a_deliverable_missed_twice_is_listed_once_with_the_latest_miss(self) -> None:
        twice = self.deliverable('twice', '2026-12-10')
        with self.registry._transaction() as db:
            db.execute("INSERT INTO deliverable_misses VALUES (?, 'first reason', 'x', ?)", (twice, stamp('2026-12-11')))
            db.execute("INSERT INTO deliverable_misses VALUES (?, 'second reason', 'x', ?)", (twice, stamp('2026-12-14')))
        outcome = self.read('sabit')
        assert isinstance(outcome, Done)
        self.assertEqual([(item['code'], item['reason'], item['missed_on']) for item in outcome.result['deliverables']],
                         [('twice', 'second reason', '2026-12-14')])

    def test_ready_date_is_the_manila_day_and_the_earliest_signal(self) -> None:
        both = self.deliverable('both', '2026-12-10', delivered='2026-12-09')
        self.kasado(both, '2026-12-05')
        edge = self.deliverable('edge', '2026-12-10')
        checklist = [{'text': 'QC', 'blocking': True, 'checked_by': 'x', 'checked_at': stamp('2026-12-06', 1)}]
        with self.registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES ('to', 'staff', '[]', '[]', NULL, 0, 1) ON CONFLICT DO NOTHING")
            db.execute("INSERT INTO packages VALUES ('t6k_edge', ?, 'delivery', 'reader', 'nas', 'to', NULL, '{}', ?, 'prepared', NULL, 1)", (edge, json.dumps(checklist)))
        self.assertEqual(self.codes('bangko'), ['both', 'edge'])

    def test_per_project_and_studio_wide(self) -> None:
        self.deliverable('a', '2026-12-10', delivered='2026-12-01')
        self.deliverable('b', '2026-12-10', project=self.two, delivered='2026-12-01')
        self.assertEqual(self.codes('bangko'), ['a', 'b'])
        self.assertEqual(self.codes('bangko', {'project': self.two}), ['b'])
        self.assertEqual(self.codes('bangko', {'project': 'page-one'}), ['a'])
        self.assertIsInstance(self.read('bangko', {'project': 'no-such-project'}), Refused)

    def test_thresholds_come_from_config_with_documented_defaults(self) -> None:
        self.deliverable('five', '2026-12-10', delivered='2026-12-05')
        self.deliverable('three', '2026-12-10', delivered='2026-12-07')
        self.deliverable('two', '2026-12-10', delivered='2026-12-08')
        outcome = self.read('bangko')
        assert isinstance(outcome, Done)
        self.assertEqual(outcome.result['margin_days'], 3)
        self.assertEqual(self.read('hand-to-mouth').result['margin_days'], 1)
        with patch.dict(os.environ, {'T6_BANGKO_DAYS': '5', 'T6_HAND_TO_MOUTH_DAYS': '2'}):
            self.assertEqual(self.codes('bangko'), ['five'])
            self.assertEqual(self.codes('hand-to-mouth'), ['two'])
        with patch.dict(os.environ, {'T6_BANGKO_DAYS': 'soon'}):
            refusal = self.read('bangko')
        assert isinstance(refusal, Refused)
        self.assertEqual(refusal.rule, 'config')
        with patch.dict(os.environ, {'T6_HAND_TO_MOUTH_DAYS': '-1'}):
            self.assertIsInstance(self.read('hand-to-mouth'), Refused)

    def test_a_delayed_deliverable_is_not_sabit_and_a_missed_one_is(self) -> None:
        delayed = self.deliverable('delayed', '2026-12-10')
        missed = self.deliverable('missed', '2026-12-10', project=self.two)
        self.deliverable('plain', '2026-12-10')
        with self.registry._transaction() as db:
            db.execute("INSERT INTO deliverable_date_moves VALUES (?, 'air', '2026-12-01', '2026-12-10', 'client asked', 'x', ?)", (delayed, stamp('2026-12-02')))
            db.execute("INSERT INTO deliverable_misses VALUES (?, 'color not ready', 'x', ?)", (missed, stamp('2026-12-11', 23)))
        outcome = self.read('sabit')
        assert isinstance(outcome, Done)
        self.assertEqual([(item['code'], item['reason'], item['missed_on']) for item in outcome.result['deliverables']],
                         [('missed', 'color not ready', '2026-12-11')])
        self.assertEqual(self.codes('sabit', {'project': self.one}), [])
        self.assertEqual(self.codes('sabit', {'project': self.two}), ['missed'])

    def test_reads_store_nothing_and_are_listed_as_reader_read_only_verbs(self) -> None:
        from t6.verbs import catalogue
        self.deliverable('a', '2026-12-10', delivered='2026-12-01')
        with self.registry._transaction() as db:
            before = db.execute('SELECT (SELECT count(*) FROM audit), (SELECT count(*) FROM activity), (SELECT count(*) FROM sync_intents)').fetchone()
        for verb in ('bangko', 'hand-to-mouth', 'sabit'):
            self.read(verb)
        with self.registry._transaction() as db:
            self.assertEqual(db.execute('SELECT (SELECT count(*) FROM audit), (SELECT count(*) FROM activity), (SELECT count(*) FROM sync_intents)').fetchone(), before)
        listed = {entry['verb']: entry for entry in catalogue() if entry['noun'] == 'deliverable'}
        for verb in ('bangko', 'hand-to-mouth', 'sabit'):
            self.assertEqual((listed[verb]['role'], listed[verb]['read_only']), ('reader', True))
        self.assertIsInstance(self.read('bangko', actor='stranger'), Refused)
