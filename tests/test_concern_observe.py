"""`concern observe`: new, known and not seen, with an invented Project and a fake clock."""
import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime
from typing import Any
from unittest.mock import patch

from t6.client import Client
from t6.concern import CHRONIC_DAYS, WEEK, ConcernStore
from t6.concern_observe import observe
from t6.domain.studio_time import MANILA
from t6.ids import EntityKind, T6Id
from t6.notion.testing import FakeNotion, title
from t6.person import People
from t6.preview import PreviewTokens
from t6.privacy import Person, Registry
from t6.registry import Registered
from t6.registry.sqlite import SqliteProjectRegistry
from t6.server.http import make_server
from t6.server.queue import JobQueue
from t6.server.verbs import VerbAPI
from t6.verbs import ConcernObserveInput, Done, Refused
from test_person import MemoryRegistry

DAY = 24 * 60 * 60


def manila(run_date: str) -> float:
    return datetime.fromisoformat(run_date).replace(tzinfo=MANILA).timestamp()


class ObserveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = MemoryRegistry()
        self.addCleanup(self.registry.db.close)
        self.now = manila('2026-11-16') + 6 * 3600
        self.registry._clock = lambda: self.now
        self.registry._new_id = lambda: str(T6Id.mint(EntityKind.PROJECT))
        self.project = self.register('project-one')
        self.other = self.register('project-two')
        with self.registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES ('coordinator', 'staff', '[]', '[]', NULL, 0, 1)")
            db.execute("INSERT INTO persons VALUES ('outsider', 'staff', '[]', '[]', NULL, 0, 1)")
            db.execute("INSERT INTO persons VALUES ('retired', 'staff', '[]', '[]', NULL, 0, 0)")
        self.store = ConcernStore(self.registry, PreviewTokens('secret'))
        self.keys = 0

    def register(self, page: str) -> str:
        registered = self.registry.register(actor='setup', idempotency_key=page, notion_page_id=page)
        assert isinstance(registered, Registered)
        return registered.project.id

    def go(self, run_date: str, findings: list[dict[str, Any]], unavailable: list[dict[str, Any]] | None = None, *, closes: bool = False, dry_run: bool = False, key: str | None = None) -> Any:
        self.keys += 1
        values = {'run_date': run_date, 'findings': findings, 'unavailable': unavailable or [], 'dry_run': dry_run, 'idempotency_key': key or f'run-{self.keys}'}
        outcome = observe(self.store, values, actor='reports', coordinators=lambda project: {'coordinator'}, closes=closes)
        assert isinstance(outcome, Done), outcome
        return outcome.result

    def close(self, concern: str) -> None:
        preview = self.store.write('resolve', {'concern': concern}, actor='person', idempotency_key='close-' + concern)
        self.store.write('resolve', {'concern': concern}, actor='person', idempotency_key='close-' + concern, confirm=preview.token)  # type: ignore[union-attr]

    def finding(self, text: str, source: str = 'flag_rule', **more: Any) -> dict[str, Any]:
        return {'project': self.project, 'text': text, 'source': source, **more}

    def open_of(self, result: dict[str, Any], project: str | None = None) -> list[dict[str, Any]]:
        return next((p['open'] for p in result['projects'] if p['project_id'] == (project or self.project)), [])

    def test_a_new_finding_opens_a_concern_on_the_run_date(self) -> None:
        result = self.go('2026-11-16', [self.finding('No editor is named')])
        [item] = self.open_of(result)
        self.assertEqual((item['text'], item['source'], item['opened_on'], item['age_days'], item['weeks'], item['chronic'], item['state'], item['seen_this_run']),
                         ('No editor is named', 'flag_rule', '2026-11-16', 0, 0, False, 'open', True))
        record = self.store.get(item['id'])
        assert record is not None
        self.assertEqual((record['opened_at'], record['evidence_at'], record['owner_id'], record['waiting_on'], record['source']), (manila('2026-11-16'), self.now, None, '', 'flag_rule'))
        self.assertEqual({k: v for k, v in result['counts'].items() if v}, {'new': 1})
        with self.registry._transaction() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM internal_jobs WHERE kind = 'concern_silence'").fetchone()[0], 2)
        self.assertTrue(any(a.action == 'concern_auto_raise' and a.subject_id == item['id'] for a in self.registry.activity(self.project)))

    def test_the_same_finding_a_week_later_keeps_opened_at_and_grows_weeks(self) -> None:
        first = self.open_of(self.go('2026-11-16', [self.finding('No editor is named')]))[0]
        self.now += 7 * DAY
        result = self.go('2026-11-23', [self.finding('no  EDITOR is named')])
        [item] = self.open_of(result)
        self.assertEqual((item['id'], item['opened_on'], item['age_days'], item['weeks'], item['chronic']), (first['id'], '2026-11-16', 7, 1, False))
        self.assertEqual(item['text'], 'no  EDITOR is named')
        self.assertEqual({k: v for k, v in result['counts'].items() if v}, {'known': 1})
        self.assertEqual(self.store.get(first['id'])['evidence_at'], self.now)  # type: ignore[index]

    def test_a_concern_is_chronic_after_28_days(self) -> None:
        self.go('2026-11-16', [self.finding('Playdate is missing')])
        self.assertEqual(CHRONIC_DAYS, 28)
        self.now += 28 * DAY
        self.assertFalse(self.open_of(self.go('2026-12-14', [self.finding('Playdate is missing')]))[0]['chronic'])
        self.now += DAY
        self.assertTrue(self.open_of(self.go('2026-12-15', [self.finding('Playdate is missing')]))[0]['chronic'])

    def test_a_missing_finding_changes_nothing_and_the_old_timers_take_it_quiet_then_expired(self) -> None:
        item = self.open_of(self.go('2026-11-16', [self.finding('No editor is named')]))[0]
        evidence = self.now
        before = self.store.get(item['id'])
        assert before is not None
        self.now += 7 * DAY
        result = self.go('2026-11-23', [])
        [seen] = self.open_of(result)
        self.assertEqual((seen['id'], seen['seen_this_run'], seen['state']), (item['id'], False, 'open'))
        self.assertEqual({k: v for k, v in result['counts'].items() if v}, {'not_seen': 1})
        self.assertEqual(self.store.get(item['id']), {**before, 'age_seconds': self.now - before['opened_at']})
        self.now = evidence + 2 * WEEK
        self.assertTrue(self.store.silence(item['id'], evidence, 'quiet'))
        self.now = evidence + 4 * WEEK
        self.assertTrue(self.store.silence(item['id'], evidence, 'expired'))
        self.assertEqual(self.open_of(self.go('2026-12-14', []))[0]['state'], 'expired')

    def test_a_quiet_concern_found_again_is_open_again_with_fresh_timers(self) -> None:
        item = self.open_of(self.go('2026-11-16', [self.finding('No editor is named')]))[0]
        self.now += 2 * WEEK
        self.assertTrue(self.store.silence(item['id'], self.store.get(item['id'])['evidence_at'], 'quiet'))  # type: ignore[index]
        self.go('2026-11-30', [self.finding('No editor is named')])
        record = self.store.get(item['id'])
        assert record is not None
        self.assertEqual((record['state'], record['evidence_at']), ('open', self.now))
        with self.registry._transaction() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM internal_jobs WHERE kind = 'concern_silence'").fetchone()[0], 4)

    def test_a_concern_closed_by_a_person_shows_once_and_the_same_text_starts_a_new_one(self) -> None:
        item = self.open_of(self.go('2026-11-16', [self.finding('No editor is named')]))[0]
        self.now += 3 * DAY
        preview = self.store.write('resolve', {'concern': item['id']}, actor='person', idempotency_key='close')
        self.store.write('resolve', {'concern': item['id']}, actor='person', idempotency_key='close', confirm=preview.token)  # type: ignore[union-attr]
        self.now += 4 * DAY
        result = self.go('2026-11-23', [self.finding('No editor is named')])
        [project] = result['projects']
        self.assertEqual(project['closed_since_last_run'], [{'id': item['id'], 'text': 'No editor is named'}])
        [fresh] = project['open']
        self.assertNotEqual(fresh['id'], item['id'])
        self.assertEqual((fresh['opened_on'], fresh['age_days']), ('2026-11-23', 0))
        self.assertEqual(result['counts']['new'], 1)
        self.now += 7 * DAY
        self.assertEqual(self.go('2026-11-30', [self.finding('No editor is named')])['projects'][0]['closed_since_last_run'], [])

    def test_a_pair_that_could_not_be_read_is_not_counted_as_not_seen(self) -> None:
        self.go('2026-11-16', [self.finding('Leave clash', 'leave'), self.finding('Playdate is missing', 'flag_rule')])
        self.now += 7 * DAY
        result = self.go('2026-11-23', [], [{'project': self.project, 'source': 'leave'}])
        self.assertEqual({k: v for k, v in result['counts'].items() if v}, {'not_seen': 1, 'unavailable': 1})
        self.assertEqual({i['text']: i['seen_this_run'] for i in self.open_of(result)}, {'Leave clash': False, 'Playdate is missing': False})

    def test_a_concern_of_a_person_scan_or_date_rule_is_listed_and_never_touched(self) -> None:
        with self.registry._transaction() as db:
            for concern, source in (('t6c_person', 'person'), ('t6c_scan', 'pm_notes_scan'), ('t6c_rule', 'date_rule')):
                db.execute("INSERT INTO concerns (id, project_id, owner_id, waiting_on, anchor, text, text_key, source, as_of, opened_at, evidence_at, state) VALUES (?, ?, 'coordinator', 'client', ?, ?, ?, ?, '2026-11-01', 1, 1, 'open')",
                           (concern, self.project, 'Waiting on client ' + concern, 'Waiting on client ' + concern, 'waiting on client ' + concern, source))
        self.now += 60 * DAY
        result = self.go('2026-11-16', [self.finding('Waiting on client t6c_person', 'pm_notes_read')], closes=True)
        self.assertEqual({i['id']: (i['source'], i['seen_this_run']) for i in self.open_of(result)}, {'t6c_person': ('person', True), 't6c_scan': ('pm_notes_scan', False), 't6c_rule': ('date_rule', False)})
        self.assertEqual({k: v for k, v in result['counts'].items() if v}, {'known': 1, 'projects_with_both': 1})
        with self.registry._transaction() as db:
            self.assertEqual(db.execute("SELECT COUNT(*), MIN(evidence_at), MAX(evidence_at), MIN(revision), MAX(revision), MIN(state), MAX(state) FROM concerns").fetchone(), (3, 1, 1, 1, 1, 'open', 'open'))

    def test_an_owner_must_be_an_active_coordinator_of_the_project_or_the_finding_is_skipped(self) -> None:
        result = self.go('2026-11-16', [self.finding('Ok owner', owner='coordinator', waiting_on='lab'), self.finding('Not a coordinator', owner='outsider'), self.finding('Retired', owner='retired'),
                                         self.finding('Unknown owner', owner='nobody'), self.finding('No owner'), {'project': 'unknown-page', 'text': 'Lost', 'source': 'leave'}, self.finding('  ')])
        self.assertEqual({k: v for k, v in result['counts'].items() if v}, {'new': 2, 'skipped': 5})
        self.assertEqual(sorted(i['text'] for i in self.open_of(result)), ['No owner', 'Ok owner'])
        record = self.store.get(next(i['id'] for i in self.open_of(result) if i['text'] == 'Ok owner'))
        assert record is not None
        self.assertEqual((record['owner_id'], record['waiting_on']), ('coordinator', 'lab'))

    def test_a_dry_run_writes_nothing_and_returns_the_reply_the_real_run_gives(self) -> None:
        self.go('2026-11-16', [self.finding('Playdate is missing')])
        self.now += 7 * DAY
        with self.registry._transaction() as db:
            before = list(db.iterdump())
        findings = [self.finding('Playdate is missing'), self.finding('No editor is named')]
        dry = self.go('2026-11-23', findings, dry_run=True)
        with self.registry._transaction() as db:
            self.assertEqual(list(db.iterdump()), before)
        self.assertEqual(dry, self.go('2026-11-23', findings))
        self.assertEqual(dry['counts']['new'], 1)

    def test_the_same_key_twice_writes_once_and_a_different_request_with_it_is_refused(self) -> None:
        findings = [self.finding('Playdate is missing')]
        first = self.go('2026-11-16', findings, key='same')
        with self.registry._transaction() as db:
            before = list(db.iterdump())
        self.now += DAY
        self.assertEqual(self.go('2026-11-16', findings, key='same'), first)
        with self.registry._transaction() as db:
            self.assertEqual(list(db.iterdump()), before)
        refused = observe(self.store, {'run_date': '2026-11-16', 'findings': [], 'idempotency_key': 'same'}, actor='reports', coordinators=lambda project: set())
        assert isinstance(refused, Refused)
        self.assertEqual(refused.rule, 'idempotency_key')

    def test_limits_and_bad_input_are_refused(self) -> None:
        def refused(**values: Any) -> Refused:
            outcome = observe(self.store, {'run_date': '2026-11-16', 'findings': [], 'unavailable': [], 'idempotency_key': 'k', **values}, actor='reports', coordinators=lambda project: set())
            assert isinstance(outcome, Refused)
            return outcome
        many = [self.finding(f'Line {n}') for n in range(501)]
        self.assertEqual(refused(findings=many).rule, 'input')
        self.assertEqual(refused(unavailable=[{'project': self.project, 'source': 'leave'}] * 101).rule, 'input')
        self.assertEqual(refused(run_date='16 Nov').rule, 'input')
        self.assertEqual(refused(findings=[self.finding('x', 'person')]).rule, 'source')
        self.assertEqual(refused(idempotency_key=' ').rule, 'idempotency_key')
        observe(self.store, {'run_date': '2026-11-16', 'findings': many[:500], 'idempotency_key': 'k'}, actor='reports', coordinators=lambda project: set())

    def test_projects_with_both_counts_projects_with_a_scan_concern_and_a_read_finding(self) -> None:
        with self.registry._transaction() as db:
            for project, concern in ((self.project, 't6c_a'), (self.other, 't6c_b')):
                db.execute("INSERT INTO concerns (id, project_id, owner_id, waiting_on, anchor, text, text_key, source, as_of, opened_at, evidence_at, state) VALUES (?, ?, 'coordinator', 'x', 'x', 'x', ?, 'pm_notes_scan', '2026-11-01', 1, 1, 'open')", (concern, project, concern))
        result = self.go('2026-11-16', [self.finding('Waiting on lab', 'pm_notes_read'), {'project': self.other, 'text': 'Playdate is missing', 'source': 'flag_rule'}])
        self.assertEqual(result['counts']['projects_with_both'], 1)

    def test_the_run_closes_nothing_unless_the_switch_is_on(self) -> None:
        self.go('2026-11-16', [self.finding('One'), self.finding('Two', 'leave'), self.finding('Three'), self.finding('Four')])
        with self.registry._transaction() as db:
            db.execute("UPDATE concerns SET manual_override = 1 WHERE text = 'Three'")
        self.now += 7 * DAY
        off = self.go('2026-11-23', [])
        self.assertEqual((off['counts']['not_seen'], off['counts']['closed'], len(self.open_of(off))), (4, 0, 4))
        on = self.go('2026-11-23', [], [{'project': self.project, 'source': 'leave'}], closes=True)
        self.assertEqual({k: v for k, v in on['counts'].items() if v}, {'not_seen': 3, 'unavailable': 1, 'closed': 2})
        [project] = on['projects']
        self.assertEqual(sorted(i['text'] for i in project['open']), ['Three', 'Two'])
        self.assertEqual(sorted(i['text'] for i in project['closed_since_last_run']), ['Four', 'One'])
        closed = self.store.get(project['closed_since_last_run'][0]['id'])
        assert closed is not None
        self.assertEqual((closed['state'], closed['closed_at']), ('closed', self.now))
        self.assertTrue(any(a.actor == 'Sixto (auto)' and a.action == 'concern_auto_resolve' for a in self.registry.activity(self.project)))

    def test_one_audit_row_for_each_real_run_holds_the_counts_and_no_text(self) -> None:
        self.go('2026-11-16', [self.finding('Client Corp has not replied')], dry_run=True)
        self.go('2026-11-16', [self.finding('Client Corp has not replied')])
        with self.registry._transaction() as db:
            rows = db.execute("SELECT actor, before, after FROM audit WHERE action = 'concern_observe'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0][2])['counts']['new'], 1)
        self.assertNotIn('Client Corp', str(rows))
        with self.registry._transaction() as db:
            self.assertNotIn('Client Corp', str(db.execute("SELECT * FROM activity WHERE action IN ('concern_observe', 'concern_auto_raise')").fetchall()))

    def test_a_close_the_previous_run_already_listed_is_not_listed_again(self) -> None:
        ids = {i['text']: i['id'] for i in self.open_of(self.go('2026-11-09', [self.finding('Early'), self.finding('Late')]))}
        self.now += 7 * DAY - 3600  # the morning of the next run day, before that run
        self.close(ids['Early'])
        self.now += 3600
        self.assertEqual([i['text'] for i in self.go('2026-11-16', [])['projects'][0]['closed_since_last_run']], ['Early'])
        self.now += 3600  # the same day, after the run
        self.close(ids['Late'])
        self.now += 7 * DAY
        self.assertEqual([i['text'] for i in self.go('20261123', [])['projects'][0]['closed_since_last_run']], ['Late'])

    def test_a_compact_run_date_is_stored_as_an_iso_date(self) -> None:
        result = self.go('20261116', [self.finding('One')])
        self.assertEqual((result['run_date'], self.open_of(result)[0]['opened_on']), ('2026-11-16', '2026-11-16'))

    def test_closed_since_last_run_starts_at_the_previous_run_date_and_with_none_seven_days_back(self) -> None:
        item = self.open_of(self.go('2026-11-16', [self.finding('One')]))[0]
        with self.registry._transaction() as db:
            db.execute('UPDATE concerns SET state = ?, closed_at = ? WHERE id = ?', ('closed', manila('2026-11-09') - 1, item['id']))
        self.assertEqual(self.go('2026-11-16', [], dry_run=True)['projects'], [])
        with self.registry._transaction() as db:
            db.execute('UPDATE concerns SET closed_at = ? WHERE id = ?', (manila('2026-11-09'), item['id']))
        self.assertEqual(len(self.go('2026-11-16', [], dry_run=True)['projects'][0]['closed_since_last_run']), 1)


class ObserveApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.notion = FakeNotion()
        for source in ('projects', 'contacts'):
            self.notion.add_snapshot_data_source(source, source)
        registry = SqliteProjectRegistry(self.tmp.name + '/registry')
        self.registry = registry
        operator = 't6n_' + '1' * 32
        with registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES (?, 'staff', '[]', '[]', NULL, 0, 1)", (operator,))
            db.execute("INSERT INTO person_permissions VALUES (?, 'operator')", (operator,))
        self.api = VerbAPI(People(registry, PreviewTokens('key'), {'reports': frozenset({'coordinator'}), 'watchdog': frozenset({'coordinator', 'unmasked'})}), JobQueue(self.tmp.name + '/queue'),
                           {'secret': 'reports', 'real': 'watchdog', 'plain': 'munki', 'operator': operator}, lambda _: self.notion.client(),
                           privacy_registry=Registry(people=[Person(name='Client Corp')]), data_sources={'projects': 'projects', 'contacts': 'contacts'})
        env = patch.dict(os.environ, {'T6_NOTION_TOKEN': 'fake'})
        env.start()
        self.addCleanup(env.stop)
        self.server = make_server(('127.0.0.1', 0), self.api)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(self.server.shutdown)
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        page = self.notion.add_page('projects', {'Name': title('Observed')})
        registered = registry.register(actor='setup', idempotency_key='observed', notion_page_id=page['id'])
        assert isinstance(registered, Registered)
        self.project = registered.project.id

    def observe(self, secret: str, **more: Any) -> Any:
        inputs = ConcernObserveInput.model_validate({'run_date': '2026-11-16', 'findings': [{'project': self.project, 'text': 'Client Corp has not replied', 'source': 'pm_notes_read'}], 'idempotency_key': 'api-run', **more})
        return Client(self.url, secret).concern_observe(inputs)

    def test_the_reply_is_masked_without_real_name_access_and_the_log_line_holds_counts_only(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            masked = self.observe('secret')
        assert isinstance(masked, Done)
        self.assertNotIn('Client Corp', str(masked.result))
        self.assertEqual(masked.result['counts']['new'], 1)
        self.assertIn('t6.server: concern observe run_date=2026-11-16 new=1 known=0 not_seen=0 unavailable=0 skipped=0 projects_with_both=0 closed=0', err.getvalue())
        self.assertNotIn('Client', err.getvalue())
        self.assertNotIn(self.project, err.getvalue())
        real = self.observe('real', idempotency_key='second')
        assert isinstance(real, Done)
        self.assertIn('Client Corp has not replied', str(real.result))
        self.assertEqual(real.result['counts']['known'], 1)

    def test_the_verb_refuses_a_long_findings_list_and_a_service_without_the_coordinator_role(self) -> None:
        self.assertIsInstance(self.observe('plain', idempotency_key='plain'), Refused)
        findings = [{'project': self.project, 'text': f'Line {n}', 'source': 'leave'} for n in range(501)]
        long = self.observe('secret', findings=findings, idempotency_key='long')
        assert isinstance(long, Refused)
        self.assertEqual(long.rule, 'input')

    def test_the_server_switch_decides_whether_a_run_closes(self) -> None:
        self.observe('real')
        self.api.concern_observe_closes = True
        result = self.observe('real', findings=[], idempotency_key='empty')
        assert isinstance(result, Done)
        self.assertEqual(result.result['counts']['closed'], 1)


class ObserveCliTests(unittest.TestCase):
    def test_the_command_reads_the_findings_file_and_calls_the_verb(self) -> None:
        from contextlib import redirect_stdout
        from pathlib import Path
        from t6.cli import main
        calls: list[Any] = []

        def call(_client: Any, noun: str, verb: str, inputs: Any) -> Any:
            calls.append((noun, verb, inputs))
            return Done(result={'run_date': inputs.run_date, 'projects': [], 'counts': {}})

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'findings.json'
            path.write_text(json.dumps({'run_date': '2026-11-16', 'findings': [{'project': 'p', 'text': 'No editor', 'source': 'leave'}]}))
            with patch('t6.client.Client.call', call), redirect_stdout(io.StringIO()):
                self.assertEqual(main(['concern', 'observe', '--file', str(path), '--dry-run', '--idempotency-key', 'k']), 0)
        [(noun, verb, inputs)] = calls
        self.assertEqual((noun, verb, inputs.dry_run, inputs.idempotency_key, inputs.findings[0].source), ('concern', 'observe', True, 'k', 'leave'))
