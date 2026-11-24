"""`schedule snapshot` (#722): a fake Google HTTP layer, through the API and the typed client, to a ScheduleSnapshot."""
from __future__ import annotations

import base64
import json
import os
import tempfile
import threading
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs, unquote, urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from t6.client import Client
from t6.person import People
from t6.preview import PreviewTokens
from t6.registry.sqlite import SqliteProjectRegistry
from t6.schedule.contract import CellFill
from t6.schedule import ScheduleSnapshot, UnsupportedLayout, extract_schedule, parse_request, parse_response
from t6.server.http import make_server
from t6.server.queue import JobQueue
from t6.server.verbs import VerbAPI
from t6.verbs import Done, Refused, ScheduleSnapshotInput

FIXTURE = Path(__file__).parent / 'fixtures' / 'schedule' / 'request.v1.json'
TOKEN_URI = 'https://oauth.example.invalid/token'
TAB = 'as of Nov 1'
TAB_ID = 4242


def key_file(directory: str) -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    path = Path(directory) / 'key.json'
    path.write_text(json.dumps({'client_email': 'synthetic@example.invalid', 'private_key': pem, 'token_uri': TOKEN_URI}))
    return str(path)


def letters(index: int) -> str:
    out = ''
    n = index + 1
    while n:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


class FakeSheets:
    """The Sheets calls this verb may make, answered from the synthetic fixture. Records every request."""

    def __init__(self, rows: int = 29, columns: int = 15) -> None:
        self.source = parse_request(FIXTURE.read_text())
        self.rows, self.columns = rows, columns
        self.calls: list[tuple[str, str, str]] = []
        self.scopes: list[str] = []
        self.stop_after: int | None = None  # a read that ends early, still naming the range it covered
        self.fail_values = self.fail_geometry = False

    def send(self, method: str, url: str, headers: Mapping[str, str], body: bytes) -> tuple[int, bytes]:
        if method == 'POST':
            assertion = parse_qs(body.decode())['assertion'][0].split('.')[1]
            self.scopes.append(json.loads(base64.urlsafe_b64decode(assertion + '=='))['scope'])
            return 200, b'{"access_token":"synthetic-token","expires_in":3600}'
        parts = urlsplit(url)
        query = parse_qs(parts.query, keep_blank_values=True)
        path = parts.path
        self.calls.append((method, path, parts.query))
        assert headers['Authorization'] == 'Bearer synthetic-token'
        prefix = '/v4/spreadsheets/synthetic-book'
        if path.startswith(prefix + '/values/'):
            return self._values(unquote(path[len(prefix + '/values/'):]), query)
        assert path == prefix, path
        fields = query['fields'][0]
        if 'merges' not in fields:
            tabs = [{'properties': {'sheetId': 7, 'title': 'other tab', 'gridProperties': {'rowCount': 10, 'columnCount': 5}}},
                    {'properties': {'sheetId': TAB_ID, 'title': TAB, 'gridProperties': {'rowCount': self.rows, 'columnCount': self.columns}}}]
            return 200, json.dumps({'sheets': tabs}).encode()
        assert query['ranges'] == [f"'{TAB}'"]
        if self.fail_geometry:
            return 503, b'{}'
        sheet: dict[str, Any] = {'properties': {'sheetId': TAB_ID}, 'merges': [
            {'sheetId': TAB_ID, 'startRowIndex': m.start_row, 'endRowIndex': m.end_row,
             'startColumnIndex': m.start_col, 'endColumnIndex': m.end_col} for m in self.source.merges or []]}
        if query.get('includeGridData') == ['true']:
            colours = {(f.row, f.col): f.color for f in self.source.fills}
            rows = []
            for r in range(self.rows):
                row = []
                for c in range(self.columns):
                    color = colours.get((r, c))
                    if color:
                        rgb = {k: int(color[i:i + 2], 16) / 255 for k, i in (('red', 1), ('green', 3), ('blue', 5))}
                        row.append({'effectiveFormat': {'backgroundColor': rgb}})
                    else:
                        row.append({'effectiveFormat': {'backgroundColor': {'red': 1, 'green': 1, 'blue': 1}}})
                rows.append({'values': row})
            sheet['data'] = [{'rowData': rows}]
        return 200, json.dumps({'sheets': [sheet]}).encode()

    def _values(self, rng: str, query: dict[str, list[str]]) -> tuple[int, bytes]:
        assert rng == f"'{TAB}'!A1:{letters(self.columns - 1)}{self.rows}", rng
        assert query['valueRenderOption'] == ['UNFORMATTED_VALUE'] and query['majorDimension'] == ['ROWS']
        if self.fail_values:
            return 500, b'{}'
        last = self.stop_after or self.rows
        grid = [list(r) + [None] * (self.columns - len(r)) for r in self.source.cells]
        grid += [[None] * self.columns for _ in range(self.rows - len(grid))]
        shown: list[list[Any]] = []
        for row in grid[:last]:
            cells = ['' if v is None else v for v in row]
            while cells and cells[-1] == '':
                cells.pop()
            shown.append(cells)
        while shown and not shown[-1]:
            shown.pop()
        return 200, json.dumps({'range': f"'{TAB}'!A1:{letters(self.columns - 1)}{last}", 'majorDimension': 'ROWS', 'values': shown}).encode()


class ScheduleSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sheets = FakeSheets()
        registry = SqliteProjectRegistry(self.tmp.name + '/registry')
        api = VerbAPI(People(registry, PreviewTokens('key'), {'reports': frozenset({'operator'})}), JobQueue(self.tmp.name + '/queue'), {'secret': 'reports'}, lambda _: None)
        self.server = make_server(('127.0.0.1', 0), api)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(self.server.shutdown)
        self.environ = patch.dict(os.environ, {'GOOGLE_APPLICATION_CREDENTIALS': key_file(self.tmp.name)})
        self.environ.start()
        self.addCleanup(self.environ.stop)
        sheets = self.sheets
        fake = type('FakeTransport', (), {'send': lambda self, *a: sheets.send(*a)})
        patcher = patch('t6.google.http.UrllibHttp', fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = Client(f'http://127.0.0.1:{self.server.server_port}', 'secret')

    def snapshot(self, year: int | None = 2026) -> ScheduleSnapshot | UnsupportedLayout:
        outcome = self.client.schedule_snapshot(ScheduleSnapshotInput(spreadsheet_id='synthetic-book', tab_title=TAB, year=year))
        self.assertIsInstance(outcome, Done, outcome)
        assert isinstance(outcome, Done)
        return parse_response(outcome.result)

    def expected(self, **changes: Any) -> ScheduleSnapshot:
        scope = self.sheets.source.source_scope.model_copy(update={'document_id': 'synthetic-book', 'tab_id': str(TAB_ID), 'tab_title': TAB})
        request = self.sheets.source.model_copy(update={'source_scope': scope, 'source_revision': None, **changes})
        result = extract_schedule(request)
        assert isinstance(result, ScheduleSnapshot)
        return result

    def test_values_and_merges_from_one_tab_give_the_extractor_snapshot(self) -> None:
        result = self.snapshot()
        self.assertEqual(result, self.expected())
        assert isinstance(result, ScheduleSnapshot)
        self.assertEqual(result.coverage.status, 'complete')
        self.assertTrue(result.candidates)
        assert result.source_revision is not None  # the fingerprint: Sheets gives no revision
        self.assertTrue(result.source_revision.startswith('sha256:'))

    def test_a_cell_fill_travels_as_raw_evidence_on_its_candidate(self) -> None:
        source = self.sheets.source
        self.sheets.source = source.model_copy(update={'fills': [*source.fills, CellFill(row=10, col=4, color='#112233')]})
        result = self.snapshot()
        assert isinstance(result, ScheduleSnapshot)
        self.assertEqual(result, self.expected(fills=self.sheets.source.fills))
        self.assertEqual([c.provenance.fill for c in result.candidates if c.provenance.anchor == 'E11'], ['#112233'])

    def test_without_a_year_the_extractor_decides_as_it_would_for_the_same_tab(self) -> None:
        self.assertEqual(self.snapshot(year=None), self.expected(year=None))

    def test_blank_rows_and_columns_that_google_trims_are_still_a_complete_read(self) -> None:
        self.sheets.rows, self.sheets.columns = 40, 18
        result = self.snapshot()
        assert isinstance(result, ScheduleSnapshot)
        self.assertEqual((result.coverage.rows_total, result.coverage.columns_total), (40, 18))
        self.assertEqual(result.coverage.status, 'complete')
        self.assertEqual(result.candidates, self.expected().candidates)

    def test_a_read_that_stops_early_is_incomplete_not_a_shorter_schedule(self) -> None:
        self.sheets.stop_after = 12
        result = self.snapshot()
        assert isinstance(result, ScheduleSnapshot)
        self.assertEqual(result.coverage.status, 'incomplete')
        self.assertIn('rows_short', result.coverage.reasons)
        self.assertTrue(all(g.status != 'complete' for g in result.groups))

    def test_no_merge_list_is_incomplete(self) -> None:
        self.sheets.fail_geometry = True
        result = self.snapshot()
        assert isinstance(result, ScheduleSnapshot)
        self.assertIn('merges_missing', result.coverage.reasons)
        self.assertEqual(result.candidates, [])

    def test_a_failed_values_read_is_refused_not_guessed(self) -> None:
        self.sheets.fail_values = True
        outcome = self.client.schedule_snapshot(ScheduleSnapshotInput(spreadsheet_id='synthetic-book', tab_title=TAB))
        self.assertIsInstance(outcome, Refused)
        assert isinstance(outcome, Refused)
        self.assertEqual(outcome.rule, 'google')

    def test_an_unknown_tab_is_refused(self) -> None:
        outcome = self.client.schedule_snapshot(ScheduleSnapshotInput(spreadsheet_id='synthetic-book', tab_title='missing'))
        self.assertIsInstance(outcome, Refused)

    def test_only_the_readonly_scope_and_get_requests_reach_google(self) -> None:
        self.snapshot()
        self.assertEqual(self.sheets.scopes, ['https://www.googleapis.com/auth/spreadsheets.readonly'])
        self.assertTrue(self.sheets.calls)
        self.assertEqual({method for method, _, _ in self.sheets.calls}, {'GET'})
        for _, path, _ in self.sheets.calls:
            self.assertNotIn(':batchUpdate', path)
            self.assertNotIn(':append', path)
        grid_reads = [q for _, _, q in self.sheets.calls if 'includeGridData' in q]
        self.assertEqual(len(grid_reads), 1)  # one tab, limited by `ranges` and `fields`
        self.assertIn('ranges=', grid_reads[0])

    def test_without_the_credential_the_verb_is_refused_and_asks_nothing_of_google(self) -> None:
        with patch.dict(os.environ, {'GOOGLE_APPLICATION_CREDENTIALS': ''}):
            outcome = self.client.schedule_snapshot(ScheduleSnapshotInput(spreadsheet_id='synthetic-book', tab_title=TAB))
        self.assertIsInstance(outcome, Refused)
        assert isinstance(outcome, Refused)
        self.assertEqual(outcome.rule, 'config')
        self.assertEqual(self.sheets.calls, [])


if __name__ == '__main__':
    unittest.main()
