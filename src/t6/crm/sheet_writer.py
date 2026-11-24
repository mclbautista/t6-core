"""Write values into the CRM Google Sheet as the t6 service account (the first Sheet write, #712)."""
from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote, urlencode

if TYPE_CHECKING:
    from t6.google.http import Http, ServiceAccount

SCOPE = 'https://www.googleapis.com/auth/spreadsheets'
SHEET_VARIABLE = 'T6_CRM_SHEET_ID'


class GoogleSheetWriter:
    """Replace a whole column of a tab. The tabs themselves are set up once by hand."""

    def __init__(self, spreadsheet: str, account: ServiceAccount, http: Http) -> None:
        self._base = 'https://sheets.googleapis.com/v4/spreadsheets/' + quote(spreadsheet, safe='') + '/values/'
        self._account, self._http = account, http

    def _token(self) -> str:
        from t6.google.http import GRANT
        form = urlencode({'grant_type': GRANT, 'assertion': self._account.assertion(time.time(), SCOPE)}).encode()
        status, raw = self._http.send('POST', self._account.token_uri, {'Content-Type': 'application/x-www-form-urlencoded'}, form)
        token = json.loads(raw).get('access_token') if status == 200 else None
        if not token:
            raise ValueError(f'Google Sheets sign-in failed ({status})')
        return str(token)

    def _send(self, method: str, range_: str, token: str, body: object, query: str = '') -> None:
        url = self._base + quote(range_, safe="!:'") + query
        headers = {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}
        status, _ = self._http.send(method, url, headers, json.dumps(body).encode())
        if status != 200:
            raise ValueError(f'Google Sheets write failed ({status})')

    def replace_column(self, tab: str, values: Sequence[str]) -> None:
        """Put `values` in column A from row 1, then clear the rows below, so the column is never empty in between."""
        token = self._token()
        name = "'" + tab.replace("'", "''") + "'"
        if values:
            self._send('PUT', f'{name}!A1:A{len(values)}', token, {'majorDimension': 'COLUMNS', 'values': [list(values)]},
                       '?valueInputOption=RAW')
        self._send('POST', f'{name}!A{len(values) + 1}:A:clear', token, {})


def configured_writer(environ: Mapping[str, str]) -> GoogleSheetWriter | None:
    """None (the CRM Sheet feature is off) unless the Sheet ID and the service account key file are both set."""
    from t6.google.config import credential_path
    from t6.google.http import ServiceAccount, UrllibHttp
    sheet, key = (environ.get(SHEET_VARIABLE) or '').strip(), credential_path(environ)
    if not sheet or key is None:
        return None
    return GoogleSheetWriter(sheet, ServiceAccount.from_json(Path(key).read_text(encoding='utf-8')), UrllibHttp())
