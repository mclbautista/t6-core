"""The Concerns sheet export: built and pushed to the VPS after each autoscan, read back through the edge server (#734)."""
import contextlib
import csv
import io
import json
import tempfile
import unittest
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

from t6.concern import _insert
from t6.domain import Project
from t6.domain.studio_time import MANILA
from t6.ids import EntityKind, T6Id
from t6.preview import PreviewTokens
from t6.registry import Registered
from t6.server import Request, ServerConfig, WebhookApp
from t6.server.queue import JobQueue
from t6.verbs import Done, Refused
from test_person import MemoryRegistry

PUSH, TOKEN = 'push-secret', 'read-token'
HEADER = ['Concern id', 'revision', 'Project code', 'Project name', 'Concern text', 'owner', 'waiting on', 'State', 'close requested by', 'close requested at',
          'source', 'quoted line', 'as of', 'last evidence', 'opened', 'days open', 'allowed actions', 'last result']


def manila(text: str) -> float:
    return datetime.fromisoformat(text).replace(tzinfo=MANILA).timestamp()


class FakeVps:
    """The VPS t6 server in edge mode, reached the way the API reaches it: a transport onto its tailnet listener."""

    def __init__(self, directory: str, push_secret: str = PUSH) -> None:
        self.queue = JobQueue(directory + '/vps-queue')
        config = ServerConfig(edge_routes=(), concerns_export_push_secret=push_secret, concerns_export_read_token=TOKEN, concerns_export_file=directory + '/vps/export.csv')
        self.app = WebhookApp(self.queue, config)
        self.pushes = 0
        self.down = False

    def send(self, method: str, path: str, headers: Mapping[str, str], body: bytes) -> tuple[int, dict[str, Any]]:
        self.pushes += 1
        if self.down:
            raise ConnectionRefusedError('vps is down')
        answer = self.app.tailnet(Request(method, '/concerns/export' + path, headers, body))  # the transport is for the push URL
        return answer.status, dict(answer.body)

    def served(self) -> list[dict[str, str]] | None:
        """What a client with the read token gets from the public path, parsed; None when it gets 404."""
        answer = self.app.public(Request('GET', '/concerns/export.csv', {'X-T6-Read-Token': TOKEN}))
        return list(csv.DictReader(io.StringIO(answer.raw.decode()))) if answer.status == 200 and answer.raw is not None else None

    def raw(self) -> str:
        answer = self.app.public(Request('GET', '/concerns/export.csv', {'X-T6-Read-Token': TOKEN}))
        assert answer.raw is not None
        return answer.raw.decode()


class ConcernExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.notion_down = False
        self.registry = MemoryRegistry()
        self.addCleanup(self.registry.db.close)
        self.now = manila('2026-11-24T10:00')  # a Tuesday
        self.registry._clock = lambda: self.now
        self.registry._new_id = lambda: str(T6Id.mint(EntityKind.PROJECT))
        result = self.registry.register(actor='setup', idempotency_key='setup', notion_page_id='project')
        assert isinstance(result, Registered)
        self.project = result.project.id
        with self.registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES ('t6n_coordinator', 'staff', '[]', '[]', 101, 0, 1)")
            db.execute("INSERT INTO persons VALUES ('t6n_closer', 'staff', '[]', '[]', 102, 0, 1)")
            db.execute("UPDATE projects SET rotation = 'in' WHERE id = ?", (self.project,))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.vps = FakeVps(tmp.name)
        self.api = self.make_api(self.vps)

    def make_api(self, vps: FakeVps | None, secret: str = PUSH) -> Any:
        from t6.concern_export import ConcernsExportPush
        from t6.person import People
        from t6.privacy import Registry
        from t6.server.verbs import VerbAPI
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        queue = JobQueue(tmp.name + '/queue')
        push = ConcernsExportPush(vps, secret) if vps is not None else None
        return VerbAPI(People(self.registry, PreviewTokens('secret')), queue, {'gateway': 'gateway'}, lambda _: None,
                       WebhookApp(queue, ServerConfig(), registry=self.registry), llm_service=object(),
                       privacy_registry=Registry(), concerns_export=push)

    def raise_concern(self, text: str, *, owner: str | None = 't6n_coordinator', waiting: str = 'client', as_of: str = '2026-11-20') -> str:
        from t6.concern import ConcernStore
        store = ConcernStore(self.registry, PreviewTokens('secret'))
        values = {'project': self.project, 'owner': owner, 'waiting_on': waiting, 'anchor': text, 'as_of': as_of}
        preview = store.write('raise', values, actor='t6n_coordinator', idempotency_key='raise-' + text)
        assert not isinstance(preview, Refused), preview
        done = store.write('raise', values, actor='t6n_coordinator', idempotency_key='raise-' + text, confirm=preview.token)  # type: ignore[union-attr]
        assert isinstance(done, Done), done
        concern = str(done.result['concern_id'])
        return concern

    def read(self, noun: str, verb: str, inputs: object, seen: object = None) -> Done:
        if (noun, verb) == ('project', 'notes'):
            return Done(result={'lines': []})
        if self.notion_down:
            raise OSError('notion down')
        if (noun, verb) == ('project', 'list'):
            return Done(result={'projects': [Project(page_id='page', name='Big Show', t6_id=self.project, project_code='BS-101')]})
        if (noun, verb) == ('person', 'list'):
            return Done(result={'people': [{'t6_id': 't6n_coordinator', 'name': 'Casey Coordinator'}, {'t6_id': 't6n_closer', 'name': 'Cyril Closer'}, {'t6_id': None, 'name': 'No Binding'}]})
        raise AssertionError((noun, verb))

    def autoscan(self, at: str = '2026-11-24T21:00', *, api: Any = None) -> tuple[bool, str]:
        self.now = manila(at)
        err = io.StringIO()
        api = api or self.api
        with patch.object(api, '_coordinators', return_value={'t6n_coordinator'}), patch.object(api, '_typed_read', side_effect=self.read), contextlib.redirect_stderr(err):
            return api.autoscan(self.now), err.getvalue()

    def pend_close(self, concern: str, actor: str = 'sheet:cyril@example.com', at: str = '2026-11-24T09:00') -> None:
        with self.registry._transaction() as db:
            db.execute("INSERT INTO concern_pending_closes (concern_id, actor, source, created_at) VALUES (?, ?, 'close', ?)", (concern, actor, manila(at)))

    def test_a_completed_autoscan_pushes_one_row_per_open_concern_with_the_columns_and_manila_dates(self) -> None:
        first = self.raise_concern('Waiting on client for KDM')
        with self.registry._transaction() as db:
            db.execute('UPDATE concerns SET opened_at = ?, evidence_at = ? WHERE id = ?', (manila('2026-11-20T23:30'), manila('2026-11-22T00:15'), first))
        completed, _ = self.autoscan()
        self.assertTrue(completed)
        self.assertEqual(self.vps.raw().splitlines()[0], ",".join(HEADER))
        [row] = self.vps.served() or []
        self.assertEqual(row, {
            'Concern id': first, 'revision': '1', 'Project code': 'BS-101', 'Project name': 'Big Show', 'Concern text': 'Waiting on client for KDM',
            'owner': 'Casey Coordinator', 'waiting on': 'client', 'State': 'Open', 'close requested by': '', 'close requested at': '', 'source': 'person',
            'quoted line': 'Waiting on client for KDM', 'as of': '2026-11-20', 'last evidence': '2026-11-22', 'opened': '2026-11-20', 'days open': '3',
            'allowed actions': 'Keep, Close', 'last result': ''})

    def test_a_pending_close_is_closing_after_psr_and_may_be_undone_a_date_rule_concern_may_only_be_kept(self) -> None:
        closing = self.raise_concern('Waiting on client for KDM')
        rule = str(T6Id.mint(EntityKind.CONCERN))
        with self.registry._transaction() as db:
            _insert(db, rule, self.project, None, None, 'Playdate is before the deadline', '2026-11-20', manila('2026-11-20T10:00'), manila('2026-11-20T10:00'), 'date_rule')
        self.pend_close(closing)
        self.autoscan()
        rows = {row['Concern id']: row for row in self.vps.served() or []}
        self.assertEqual({key: rows[closing][key] for key in ('State', 'close requested by', 'close requested at', 'allowed actions')},
                         {'State': 'Closing after PSR', 'close requested by': 'sheet:cyril@example.com', 'close requested at': '2026-11-24', 'allowed actions': 'Keep, Undo close'})
        self.assertEqual({key: rows[rule][key] for key in ('State', 'owner', 'waiting on', 'allowed actions', 'source')},
                         {'State': 'Open', 'owner': '', 'waiting on': '', 'allowed actions': 'Keep', 'source': 'date_rule'})

    def test_a_close_requested_by_a_person_shows_the_name(self) -> None:
        concern = self.raise_concern('Waiting on client for KDM')
        self.pend_close(concern, actor='t6n_closer')
        self.autoscan()
        [row] = self.vps.served() or []
        self.assertEqual(row['close requested by'], 'Cyril Closer')

    def test_closed_concerns_are_absent_and_pending_closes_are_applied_before_the_push(self) -> None:
        closing, kept = self.raise_concern('Waiting on client for KDM'), self.raise_concern('Waiting on lab for the DCP', waiting='lab')
        self.pend_close(closing)
        completed, log = self.autoscan('2026-11-30T21:00')  # the Monday run applies the pending close first
        self.assertTrue(completed)
        self.assertTrue(log.endswith(' applied=1\n'), log)
        self.assertEqual([row['Concern id'] for row in self.vps.served() or []], [kept])

    def test_fields_with_commas_quotes_and_newlines_survive_the_csv(self) -> None:
        concern = self.raise_concern('Client said "send KDM, then DCP"\nlater', waiting='client')
        self.autoscan()
        [row] = self.vps.served() or []
        self.assertEqual((row['Concern id'], row['Concern text'], row['waiting on']), (concern, 'Client said "send KDM, then DCP"\nlater', 'client'))

    def test_an_export_with_no_open_concerns_is_the_header_alone(self) -> None:
        self.autoscan()
        self.assertEqual(self.vps.served(), [])

    def test_the_push_is_off_without_the_settings_and_the_run_is_unchanged(self) -> None:
        from t6.concern_export import concerns_export_from_env
        self.assertIsNone(concerns_export_from_env({}))
        self.assertIsNone(concerns_export_from_env({'T6_CONCERNS_EXPORT_URL': 'http://vps:8790/concerns/export'}))
        self.assertIsNone(concerns_export_from_env({'T6_CONCERNS_EXPORT_PUSH_SECRET': 'x'}))
        self.assertIsNone(concerns_export_from_env({'T6_CONCERNS_EXPORT_URL': ' ', 'T6_CONCERNS_EXPORT_PUSH_SECRET': 'x'}))
        self.assertIsNotNone(concerns_export_from_env({'T6_CONCERNS_EXPORT_URL': 'http://vps:8790/concerns/export', 'T6_CONCERNS_EXPORT_PUSH_SECRET': 'x'}))
        self.raise_concern('Waiting on client for KDM')
        completed, log = self.autoscan(api=self.make_api(None))
        self.assertTrue(completed)
        self.assertEqual((self.vps.pushes, log), (0, 't6.server: concern scan archived=1 projects=1 skipped=0 raised=0 continued=0 announced=0 applied=0\n'))

    def test_a_vps_that_is_down_or_refuses_is_logged_and_does_not_fail_the_run(self) -> None:
        self.raise_concern('Waiting on client for KDM')
        self.vps.down = True
        completed, log = self.autoscan()
        self.assertTrue(completed)
        self.assertIn('concerns export push failed: ConnectionRefusedError', log)
        self.assertNotIn('KDM', log)
        self.vps.down = False
        completed, log = self.autoscan('2026-11-25T21:00', api=self.make_api(self.vps, secret='wrong'))
        self.assertTrue(completed)
        self.assertIn('concerns export push failed', log)
        self.assertIsNone(self.vps.served())  # nothing was stored

    def test_a_failed_export_build_does_not_fail_the_run_and_a_later_run_pushes_again(self) -> None:
        self.raise_concern('Waiting on client for KDM')
        self.notion_down = True
        completed, log = self.autoscan()
        self.assertTrue(completed)
        self.assertIn('concerns export push failed: OSError', log)
        self.notion_down = False
        self.assertIsNone(self.vps.served())
        completed, _ = self.autoscan('2026-11-25T21:00')
        self.assertTrue(completed)
        self.assertEqual(len(self.vps.served() or []), 1)

    def test_a_stopped_run_pushes_nothing(self) -> None:
        self.raise_concern('Waiting on client for KDM')
        from t6.verbs import Refused

        class Refusing:
            def run(self, inputs: object, *, actor: str, registry: object) -> Refused:
                return Refused(rule='masking', reason='refused')
        self.api.llm_service = Refusing()
        lines = Done(result={'lines': [{'as_of': '2026-11-24', 'text': 'A new line'}]})
        with patch.object(self.api, '_coordinators', return_value={'t6n_coordinator'}), patch.object(self.api, '_typed_read', return_value=lines), contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(self.api.autoscan(self.now))
        self.assertEqual(self.vps.pushes, 0)


if __name__ == '__main__':
    unittest.main()
