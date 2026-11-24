"""The CRM Sheet Project dropdown tab: the writer at the Sheets HTTP boundary and the sync job, on synthetic records."""
import json
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote

from test_drive_sheets import account

from t6.registry.sqlite import SqliteProjectRegistry
from t6.worker import WorkerJob
from t6.worker.internal_queue import InternalJobQueue

SCOPE = 'https://www.googleapis.com/auth/spreadsheets'


class FakeSheets:
    """Records every call; the values API answers 200."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.scopes: list[str] = []
        self.fail = False

    def send(self, method: str, url: str, headers: Mapping[str, str], body: bytes) -> tuple[int, bytes]:
        if url.startswith('https://oauth.example.invalid/'):
            import base64
            claims = parse_qs(body.decode())['assertion'][0].split('.')[1]
            self.scopes.append(json.loads(base64.urlsafe_b64decode(claims + '=='))['scope'])
            return 200, b'{"access_token":"synthetic-token","expires_in":3600}'
        assert headers['Authorization'] == 'Bearer synthetic-token'
        if self.fail:
            return 503, b'{}'
        self.calls.append((method, unquote(url), json.loads(body) if body else None))
        return 200, b'{}'


class WriterTests(unittest.TestCase):
    def writer(self, http: FakeSheets) -> Any:
        from t6.crm.sheet_writer import GoogleSheetWriter
        return GoogleSheetWriter('synthetic-sheet', account(), http)

    def test_writes_the_column_then_clears_below_it_with_the_spreadsheets_scope(self) -> None:
        http = FakeSheets()
        self.writer(http).replace_column('Projects', ['A · t6p_1', "B's · t6p_2"])
        base = 'https://sheets.googleapis.com/v4/spreadsheets/synthetic-sheet/values/'
        self.assertEqual(http.scopes, [SCOPE])
        self.assertEqual(http.calls, [
            ('PUT', base + "'Projects'!A1:A2?valueInputOption=RAW",
             {'majorDimension': 'COLUMNS', 'values': [['A · t6p_1', "B's · t6p_2"]]}),
            ('POST', base + "'Projects'!A3:A:clear", {}),
        ])

    def test_an_empty_list_only_clears_the_column(self) -> None:
        http = FakeSheets()
        self.writer(http).replace_column('Projects', [])
        self.assertEqual([(m, u.rsplit('/values/')[1]) for m, u, _ in http.calls], [('POST', "'Projects'!A1:A:clear")])

    def test_a_refused_write_is_an_error(self) -> None:
        http = FakeSheets()
        http.fail = True
        with self.assertRaises(ValueError):
            self.writer(http).replace_column('Projects', ['x'])

    def test_the_writer_is_off_without_settings(self) -> None:
        from t6.crm.sheet_writer import configured_writer
        self.assertIsNone(configured_writer({}))
        self.assertIsNone(configured_writer({'T6_CRM_SHEET_ID': 'synthetic-sheet'}))

    def test_the_writer_signs_in_with_the_service_account_key_file_only(self) -> None:
        from t6.crm.sheet_writer import configured_writer
        key = account()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, 'key.json')
            path.write_text(json.dumps({'client_email': key.email, 'private_key': key.private_key, 'token_uri': key.token_uri}))
            writer = configured_writer({'T6_CRM_SHEET_ID': 'synthetic-sheet', 'GOOGLE_APPLICATION_CREDENTIALS': str(path)})
        self.assertIsNotNone(writer)


class FakeWorkbook:
    def __init__(self) -> None:
        self.writes: list[tuple[str, list[str]]] = []

    def replace_column(self, tab: str, values: list[str]) -> None:
        self.writes.append((tab, list(values)))


def job(kind: str = 'crm.sheet_sync', payload: Mapping[str, Any] | None = None) -> WorkerJob:
    return WorkerJob(1, 'main', kind, 'k', payload or {}, 't', 1)


class SyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self._folder = tempfile.TemporaryDirectory()
        self.addCleanup(self._folder.cleanup)
        self.registry = SqliteProjectRegistry(str(Path(self._folder.name, 'registry.db')))
        self.pages: dict[str, tuple[str, str]] = {}
        self.workbook = FakeWorkbook()

    def register(self, number: int, name: str, status: str, *, bind: bool = True) -> str:
        page = f'00000000000040008000{number:012d}'
        result = self.registry.register(actor='setup', idempotency_key=f'k{number}', notion_page_id=page if bind else None)
        self.pages[page] = (name, status)
        return str(result.project.id)  # type: ignore[union-attr]

    def sync(self) -> Any:
        from t6.crm.sheet_sync import ProjectDropdown
        return ProjectDropdown(self.registry, lambda page: self.pages.get(page), self.workbook).handle(job())

    def test_lists_open_leads_and_active_projects_by_name(self) -> None:
        lead = self.register(1, 'Zed Lead', 'Leads')
        deal = self.register(2, 'Mid Deal', '🤝 Negotiations')
        proposal = self.register(3, 'Pitch', 'Proposal Made')
        live = self.register(4, 'Alpha Show', '🚨 In-Progress')
        airing = self.register(5, 'On Air', 'Airing')
        self.register(6, 'Paused', 'Standby')
        self.register(7, 'Old', 'Archived')
        self.register(8, 'Gone', 'Lost')
        self.register(9, 'No Page', 'Leads', bind=False)
        self.sync()
        self.assertEqual(self.workbook.writes, [('Projects', [
            f'Alpha Show · {live}', f'Mid Deal · {deal}', f'On Air · {airing}', f'Pitch · {proposal}', f'Zed Lead · {lead}'])])

    def test_a_rename_and_a_status_change_show_on_the_next_sync(self) -> None:
        project = self.register(1, 'First', 'Leads')
        self.sync()
        page = next(iter(self.pages))
        self.pages[page] = ('Second', 'Leads')
        self.sync()
        self.pages[page] = ('Second', 'Lost')
        self.sync()
        self.assertEqual(self.workbook.writes, [('Projects', [f'First · {project}']), ('Projects', [f'Second · {project}']), ('Projects', [])])

    def test_an_unreadable_page_is_left_out_and_a_blank_name_is_kept(self) -> None:
        blank = self.register(1, '  ', 'Leads')
        self.register(2, 'Unreadable', 'Leads')
        self.pages.popitem()  # the last one registered
        self.sync()
        self.assertEqual(self.workbook.writes, [('Projects', [f'(untitled) · {blank}'])])


class TriggerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._folder = tempfile.TemporaryDirectory()
        self.addCleanup(self._folder.cleanup)
        self.registry = SqliteProjectRegistry(str(Path(self._folder.name, 'registry.db')))
        self.now = [1_000_000.0]
        self.queue = InternalJobQueue(self.registry, clock=lambda: self.now[0])
        self.inner: list[WorkerJob] = []

    def wrap(self, result: Any, *, kind: str) -> Any:
        from t6.crm.sheet_sync import SheetSyncTrigger

        def handler(received: WorkerJob) -> Any:
            self.inner.append(received)
            return result
        return SheetSyncTrigger(self.queue, status_property='%3DR%7DW', clock=lambda: self.now[0]).wrap(handler, kind)

    def queued(self) -> int:
        with self.registry._transaction() as db:
            return int(db.execute("SELECT COUNT(*) FROM internal_jobs WHERE kind = 'crm.sheet_sync'").fetchone()[0])

    def test_a_registry_sync_queues_one_rewrite_and_a_burst_shares_it(self) -> None:
        handler = self.wrap({'synced': 1}, kind='registry.sync')
        for _ in range(3):
            self.assertEqual(handler(job('registry.sync')), {'synced': 1})
            self.now[0] += 10
        self.assertEqual(self.queued(), 1)
        self.assertEqual(len(self.inner), 3)
        self.now[0] += 600
        handler(job('registry.sync'))
        self.assertEqual(self.queued(), 2)

    def test_the_rewrite_waits_for_the_end_of_the_window(self) -> None:
        self.wrap({}, kind='registry.sync')(job('registry.sync'))
        self.assertIsNone(self.queue.claim('main'))
        self.now[0] += 301
        claim = self.queue.claim('main')
        self.assertIsNotNone(claim)
        assert claim is not None
        self.assertEqual(claim.kind, 'crm.sheet_sync')

    def test_a_title_or_status_edit_queues_one_but_another_property_does_not(self) -> None:
        handler = self.wrap({'outcome': 'ok'}, kind='notion.page_changed')
        handler(job('notion.page_changed', {'updated_properties': ['abcd']}))
        self.assertEqual(self.queued(), 0)
        handler(job('notion.page_changed', {'updated_properties': ['abcd', 'title']}))
        self.assertEqual(self.queued(), 1)
        self.now[0] += 600
        handler(job('notion.page_changed', {'updated_properties': ['%3DR%7DW']}))
        self.assertEqual(self.queued(), 2)
        self.now[0] += 600
        handler(job('notion.page_changed', {}))  # a new page: no list of edited properties
        self.assertEqual(self.queued(), 3)

    def test_a_page_that_is_not_a_project_page_queues_nothing(self) -> None:
        self.wrap({'outcome': 'not_a_project_page'}, kind='notion.page_changed')(job('notion.page_changed', {}))
        self.assertEqual(self.queued(), 0)

    def test_a_failed_handler_queues_nothing(self) -> None:
        def broken(received: WorkerJob) -> Any:
            raise RuntimeError('synthetic')
        from t6.crm.sheet_sync import SheetSyncTrigger
        with self.assertRaises(RuntimeError):
            SheetSyncTrigger(self.queue, status_property='x', clock=lambda: self.now[0]).wrap(broken, 'registry.sync')(job('registry.sync'))
        self.assertEqual(self.queued(), 0)


class InstallTests(unittest.TestCase):
    def test_nothing_changes_without_the_sheet_setting(self) -> None:
        from t6.crm.sheet_sync import install
        handlers: dict[str, Any] = {'registry.sync': lambda j: None}
        self.assertEqual(install(handlers, {}, lambda page: None), handlers)


if __name__ == '__main__':
    unittest.main()
