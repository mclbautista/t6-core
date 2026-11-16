"""`concern counts`: how many Concerns each PSR listed and sent, and how they age and close. Counts only."""
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime
from typing import Any
from unittest.mock import patch

from t6.client import Client
from t6.concern import ConcernStore
from t6.concern_observe import observe
from t6.domain.studio_time import MANILA
from t6.ids import EntityKind, T6Id
from t6.notion.testing import FakeNotion, title
from t6.person import People
from t6.preview import PreviewTokens
from t6.registry import Registered
from t6.registry.sqlite import SqliteProjectRegistry
from t6.server.http import make_server
from t6.server.queue import JobQueue
from t6.server.verbs import VerbAPI
from t6.verbs import ConcernCountsInput, Done, Preview, Refused
from test_person import MemoryRegistry

DAY = 24 * 60 * 60
SOURCES = ('person', 'pm_notes_scan', 'date_rule', 'flag_rule', 'pm_notes_read', 'leave')


def manila(day: str, hours: float = 0) -> float:
    return datetime.fromisoformat(day).replace(tzinfo=MANILA).timestamp() + hours * 3600


class CountsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = MemoryRegistry()
        self.addCleanup(self.registry.db.close)
        self.now = manila('2026-11-02', 6)
        self.registry._clock = lambda: self.now
        self.registry._new_id = lambda: str(T6Id.mint(EntityKind.PROJECT))
        self.one, self.two = self.register('page-one'), self.register('page-two')
        with self.registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES ('coordinator', 'staff', '[]', '[]', NULL, 0, 1)")
        self.store = ConcernStore(self.registry, PreviewTokens('secret'))
        self.keys = 0

    def register(self, page: str) -> str:
        registered = self.registry.register(actor='setup', idempotency_key=page, notion_page_id=page)
        assert isinstance(registered, Registered)
        return registered.project.id

    def go(self, run_date: str, findings: list[tuple[str, str, str]], *, dry_run: bool = False) -> dict[str, Any]:
        self.keys += 1
        values = {'run_date': run_date, 'findings': [{'project': p, 'text': t, 'source': s} for p, t, s in findings], 'dry_run': dry_run, 'idempotency_key': f'run-{self.keys}'}
        outcome = observe(self.store, values, actor='reports', coordinators=lambda project: {'coordinator'})
        assert isinstance(outcome, Done)
        result: dict[str, Any] = outcome.result
        return result

    def counts(self, **values: Any) -> dict[str, Any]:
        outcome = self.store.counts(**values)
        assert isinstance(outcome, Done), outcome
        result: dict[str, Any] = outcome.result
        return result

    def concern(self, text: str) -> str:
        with self.registry._transaction() as db:
            return str(db.execute('SELECT id FROM concerns WHERE text = ? AND state != ?', (text, 'closed')).fetchone()[0])

    def three_psrs(self) -> None:
        self.go('2026-11-02', [(self.one, 'Client Corp has no editor', 'flag_rule'), (self.one, 'Client Corp leave clash', 'leave')])
        self.now = manila('2026-11-05', 12)
        with self.registry._transaction() as db:
            db.execute("UPDATE concerns SET state = 'closed', closed_at = ? WHERE text = 'Client Corp leave clash'", (self.now,))
        self.now = manila('2026-11-09', 6)
        self.go('2026-11-09', [(self.one, 'Client Corp has no editor', 'flag_rule'), (self.one, 'Playdate is missing', 'flag_rule')])
        self.now = manila('2026-12-07', 6)
        self.go('2026-12-07', [(self.one, 'Client Corp has no editor', 'flag_rule'), (self.one, 'Playdate is missing', 'flag_rule'), (self.two, 'Client Corp on leave', 'leave')])

    def test_three_run_dates_with_known_listings(self) -> None:
        self.three_psrs()
        reply = self.counts()
        first, second, third = reply['runs']
        self.assertEqual([r['run_date'] for r in reply['runs']], ['2026-11-02', '2026-11-09', '2026-12-07'])
        def buckets(a: int, b: int, c: int) -> dict[str, int]:
            return {'under_2_weeks': a, '2_to_4_weeks': b, 'over_4_weeks': c}

        def by_source(**n: int) -> dict[str, int]:
            return {name: n.get(name, 0) for name in SOURCES}

        self.assertEqual(first, {'run_date': '2026-11-02', 'listed': 2, 'sent': 0, 'new': 2, 'closed': 0, 'age_buckets': buckets(2, 0, 0), 'by_source': by_source(flag_rule=1, leave=1)})
        self.assertEqual(second, {'run_date': '2026-11-09', 'listed': 2, 'sent': 0, 'new': 1, 'closed': 1, 'age_buckets': buckets(2, 0, 0), 'by_source': by_source(flag_rule=2)})
        self.assertEqual(third, {'run_date': '2026-12-07', 'listed': 3, 'sent': 0, 'new': 1, 'closed': 0, 'age_buckets': buckets(1, 1, 1), 'by_source': by_source(flag_rule=2, leave=1)})
        self.assertEqual((reply['closed_in_range'], reply['median_days_open'], reply['still_open']), (1, 3.5, 3))

    def test_a_rerun_of_one_run_date_changes_nothing(self) -> None:
        self.three_psrs()
        before = self.counts()
        self.go('2026-11-09', [(self.one, 'Client Corp has no editor', 'flag_rule'), (self.one, 'Playdate is missing', 'flag_rule')])
        self.go('2026-11-09', [], dry_run=True)
        self.assertEqual(self.counts(), before)

    def test_a_recorded_message_for_the_date_counts_as_sent_and_other_dates_do_not(self) -> None:
        self.three_psrs()
        concern, other = self.concern('Client Corp has no editor'), self.concern('Playdate is missing')
        for values, key in (({'concern': concern, 'chat': 'chat', 'message_id': '1', 'brief_date': '2026-11-09'}, 'a'), ({'concern': other, 'chat': 'chat', 'message_id': '2'}, 'b'), ({'concern': other, 'chat': 'chat', 'message_id': '3', 'brief_date': '2026-11-02'}, 'c')):
            preview = self.store.write('record-message', values, actor='reports', idempotency_key=key)
            assert isinstance(preview, Preview)
            self.assertIsInstance(self.store.write('record-message', values, actor='reports', idempotency_key=key, confirm=preview.token), Done)
        self.assertEqual([(r['run_date'], r['listed'], r['sent']) for r in self.counts()['runs']], [('2026-11-02', 2, 0), ('2026-11-09', 2, 1), ('2026-12-07', 3, 0)])

    def test_project_limits_every_count(self) -> None:
        self.three_psrs()
        reply = self.counts(project=self.two)
        self.assertEqual([(r['run_date'], r['listed'], r['new'], r['closed']) for r in reply['runs']], [('2026-11-02', 0, 0, 0), ('2026-11-09', 0, 0, 0), ('2026-12-07', 1, 1, 0)])
        self.assertEqual((reply['closed_in_range'], reply['median_days_open'], reply['still_open']), (0, None, 1))
        self.assertEqual(self.counts(project='page-two'), reply)
        refused = self.store.counts(project='nowhere')
        assert isinstance(refused, Refused)
        self.assertEqual(refused.rule, 'project')

    def test_since_and_until_pick_the_run_dates_and_the_default_is_the_last_twelve(self) -> None:
        self.three_psrs()
        self.assertEqual([r['run_date'] for r in self.counts(since='2026-11-03', until='2026-11-30')['runs']], ['2026-11-09'])
        self.assertEqual([r['run_date'] for r in self.counts(until='2026-11-09')['runs']], ['2026-11-02', '2026-11-09'])
        concern = self.concern('Playdate is missing')
        with self.registry._transaction() as db:
            for n in range(1, 14):
                db.execute('INSERT INTO concern_listings VALUES (?, ?)', (f'2027-01-{n:02d}', concern))
        runs = self.counts()['runs']
        self.assertEqual((len(runs), runs[0]['run_date'], runs[-1]['run_date']), (12, '2027-01-02', '2027-01-13'))
        for bad in ({'since': 'soon'}, {'until': '2026-13-01'}):
            refused = self.store.counts(**bad)
            assert isinstance(refused, Refused)
            self.assertEqual(refused.rule, 'input')

    def test_the_reply_holds_counts_only(self) -> None:
        self.three_psrs()
        text = json.dumps(self.counts())
        for word in ('Client', 'Playdate', 'page-', 't6p_', 't6c_', 'coordinator'):
            self.assertNotIn(word, text)

    def test_nothing_listed_is_an_empty_reply(self) -> None:
        self.assertEqual(self.counts(), {'runs': [], 'closed_in_range': 0, 'median_days_open': None, 'still_open': 0})


class CountsApiTests(unittest.TestCase):
    def test_a_service_with_only_the_reader_role_can_read_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            notion = FakeNotion()
            notion.add_snapshot_data_source('projects', 'projects')
            registry = SqliteProjectRegistry(directory + '/registry')
            api = VerbAPI(People(registry, PreviewTokens('key')), JobQueue(directory + '/queue'), {'secret': 'reports'}, lambda _: notion.client(), data_sources={'projects': 'projects'})
            server = make_server(('127.0.0.1', 0), api)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with patch.dict(os.environ, {'T6_NOTION_TOKEN': 'fake'}):
                    reply = Client(f'http://127.0.0.1:{server.server_port}', 'secret').concern_counts(ConcernCountsInput())
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
            self.assertEqual(reply, Done(result={'runs': [], 'closed_in_range': 0, 'median_days_open': None, 'still_open': 0}))
