"""Pending closes decided at the next PSR, and the Closer setting (#639)."""
import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime
from typing import Any
from unittest.mock import patch

from t6.domain.studio_time import MANILA
from t6.ids import EntityKind, T6Id
from t6.preview import PreviewTokens
from t6.registry import Registered
from t6.verbs import Done
from test_person import MemoryRegistry

DAY = 86400.0
CLOSER, COORDINATOR = 102, 101


def manila(text: str) -> float:
    return datetime.fromisoformat(text).replace(tzinfo=MANILA).timestamp()


class PendingCloseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = MemoryRegistry()
        self.addCleanup(self.registry.db.close)
        self.now = manila('2026-11-24T10:00')  # a Tuesday
        self.registry._clock = lambda: self.now
        self.registry._new_id = lambda: str(T6Id.mint(EntityKind.PROJECT))
        result = self.registry.register(actor='setup', idempotency_key='setup', notion_page_id='project')
        assert isinstance(result, Registered)
        self.project = result.project.id
        with self.registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES ('t6n_coordinator', 'staff', '[]', '[]', ?, 0, 1)", (COORDINATOR,))
            db.execute("INSERT INTO persons VALUES ('t6n_closer', 'staff', '[]', '[]', ?, 0, 1)", (CLOSER,))
            db.execute("UPDATE projects SET rotation = 'in' WHERE id = ?", (self.project,))
            for person in ('t6n_coordinator', 't6n_closer'):
                db.execute("INSERT INTO person_permissions VALUES (?, 'coordinator')", (person,))
        self.api = self.make_api()
        self.keys = 0

    def make_api(self) -> Any:
        from t6.person import People
        from t6.privacy import Registry
        from t6.server import ServerConfig, WebhookApp
        from t6.server.queue import JobQueue
        from t6.server.verbs import VerbAPI
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        queue = JobQueue(tmp.name + '/queue')
        return VerbAPI(People(self.registry, PreviewTokens('secret')), queue, {'gateway': 'gateway', 'unmasked': 'watchdog'}, lambda _: None,
                       WebhookApp(queue, ServerConfig(), registry=self.registry), llm_service=object(),
                       privacy_registry=Registry(), closers=frozenset({CLOSER}))

    def call(self, path: str, body: dict[str, Any], telegram_id: int = CLOSER) -> dict[str, Any]:
        """The gateway acting as a Person: its secret plus the Person's Telegram user id."""
        from t6.server.app import Request
        headers = {'Authorization': 'Bearer gateway', 'X-Telegram-User-Id': str(telegram_id)}
        with patch.dict('os.environ', {'T6_PREVIEW_SIGNING_KEY': 'secret'}):
            return dict(self.api(Request('POST', path, headers, json.dumps(body).encode())).body)

    def confirmed(self, verb: str, concern: str, telegram_id: int = CLOSER) -> dict[str, Any]:
        """Preview, then Confirm with the preview's token and the same idempotency key."""
        self.keys += 1
        body = {'concern': concern, 'idempotency_key': f'{verb}-{self.keys}'}
        preview = self.call('/v1/concern/' + verb, body, telegram_id)
        if preview['outcome'] != 'preview':
            return preview
        return self.call('/v1/concern/' + verb, {**body, 'confirm': preview['token']}, telegram_id)

    def raise_concern(self, text: str = 'Waiting on client for KDM', waiting: str = 'client') -> str:
        from t6.concern import ConcernStore
        store = ConcernStore(self.registry, PreviewTokens('secret'))
        values = {'project': self.project, 'owner': 't6n_coordinator', 'waiting_on': waiting, 'anchor': text, 'as_of': '2026-11-20'}
        preview = store.write('raise', values, actor='t6n_coordinator', idempotency_key='raise-' + text)
        done = store.write('raise', values, actor='t6n_coordinator', idempotency_key='raise-' + text, confirm=preview.token)  # type: ignore[union-attr]
        assert isinstance(done, Done)
        return str(done.result['concern_id'])

    def concern(self, concern: str) -> tuple[str, int]:
        with self.registry._transaction() as db:
            row = db.execute('SELECT state, manual_override FROM concerns WHERE id = ?', (concern,)).fetchone()
        return row[0], row[1]

    def autoscan(self, at: str, lines: list[dict[str, str]] | None = None, answer: dict[str, Any] | None = None) -> str:
        """The autoscan run for the 21:00 slot `at`, reading `lines` with the classifier giving `answer`."""
        self.now = manila(at)
        err = io.StringIO()
        with patch.object(self.api, '_coordinators', return_value={'t6n_coordinator'}), \
                patch.object(self.api, '_typed_read', return_value=Done(result={'lines': lines or []})), \
                patch('t6.concern_task.classify', return_value=answer), contextlib.redirect_stderr(err):
            self.assertTrue(self.api.autoscan(self.now))
        return err.getvalue()

    def listed(self, mode: str = 'open') -> dict[str, Any]:
        from t6.server.app import Request
        body = json.dumps({mode: True, 'project': self.project}).encode()
        return dict(dict(self.api(Request('POST', '/v1/concern/list', {'Authorization': 'Bearer unmasked'}, body)).body)['result'])

    def test_a_close_then_silence_is_closed_by_the_monday_run_with_the_override(self) -> None:
        concern = self.raise_concern()
        done = self.confirmed('resolve', concern)
        self.assertEqual(done['outcome'], 'done')
        self.assertEqual(self.concern(concern), ('open', 0))
        self.autoscan('2026-11-29T21:00')  # Sunday: nothing is applied
        self.assertEqual(self.concern(concern), ('open', 0))
        log = self.autoscan('2026-11-30T21:00')  # Monday
        self.assertEqual(self.concern(concern), ('closed', 1))
        self.assertTrue(log.endswith(' applied=1\n'), log)
        self.assertEqual(self.listed()['pending'], [])

    def test_a_continuing_line_before_the_run_is_a_veto(self) -> None:
        concern = self.raise_concern()
        self.confirmed('resolve', concern)
        self.autoscan('2026-11-30T21:00', [{'as_of': '2026-11-30', 'text': 'KDM still not in from client'}],
                 {'choice': 'continues', 'concern_id': concern, 'quote': 'KDM still not in from client'})
        self.assertEqual(self.concern(concern), ('open', 0))
        self.assertEqual(self.listed()['pending'], [])
        self.assertEqual([c['id'] for c in self.listed()['concerns']], [concern])

    def test_a_close_from_the_sheet_lists_the_email_as_the_close_by_name(self) -> None:
        concern = self.raise_concern()
        with self.registry._transaction() as db:
            db.execute("INSERT INTO concern_pending_closes (concern_id, actor, source, created_at) VALUES (?, 'sheet:cyril@example.com', 'close', ?)", (concern, self.now))
        [pending] = self.listed()['pending']
        self.assertEqual((pending['close_by'], pending['close_by_name']), ('sheet:cyril@example.com', 'cyril@example.com'))

    def test_a_veto_still_works_on_a_concern_a_person_reopened(self) -> None:
        concern = self.raise_concern()
        self.confirmed('resolve', concern)
        self.autoscan('2026-11-30T21:00')
        self.assertEqual(self.confirmed('reopen', concern)['outcome'], 'done')  # reopened by hand: manual_override stays set
        self.confirmed('resolve', concern)
        self.autoscan('2026-12-07T21:00', [{'as_of': '2026-12-07', 'text': 'KDM still not in from client'}],
                 {'choice': 'continues', 'concern_id': concern, 'quote': 'KDM still not in from client'})
        self.assertEqual(self.concern(concern)[0], 'open')

    def test_a_close_after_monday_08_30_waits_for_the_following_monday(self) -> None:
        concern = self.raise_concern()
        self.now = manila('2026-11-30T10:00')
        self.confirmed('resolve', concern)
        self.autoscan('2026-11-30T21:00')
        self.assertEqual(self.concern(concern), ('open', 0))
        self.autoscan('2026-12-07T21:00')
        self.assertEqual(self.concern(concern), ('closed', 1))

    def test_a_failed_monday_run_applies_nothing_and_the_next_run_that_completes_does(self) -> None:
        from t6.verbs import Refused
        concern = self.raise_concern()
        self.confirmed('resolve', concern)
        self.now = manila('2026-11-30T21:00')

        class Refusing:
            def run(self, inputs: object, *, actor: str, registry: object) -> Refused:
                return Refused(rule='masking', reason='refused')
        working, self.api.llm_service = self.api.llm_service, Refusing()
        lines = Done(result={'lines': [{'as_of': '2026-11-30', 'text': 'A new line'}]})
        with patch.object(self.api, '_coordinators', return_value={'t6n_coordinator'}), patch.object(self.api, '_typed_read', return_value=lines), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(self.api.autoscan(self.now))
        self.assertEqual(self.concern(concern), ('open', 0))
        self.api.llm_service = working
        self.autoscan('2026-12-01T21:00')  # Tuesday: the run completes and applies what Monday left
        self.assertEqual(self.concern(concern), ('closed', 1))

    def test_a_done_line_is_a_pending_close_too(self) -> None:
        concern = self.raise_concern()
        settles = {'choice': 'settles', 'concern_id': concern, 'quote': 'Client sent KDM'}
        self.autoscan('2026-11-25T21:00', [{'as_of': '2026-11-25', 'text': 'Client sent KDM'}], settles)
        self.assertEqual(self.concern(concern), ('open', 0))
        [pending] = self.listed()['pending']
        self.assertEqual((pending['close_source'], pending['close_by'], pending['close_by_name'], pending['close_quote']), ('done_line', None, None, 'Client sent KDM'))
        self.autoscan('2026-11-30T21:00')
        self.assertEqual(self.concern(concern), ('closed', 0))

    def test_a_done_line_after_monday_08_30_waits_and_a_continuing_line_vetoes_it(self) -> None:
        late, vetoed = self.raise_concern(), self.raise_concern('Waiting on lab for the DCP', 'lab')
        self.autoscan('2026-11-30T21:00', [{'as_of': '2026-11-30', 'text': 'Client sent KDM'}], {'choice': 'settles', 'concern_id': late, 'quote': 'Client sent KDM'})
        self.assertEqual(self.concern(late)[0], 'open')  # read at 21:00 Monday, after that PSR started
        self.autoscan('2026-12-01T21:00', [{'as_of': '2026-12-01', 'text': 'Lab delivered the DCP'}], {'choice': 'settles', 'concern_id': vetoed, 'quote': 'Lab delivered the DCP'})
        self.autoscan('2026-12-03T21:00', [{'as_of': '2026-12-03', 'text': 'Lab DCP failed QC, waiting on lab'}], {'choice': 'continues', 'concern_id': vetoed, 'quote': 'Lab DCP failed QC, waiting on lab'})
        self.autoscan('2026-12-07T21:00')
        self.assertEqual((self.concern(late)[0], self.concern(vetoed)[0]), ('closed', 'open'))

    def test_only_a_closer_may_close_and_undo_cancels(self) -> None:
        concern = self.raise_concern()
        refused = self.confirmed('resolve', concern, telegram_id=COORDINATOR)
        self.assertEqual((refused['outcome'], refused['rule']), ('refused', 'closer'))
        self.confirmed('resolve', concern)
        again = self.confirmed('resolve', concern)
        self.assertEqual((again['outcome'], again['rule']), ('refused', 'state'))
        undone = self.confirmed('reopen', concern)
        self.assertEqual(undone['outcome'], 'done')
        self.assertEqual(self.listed()['pending'], [])
        self.autoscan('2026-11-30T21:00')
        self.assertEqual(self.concern(concern), ('open', 0))
        nothing = self.confirmed('reopen', concern)
        self.assertEqual((nothing['outcome'], nothing['rule']), ('refused', 'state'))

    def autoscan_with(self, at: str, read: object, answer: dict[str, Any] | None = None) -> None:
        self.now = manila(at)
        with patch.object(self.api, '_coordinators', return_value={'t6n_coordinator'}), patch.object(self.api, '_typed_read', return_value=read), \
                patch('t6.concern_task.classify', return_value=answer), contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(self.api.autoscan(self.now))

    def test_a_close_stays_pending_while_its_projects_notes_were_not_read_in_the_run(self) -> None:
        from t6.verbs import Refused
        concern = self.raise_concern()
        self.confirmed('resolve', concern)
        self.autoscan_with('2026-11-30T21:00', Refused(rule='notion', reason='read failed'))
        self.assertEqual(self.concern(concern), ('open', 0))
        self.assertEqual(len(self.listed()['pending']), 1)
        self.autoscan('2026-12-01T21:00')  # the next run that reads the notes applies it
        self.assertEqual(self.concern(concern), ('closed', 1))

    def test_a_close_stays_pending_while_a_line_of_its_project_was_left_unread(self) -> None:
        concern = self.raise_concern()
        self.confirmed('resolve', concern)
        line = Done(result={'lines': [{'as_of': '2026-11-30', 'text': 'Lab says still waiting'}]})
        self.autoscan_with('2026-11-30T21:00', line, None)  # no usable answer: the line is tried again next run
        self.assertEqual(self.concern(concern), ('open', 0))
        self.autoscan('2026-12-01T21:00', [{'as_of': '2026-11-30', 'text': 'Lab says still waiting'}], {'choice': 'nothing', 'quote': 'Lab says still waiting'})
        self.assertEqual(self.concern(concern), ('closed', 1))

    def test_reopening_a_quiet_concern_with_no_pending_close_opens_it_with_the_override(self) -> None:
        concern = self.raise_concern()
        with self.registry._transaction() as db:
            db.execute("UPDATE concerns SET state = 'quiet' WHERE id = ?", (concern,))
        self.assertEqual(self.confirmed('reopen', concern)['outcome'], 'done')
        self.assertEqual(self.concern(concern), ('open', 1))

    def settles(self, concern: str) -> str | None:
        with self.registry._transaction() as db:
            return db.execute('SELECT settles_id FROM concerns WHERE id = ?', (concern,)).fetchone()[0]  # type: ignore[no-any-return]

    def test_the_settlement_of_a_close_is_set_only_when_it_is_applied(self) -> None:
        job = str(T6Id.mint(EntityKind.JOB_ORDER))
        applied, undone = self.raise_concern(), self.raise_concern('Waiting on lab for the DCP', 'lab')
        for concern in (applied, undone):
            self.keys += 1
            body = {'concern': concern, 'settles_id': job, 'idempotency_key': f'r{self.keys}'}
            self.call('/v1/concern/resolve', {**body, 'confirm': self.call('/v1/concern/resolve', body)['token']})
        self.assertEqual((self.settles(applied), self.settles(undone)), (None, None))
        self.confirmed('reopen', undone)
        self.autoscan('2026-11-30T21:00')
        self.assertEqual((self.settles(applied), self.settles(undone)), (job, None))

    def test_only_a_closer_may_undo_a_pending_close(self) -> None:
        concern = self.raise_concern()
        self.confirmed('resolve', concern)
        refused = self.confirmed('reopen', concern, telegram_id=COORDINATOR)
        self.assertEqual((refused['outcome'], refused['rule']), ('refused', 'closer'))
        self.assertEqual(len(self.listed()['pending']), 1)
        self.autoscan('2026-11-30T21:00')
        self.assertEqual(self.confirmed('reopen', concern, telegram_id=COORDINATOR)['outcome'], 'done')  # a plain reopen keeps the role rules

    def test_a_data_close_stays_immediate_and_a_date_rule_concern_refuses_resolve(self) -> None:
        from t6.concern import ConcernStore
        store = ConcernStore(self.registry, PreviewTokens('secret'))
        ids = store.reconcile_dates(self.project, 't6n_coordinator', {'playdate': '2026-12-10', 'submission_deadline': '2026-12-01', 'archival_deadline': None})
        rule = ids['submission_before_playdate']
        refused = self.confirmed('resolve', rule)
        self.assertEqual((refused['outcome'], refused['rule']), ('refused', 'rule_made'))
        store.reconcile_dates(self.project, 't6n_coordinator', {'playdate': '2026-12-10', 'submission_deadline': '2026-12-11', 'archival_deadline': None})
        self.assertEqual(self.concern(rule)[0], 'closed')

    def test_a_data_close_of_a_pending_concern_drops_its_pending_close(self) -> None:
        from t6.concern import ConcernStore
        store = ConcernStore(self.registry, PreviewTokens('secret'))
        values = {'project': self.project, 'owner': 't6n_coordinator', 'waiting_on': 'client', 'anchor': 'Waiting on client for the playdate', 'as_of': '2026-11-20', 'settles_date': 'playdate'}
        preview = store.write('raise', values, actor='t6n_coordinator', idempotency_key='awaited')
        concern = store.write('raise', values, actor='t6n_coordinator', idempotency_key='awaited', confirm=preview.token).result['concern_id']  # type: ignore[union-attr]
        self.confirmed('resolve', concern)
        store.settle_dates(self.project, {'playdate': '2026-12-10'})
        self.assertEqual(self.concern(concern), ('closed', 0))
        self.assertEqual(self.listed()['pending'], [])

    def test_concern_list_puts_a_pending_concern_only_in_the_pending_list(self) -> None:
        kept, closing = self.raise_concern(), self.raise_concern('Waiting on lab for the DCP', 'lab')
        self.now += 60
        self.confirmed('resolve', closing)
        for mode in ('open', 'raise_at_psr'):
            listed = self.listed(mode)
            self.assertEqual([c['id'] for c in listed['concerns']], [kept], mode)
            [pending] = listed['pending']
            self.assertEqual(pending['id'], closing)
            self.assertEqual(sorted(pending), sorted(['id', 'project_id', 'owner_id', 'waiting_on', 'text', 'source', 'anchor', 'as_of', 'opened_at', 'evidence_at', 'state', 'settles_id',
                                                      'age_seconds', 'chronic', 'close_by', 'close_by_name', 'close_at', 'close_source', 'close_quote']))
            self.assertEqual((pending['text'], pending['project_id'], pending['owner_id'], pending['waiting_on']), ('Waiting on lab for the DCP', self.project, 't6n_coordinator', 'lab'))
            self.assertEqual((pending['close_by'], pending['close_at'], pending['close_source'], pending['close_quote']), ('t6n_closer', self.now, 'close', None))


if __name__ == '__main__':
    unittest.main()
