"""Synthetic workbook parity at the Sheets HTTP boundary."""
import base64
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from collections.abc import Mapping
from urllib.parse import parse_qs

from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
from openpyxl import Workbook

from t6.google.http import ServiceAccount
from t6.drives.sheets import WorkbookSheetSource


def account() -> ServiceAccount:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return ServiceAccount('synthetic@example.invalid', key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode(), 'https://oauth.example.invalid/token')


class SheetsHttp:
    def __init__(self) -> None:
        self.scopes: list[str] = []
        self.ids: list[str] = []
        self.fail = False
        self.payload: dict[str, Any] | None = None

    def send(self, method: str, url: str, headers: Mapping[str, str], body: bytes) -> tuple[int, bytes]:
        if method == 'POST':
            assertion = parse_qs(body.decode())['assertion'][0].split('.')[1]
            self.scopes.append(json.loads(base64.urlsafe_b64decode(assertion + '=='))['scope'])
            return 200, b'{"access_token":"synthetic-token","expires_in":3600}'
        if self.fail:
            return 503, b'{}'
        self.ids.append(url.split('/spreadsheets/')[1].split('?')[0])
        if self.payload is not None:
            return 200, json.dumps(self.payload).encode()
        def cell(value: Any) -> dict[str, Any]:
            return {'effectiveValue': {'boolValue' if isinstance(value, bool) else
                                      'numberValue' if isinstance(value, (int, float)) else 'stringValue': value}}
        rows = [[cell('Synthetic label'), {}, cell(2.5), cell(True)], [],
                [{'effectiveValue': {'numberValue': 46000}, 'effectiveFormat': {'numberFormat': {'type': 'DATE'}}}]]
        return 200, json.dumps({'sheets': [{'properties': {'title': 'SYNTHETIC TAB'},
                                            'data': [{'rowData': [{'values': r} for r in rows]}]}]}).encode()


class SheetsTests(unittest.TestCase):
    def test_same_rows_as_workbooks_for_both_spreadsheets_with_readonly_scope(self) -> None:
        from datetime import datetime
        from t6.drives.sheets import GoogleSheetsSource
        http = SheetsHttp()
        with tempfile.TemporaryDirectory() as folder:
            for name in ('synthetic-projects', 'synthetic-tracker'):
                path = Path(folder) / (name + '.xlsx')
                book = Workbook()
                sheet = book.active
                assert sheet is not None
                sheet.title = 'SYNTHETIC TAB'
                sheet.append(['Synthetic label', None, 2.5, True])
                sheet.append([])
                sheet.append([datetime(2025, 12, 9)])
                book.save(path)
                book.close()
                source = GoogleSheetsSource(name, account(), http)
                self.assertEqual(source.tabs(), WorkbookSheetSource(path).tabs())
                self.assertEqual(source.tabs(), source.tabs())
        self.assertEqual(http.ids, ['synthetic-projects', 'synthetic-tracker'])
        self.assertEqual(http.scopes, ['https://www.googleapis.com/auth/spreadsheets.readonly'] * 2)
