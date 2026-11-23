"""Sheet rows per tab, independent of the service that supplies them."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from functools import cached_property
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from t6.google.http import Http, ServiceAccount


class SheetSource(Protocol):
    """One consistent snapshot of a workbook's rows, identified for source evidence."""

    @property
    def workbook(self) -> str: ...

    def tabs(self) -> Mapping[str, Sequence[tuple[Any, ...]]]: ...


class WorkbookSheetSource:
    """Read workbook rows; tests build their workbooks in temporary folders."""

    def __init__(self, path: str | Path) -> None:
        self.workbook = str(Path(path).resolve())

    def tabs(self) -> dict[str, tuple[tuple[Any, ...], ...]]:
        return self._rows

    @cached_property
    def _rows(self) -> dict[str, tuple[tuple[Any, ...], ...]]:
        from openpyxl import load_workbook
        workbook = load_workbook(self.workbook, read_only=True, data_only=True)
        try:
            return {sheet.title: tuple(sheet.iter_rows(values_only=True)) for sheet in workbook.worksheets}
        finally:
            workbook.close()


class GoogleSheetsSource:
    """A cached Sheets snapshot using effective values, including typed dates."""

    def __init__(self, spreadsheet: str, account: ServiceAccount, http: Http) -> None:
        self.workbook = spreadsheet
        self._account, self._http = account, http

    def tabs(self) -> dict[str, tuple[tuple[Any, ...], ...]]:
        return self._rows

    @cached_property
    def _rows(self) -> dict[str, tuple[tuple[Any, ...], ...]]:
        import json
        import time
        from datetime import datetime, timedelta
        from urllib.parse import quote, urlencode
        from t6.google.http import GRANT
        form = urlencode({'grant_type': GRANT, 'assertion': self._account.assertion(
            time.time(), 'https://www.googleapis.com/auth/spreadsheets.readonly')}).encode()
        status, raw = self._http.send('POST', self._account.token_uri,
                                      {'Content-Type': 'application/x-www-form-urlencoded'}, form)
        answer = json.loads(raw)
        if status != 200 or not answer.get('access_token'):
            raise ValueError(f'Google Sheets sign-in failed ({status})')
        status, raw = self._http.send('GET', 'https://sheets.googleapis.com/v4/spreadsheets/' +
                                     quote(self.workbook, safe='') + '?includeGridData=true',
                                     {'Authorization': 'Bearer ' + answer['access_token']}, b'')
        answer = json.loads(raw)
        if status != 200 or not isinstance(answer.get('sheets'), list):
            raise ValueError(f'Google Sheets read failed ({status})')
        tabs = {}
        for sheet in answer['sheets']:
            rows: list[tuple[Any, ...]] = []
            for grid in sheet.get('data', []):
                for row in grid.get('rowData', []):
                    values = []
                    for cell in row.get('values', []):
                        effective = cell.get('effectiveValue', {})
                        if 'errorValue' in effective:
                            value: Any = cell.get('formattedValue', '#ERROR!')
                        else:
                            value = next(iter(effective.values()), None)
                        kind = cell.get('effectiveFormat', {}).get('numberFormat', {}).get('type')
                        if isinstance(value, (int, float)) and kind in ('DATE', 'DATE_TIME', 'TIME'):
                            value = datetime(1899, 12, 30) + timedelta(days=value)
                        values.append(value)
                    rows.append(tuple(values))
            width = max((len(row) for row in rows), default=0)
            tabs[sheet['properties']['title']] = tuple(row + (None,) * (width - len(row)) for row in rows)
        return tabs


def configured_sources(environ: Mapping[str, str]) -> tuple[SheetSource, SheetSource]:
    """Both spreadsheet IDs and the service account file come from configuration."""
    from t6.google.http import ServiceAccount, UrllibHttp
    from t6.google.config import CREDENTIAL_VARIABLE
    keys = ('T6_DRIVE_PROJECT_SPREADSHEET_ID', 'T6_DRIVE_TRACKER_SPREADSHEET_ID', CREDENTIAL_VARIABLE)
    if any(not environ.get(key) for key in keys):
        raise ValueError('Set ' + ', '.join(keys))
    account = ServiceAccount.from_json(Path(environ[keys[2]]).read_text(encoding='utf-8'))
    http = UrllibHttp()
    return (GoogleSheetsSource(environ[keys[0]], account, http),
            GoogleSheetsSource(environ[keys[1]], account, http))
