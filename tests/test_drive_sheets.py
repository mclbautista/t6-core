"""Synthetic workbook parity at the Sheets HTTP boundary."""
import base64
import json
import unittest
from typing import Any
from collections.abc import Mapping
from urllib.parse import parse_qs, unquote

from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization

from t6.google.http import ServiceAccount


def account() -> ServiceAccount:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return ServiceAccount('synthetic@example.invalid', key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode(), 'https://oauth.example.invalid/token')


class SheetsHttp:
    """A fake Sheets API: tab titles first, then `values:batchGet` in chunks."""

    def __init__(self) -> None:
        self.scopes: list[str] = []
        self.ids: list[str] = []
        self.urls: list[str] = []
        self.fail = False
        self.tab_rows: dict[str, list[list[Any]]] = {
            'SYNTHETIC TAB': [['Synthetic label', '', 2.5, True], [], [46000]]}

    def send(self, method: str, url: str, headers: Mapping[str, str], body: bytes) -> tuple[int, bytes]:
        if method == 'POST':
            assertion = parse_qs(body.decode())['assertion'][0].split('.')[1]
            self.scopes.append(json.loads(base64.urlsafe_b64decode(assertion + '=='))['scope'])
            return 200, b'{"access_token":"synthetic-token","expires_in":3600}'
        if self.fail:
            return 503, b'{}'
        self.urls.append(url)
        path = url.split('/spreadsheets/')[1]
        if '/values:batchGet' not in path:
            self.ids.append(path.split('?')[0])
            return 200, json.dumps({'sheets': [{'properties': {'title': t}} for t in self.tab_rows]}).encode()
        wanted = [unquote(r)[1:-1].replace("''", "'") for r in parse_qs(path.split('?')[1], keep_blank_values=True)['ranges']]
        return 200, json.dumps({'valueRanges': [
            {'range': f"'{t}'!A1:Z{len(self.tab_rows[t])}", **({'values': self.tab_rows[t]} if self.tab_rows[t] else {})}
            for t in wanted]}).encode()


class SheetsTests(unittest.TestCase):
    def test_rows_for_both_spreadsheets_with_readonly_scope_and_no_grid_read(self) -> None:
        from t6.drives.sheets import GoogleSheetsSource
        http = SheetsHttp()
        for name in ('synthetic-projects', 'synthetic-tracker'):
            source = GoogleSheetsSource(name, account(), http)
            expected = {'SYNTHETIC TAB': (('Synthetic label', None, 2.5, True),
                                          (None, None, None, None), (46000, None, None, None))}
            self.assertEqual(source.tabs(), expected)
            self.assertEqual(source.tabs(), source.tabs())
        self.assertEqual(http.ids, ['synthetic-projects', 'synthetic-tracker'])
        self.assertEqual(http.scopes, ['https://www.googleapis.com/auth/spreadsheets.readonly'] * 2)
        self.assertFalse([u for u in http.urls if 'includeGridData' in u])
        self.assertTrue(all('valueRenderOption=UNFORMATTED_VALUE' in u and 'dateTimeRenderOption=SERIAL_NUMBER' in u
                            for u in http.urls if 'batchGet' in u))

    def test_many_tabs_are_read_in_small_batches_and_empty_tabs_are_kept(self) -> None:
        from t6.drives.sheets import GoogleSheetsSource
        http = SheetsHttp()
        http.tab_rows = {f"TAB {n}'s": ([['Synthetic', n]] if n % 7 else []) for n in range(40)}
        tabs = GoogleSheetsSource('synthetic-many', account(), http).tabs()
        self.assertEqual(list(tabs), list(http.tab_rows))
        self.assertEqual(tabs["TAB 1's"], (('Synthetic', 1),))
        self.assertEqual(tabs["TAB 7's"], ())
        batches = [u for u in http.urls if 'batchGet' in u]
        self.assertEqual(len(batches), 3)
        self.assertTrue(all(u.count('ranges=') <= GoogleSheetsSource.TABS_PER_REQUEST for u in batches))

    def test_a_failed_read_raises_instead_of_returning_partial_rows(self) -> None:
        from t6.drives.sheets import GoogleSheetsSource
        http = SheetsHttp()
        http.fail = True
        with self.assertRaises(ValueError):
            GoogleSheetsSource('synthetic-projects', account(), http).tabs()

    def test_a_serial_date_reads_as_the_same_day_as_a_workbook_date(self) -> None:
        from datetime import date
        from t6.drives.importer import _date
        self.assertEqual(_date(46000), date(2025, 12, 9))
        self.assertEqual(_date(46000.0), date(2025, 12, 9))
        for bad in (12, 5.5, True, 1e9):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                _date(bad)

    def test_a_sheet_over_the_cell_limit_or_time_budget_fails_the_read(self) -> None:
        from unittest.mock import patch
        from t6.drives.sheets import GoogleSheetsSource
        http = SheetsHttp()
        http.tab_rows = {f'TAB {n}': [['x'] * 10] * 10 for n in range(5)}  # 500 cells
        small = GoogleSheetsSource('synthetic-big', account(), http)
        small.MAX_CELLS = 400
        with self.assertRaisesRegex(ValueError, 'too many cells'):
            small.tabs()
        slow = GoogleSheetsSource('synthetic-slow', account(), http)
        slow.TABS_PER_REQUEST = 1
        times = iter([0.0, 1.0, 1000.0, 2000.0, 3000.0, 4000.0, 5000.0])
        with patch('time.monotonic', side_effect=lambda: next(times)):
            with self.assertRaisesRegex(ValueError, 'time budget'):
                slow.tabs()

    def test_an_oversized_response_is_refused_by_the_real_transport(self) -> None:
        from unittest.mock import MagicMock, patch
        from t6.google import http as google_http
        response = MagicMock()
        response.status = 200
        response.read.side_effect = lambda n=-1: b'x' * (n if n > 0 else 10)
        response.__enter__.return_value = response
        with patch('urllib.request.urlopen', return_value=response), patch.object(google_http, 'MAX_RESPONSE_BYTES', 100):
            with self.assertRaisesRegex(ValueError, 'too large'):
                google_http.UrllibHttp().send('GET', 'https://example.invalid/x', {}, b'')

