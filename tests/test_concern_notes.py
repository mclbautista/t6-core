"""PM Notes automation through the Concern processing boundary."""
import unittest
from datetime import datetime
from typing import Any

from t6.concern_scan import ConcernScanner
from t6.concern import ConcernStore, WEEK
from t6.domain.studio_time import MANILA
from t6.ids import EntityKind, T6Id
from t6.preview import PreviewTokens
from t6.registry import Registered
from t6.verbs import Done, Preview, Refused
from test_person import MemoryRegistry


class ConcernNotesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = MemoryRegistry()
        self.addCleanup(self.registry.db.close)
        self.now = 1800000000.0
        self.registry._clock = lambda: self.now
        self.registry._new_id = lambda: str(T6Id.mint(EntityKind.PROJECT))
        result = self.registry.register(actor='setup', idempotency_key='setup', notion_page_id='project')
        assert isinstance(result, Registered)
        self.project = result.project.id
        with self.registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES ('coordinator', 'staff', '[]', '[]', NULL, 0, 1)")
        self.store = ConcernStore(self.registry, PreviewTokens('secret'))

    def first_written(self, day: str) -> float:
        return datetime.fromisoformat(day).replace(tzinfo=MANILA).timestamp()

    def concern(self, identifier: str) -> dict[str, Any]:
        record = self.store.get(identifier)
        assert record is not None
        return record

    def process(self, line: str, when: str, answer: dict[str, Any]) -> None:
        ConcernScanner(self.store).process_notes(self.project, 'coordinator', [{'text': line, 'as_of': when}], lambda _: answer)

    def test_announce_then_close_and_person_reopens(self) -> None:
        self.process('Waiting on client for KDM', '2026-11-01', {'choice': 'new', 'quote': 'Waiting on client for KDM', 'waiting_on': 'client'})
        changes = ConcernScanner(self.store).changes(self.project, '2026-01-01')
        concern = changes[0]['concern_id']
        self.process('Client sent KDM', '2026-11-08', {'choice': 'settles', 'concern_id': concern, 'quote': 'Client sent KDM'})
        self.assertEqual(self.concern(concern)['state'], 'open')
        changes = ConcernScanner(self.store).changes(self.project, '2026-01-01')
        self.assertEqual([c['action'] for c in changes], ['concern_auto_raise', 'concern_auto_announce'])
        self.assertEqual(self.store.pending_closes()[0]['close_source'], 'done_line')
        self.now += WEEK
        ConcernScanner(self.store).apply_pending(self.now - WEEK)  # a PSR that started when the line was read does not decide it
        self.assertEqual(self.concern(concern)['state'], 'open')
        ConcernScanner(self.store).apply_pending(self.now)
        self.assertEqual(self.concern(concern)['state'], 'closed')
        values = {'concern': concern}
        preview = self.store.write('reopen', values, actor='person', idempotency_key='undo')
        assert isinstance(preview, Preview)
        self.assertIsInstance(self.store.write('reopen', values, actor='person', idempotency_key='undo', confirm=preview.token), Done)
        self.assertEqual(self.concern(concern)['state'], 'open')
        self.assertEqual(len(ConcernScanner(self.store).changes(self.project, '2026-01-01')), 3)

    def test_bad_answers_and_old_evidence_do_not_change_concerns(self) -> None:
        self.process('Waiting on client', '2026-11-01', {'choice': 'new', 'quote': 'Waiting on client', 'waiting_on': 'client'})
        concern = ConcernScanner(self.store).changes(self.project, '2026-01-01')[0]['concern_id']
        self.process('Client delivered', '2026-11-08', {'choice': 'settles', 'concern_id': 'invented', 'quote': 'Client delivered'})
        self.process('Client delivered', '2026-11-08', {'choice': 'settles', 'concern_id': concern, 'quote': 'fabricated'})
        self.process('Client delivered', '2026-10-31', {'choice': 'settles', 'concern_id': concern, 'quote': 'Client delivered'})
        self.assertEqual(len(ConcernScanner(self.store).changes(self.project, '2026-01-01')), 1)
        self.assertEqual(self.concern(concern)['state'], 'open')

    def test_person_can_reject_announcement_before_close(self) -> None:
        self.process('Waiting on client', '2026-11-01', {'choice': 'new', 'quote': 'Waiting on client', 'waiting_on': 'client'})
        concern = ConcernScanner(self.store).changes(self.project, '2026-01-01')[0]['concern_id']
        self.process('Client delivered', '2026-11-08', {'choice': 'settles', 'concern_id': concern, 'quote': 'Client delivered'})
        preview = self.store.write('reopen', {'concern': concern}, actor='person', idempotency_key='reject')
        assert isinstance(preview, Preview)
        self.assertIsInstance(self.store.write('reopen', {'concern': concern}, actor='person', idempotency_key='reject', confirm=preview.token), Done)
        self.now += WEEK
        ConcernScanner(self.store).apply_pending(self.now)
        self.assertEqual(self.concern(concern)['state'], 'open')

    def test_reworded_line_continues_with_same_id_and_age(self) -> None:
        self.process('Waiting on client for KDM', '2026-11-01', {'choice': 'new', 'quote': 'Waiting on client for KDM', 'waiting_on': 'client'})
        concern = ConcernScanner(self.store).changes(self.project, '2026-01-01')[0]['concern_id']
        self.now += 100
        self.process('KDM still pending from client', '2026-11-08', {'choice': 'continues', 'concern_id': concern, 'quote': 'KDM still pending from client'})
        self.assertEqual(self.concern(concern)['age_seconds'], self.now - self.first_written('2026-11-01'))
        self.assertEqual([c['concern_id'] for c in ConcernScanner(self.store).changes(self.project, '2026-01-01')], [concern, concern])

    def test_same_line_on_a_newer_date_is_fresh_evidence_and_never_a_duplicate(self) -> None:
        self.process('Waiting on client', '2026-11-01', {'choice': 'new', 'quote': 'Waiting on client', 'waiting_on': 'client'})
        concern = ConcernScanner(self.store).changes(self.project, '2026-01-01')[0]['concern_id']
        self.process('Waiting on client', '2026-11-01', {'choice': 'new', 'quote': 'Waiting on client', 'waiting_on': 'client'})
        self.assertEqual(len(ConcernScanner(self.store).changes(self.project, '2026-01-01')), 1)
        self.now += 100
        self.process('Waiting on client', '2026-11-08', {'choice': 'new', 'quote': 'Waiting on client', 'waiting_on': 'client'})
        changes = ConcernScanner(self.store).changes(self.project, '2026-01-01')
        self.assertEqual([(c['concern_id'], c['action']) for c in changes], [(concern, 'concern_auto_raise'), (concern, 'concern_auto_continue')])
        record = self.concern(concern)
        self.assertEqual((record['as_of'], record['evidence_at'], record['age_seconds']), ('2026-11-08', self.now, self.now - self.first_written('2026-11-01')))
        self.process('Waiting on client', '2026-11-08', {'choice': 'new', 'quote': 'Waiting on client', 'waiting_on': 'client'})
        self.assertEqual(len(ConcernScanner(self.store).changes(self.project, '2026-01-01')), 2)
        with self.registry._transaction() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM concerns').fetchone()[0], 1)

    def test_a_concern_raised_from_an_old_line_is_as_old_as_the_line(self) -> None:
        self.process('Waiting on client', '2026-11-01', {'choice': 'new', 'quote': 'Waiting on client', 'waiting_on': 'client'})
        concern = ConcernScanner(self.store).changes(self.project, '2026-01-01')[0]['concern_id']
        self.assertEqual(self.concern(concern)['opened_at'], self.first_written('2026-11-01'))
        self.process('Waiting on lab', '2999-01-01', {'choice': 'new', 'quote': 'Waiting on lab', 'waiting_on': 'lab'})
        newest = ConcernScanner(self.store).changes(self.project, '2026-01-01')[-1]['concern_id']
        self.assertEqual(self.concern(newest)['opened_at'], self.now)

    def test_a_rejected_answer_is_asked_three_times_and_an_unavailable_llm_forever(self) -> None:
        calls = []
        def bad(_: Any) -> Any:
            calls.append(1)
            return {'choice': 'new', 'quote': 'Waiting on client', 'waiting_on': 'nobody'}
        line = [{'text': 'Waiting on client', 'as_of': '2026-11-01'}]
        for _ in range(6):
            ConcernScanner(self.store).process_notes(self.project, 'coordinator', line, bad)
        self.assertEqual(len(calls), 3)
        calls.clear()
        other = [{'text': 'Waiting on lab', 'as_of': '2026-11-01'}]
        for _ in range(6):
            ConcernScanner(self.store).process_notes(self.project, 'coordinator', other, lambda _: calls.append(1))
        self.assertEqual(len(calls), 6)
        self.assertEqual(ConcernScanner(self.store).changes(self.project, '2026-01-01'), [])

    def test_an_invalid_llm_reply_is_asked_three_times_not_forever(self) -> None:
        # The reply failed the schema (not refused): it counts as a rejected answer, so the daily scan stops after MAX_ATTEMPTS (#504).
        from types import SimpleNamespace
        from t6.concern_task import classify
        calls: list[int] = []

        class Invalid:
            def run(self, inputs: object, *, actor: str, registry: object) -> Done:
                calls.append(1)
                return Done(result=SimpleNamespace(output=None, output_valid=False))
        line = [{'text': 'Waiting on client', 'as_of': '2026-11-01'}]
        for _ in range(6):
            ConcernScanner(self.store).process_notes(self.project, 'coordinator', line, lambda payload: classify(Invalid(), payload, actor='coordinator', registry=self.registry))
        self.assertEqual(len(calls), 3)
        self.assertEqual(ConcernScanner(self.store).changes(self.project, '2026-01-01'), [])

    def test_a_line_matched_to_a_concern_with_a_person_decision_is_sent_once(self) -> None:
        self.process('Waiting on client', '2026-11-01', {'choice': 'new', 'quote': 'Waiting on client', 'waiting_on': 'client'})
        concern = ConcernScanner(self.store).changes(self.project, '2026-01-01')[0]['concern_id']
        with self.registry._transaction() as db:
            db.execute('UPDATE concerns SET manual_override = 1 WHERE id = ?', (concern,))
        calls = []
        def answer(_: Any) -> Any:
            calls.append(1)
            return {'choice': 'settles', 'concern_id': concern, 'quote': 'Client delivered'}
        for _ in range(4):
            ConcernScanner(self.store).process_notes(self.project, 'coordinator', [{'text': 'Client delivered', 'as_of': '2026-11-08'}], answer)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.concern(concern)['state'], 'open')

    def test_a_line_that_keeps_missing_its_concern_is_asked_three_times(self) -> None:
        self.process('Waiting on client', '2026-11-01', {'choice': 'new', 'quote': 'Waiting on client', 'waiting_on': 'client'})
        concern = ConcernScanner(self.store).changes(self.project, '2026-01-01')[0]['concern_id']
        calls = []
        def answer(_: Any) -> Any:
            calls.append(1)
            with self.registry._transaction() as db:
                db.execute('UPDATE concerns SET revision = revision + 1 WHERE id = ?', (concern,))
            return {'choice': 'continues', 'concern_id': concern, 'quote': 'Still waiting'}
        for _ in range(6):
            ConcernScanner(self.store).process_notes(self.project, 'coordinator', [{'text': 'Still waiting', 'as_of': '2026-11-08'}], answer)
        self.assertEqual(len(calls), 3)

    def test_no_waiting_cue_rejects_a_false_new_answer(self) -> None:
        self.process('Color looks good', '2026-11-01', {'choice': 'new', 'quote': 'Color looks good', 'waiting_on': 'Color'})
        self.assertEqual(ConcernScanner(self.store).changes(self.project, '2026-01-01'), [])


class ConcernAutoScanTests(ConcernNotesTests):
    def api(self) -> Any:
        import tempfile
        from t6.person import People
        from t6.server import ServerConfig, WebhookApp
        from t6.server.queue import JobQueue
        from t6.server.verbs import VerbAPI
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        queue = JobQueue(tmp.name + '/queue')
        return VerbAPI(People(self.registry, PreviewTokens('secret')), queue, {'gateway': 'gateway'}, lambda _: None, WebhookApp(queue, ServerConfig(), registry=self.registry), llm_service=object())

    def test_scan_reads_each_projects_notes_and_skips_projects_without_a_coordinator(self) -> None:
        from unittest.mock import patch
        api = self.api()
        with self.registry._transaction() as db:
            db.execute("UPDATE projects SET rotation = 'in' WHERE id = ?", (self.project,))
        notes = Done(result={'lines': [{'text': 'Waiting on client', 'as_of': '2026-11-01'}]})
        import contextlib
        import io
        err = io.StringIO()
        with patch.object(api, '_coordinators', return_value=set()), patch.object(api, '_typed_read', return_value=notes), contextlib.redirect_stderr(err):
            self.assertTrue(api.autoscan(self.now))
        self.assertEqual(err.getvalue(), 't6.server: concern scan archived=1 projects=0 skipped=1 raised=0 continued=0 announced=0 applied=0\n')  # counted, never silent (#596)
        answer = {'choice': 'new', 'quote': 'Waiting on client', 'waiting_on': 'client'}
        err = io.StringIO()
        with patch.object(api, '_coordinators', return_value={'coordinator'}), patch.object(api, '_typed_read', return_value=notes), patch('t6.concern_task.classify', return_value=answer), contextlib.redirect_stderr(err):
            self.assertTrue(api.autoscan(self.now))
        self.assertEqual(len(ConcernScanner(self.store).changes(self.project, '2026-01-01')), 1)
        self.assertEqual(err.getvalue(), 't6.server: concern scan archived=1 projects=1 skipped=0 raised=1 continued=0 announced=0 applied=0\n')

    def test_a_refused_llm_run_stops_the_whole_scan_after_one_call(self) -> None:
        # A refusal of the request itself holds for every line; re-sending each line made 51k refused calls on Sixto (#504).
        from unittest.mock import patch
        import contextlib
        import io
        api = self.api()
        with self.registry._transaction() as db:
            db.execute("UPDATE projects SET rotation = 'in' WHERE id = ?", (self.project,))
        notes = Done(result={'lines': [{'text': f'Waiting on client {n}', 'as_of': '2026-11-01'} for n in range(5)]})
        calls: list[object] = []

        class Refusing:
            def run(self, inputs: object, *, actor: str, registry: object) -> Refused:
                calls.append(inputs)
                return Refused(rule='masking', reason='Unresolved registered names in the prompt template')
        api.llm_service = Refusing()
        err = io.StringIO()
        with patch.object(api, '_coordinators', return_value={'coordinator'}), patch.object(api, '_typed_read', return_value=notes), contextlib.redirect_stderr(err):
            self.assertFalse(api.autoscan(self.now))  # not completed: the schedule does not store it
        self.assertEqual(len(calls), 1)
        self.assertEqual(err.getvalue(), 't6.server: concern scan stopped: llm run refused rule=masking archived=1 projects=0\n')
        self.assertEqual(ConcernScanner(self.store).changes(self.project, '2026-01-01'), [])
