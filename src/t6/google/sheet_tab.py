"""Read one tab of a Google Sheet as a schedule request: values, merges, fills and the tab's grid size (#722).

Read only. The sign-in asks for the `spreadsheets.readonly` scope and every Sheets request is a GET. The values come from
`values.get`; merges and fills come from one `spreadsheets.get` limited by `ranges` and `fields` to the selected tab, so the
workbook's other tabs are never read. Sheets reports no revision cheaply, so `source_revision` stays null and the extractor
fingerprints the content.
"""
from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote, urlencode

from t6.google.http import GRANT, Http, ServiceAccount
from t6.schedule.contract import CellFill, CellValue, Merge, ScheduleRequest, SourceScope
from t6.schedule.grid import a1

SCOPE = 'https://www.googleapis.com/auth/spreadsheets.readonly'
API = 'https://sheets.googleapis.com/v4/spreadsheets/'
MAX_CELLS = 500_000  # a larger tab fails the read instead of stalling the host
_RANGE = re.compile(r'^([A-Z]+)(\d+)(?::([A-Z]+)(\d+))?$')


class SheetReadError(ValueError):
    """A Google read that cannot give a tab. The message holds a status or a reason, never a credential."""


def _columns(letters: str) -> int:
    count = 0
    for char in letters:
        count = count * 26 + ord(char) - 64
    return count


def _extent(answered: Any) -> tuple[int, int] | None:
    """Rows and columns an answered range covers from A1, or None when it does not start at A1."""
    match = _RANGE.match(str(answered).rpartition('!')[2])
    if not match:
        return None
    first_col, first_row, last_col, last_row = match.groups()
    if (first_col, first_row) != ('A', '1'):
        return None
    return (int(last_row), _columns(last_col)) if last_col else (1, 1)


def _cell(value: Any) -> CellValue:
    if value is None or value == '':
        return None
    if isinstance(value, bool):
        return 'TRUE' if value else 'FALSE'
    return value if isinstance(value, (str, int, float)) else str(value)


def _fills(sheet: Mapping[str, Any]) -> list[CellFill]:
    fills: list[CellFill] = []
    for block in sheet.get('data') or []:
        for r, row in enumerate(block.get('rowData') or [], block.get('startRow', 0)):
            for c, cell in enumerate(row.get('values') or [], block.get('startColumn', 0)):
                colour = (cell.get('effectiveFormat') or {}).get('backgroundColor')
                if colour is None:
                    continue
                parts = tuple(round(float(colour.get(name, 0)) * 255) for name in ('red', 'green', 'blue'))
                if parts != (255, 255, 255):  # the default background says nothing
                    fills.append(CellFill(row=r, col=c, color='#%02x%02x%02x' % parts))
    return fills


class SheetTabReader:
    def __init__(self, account: ServiceAccount, http: Http) -> None:
        self._account, self._http = account, http
        self._headers: dict[str, str] | None = None

    def _get(self, url: str) -> dict[str, Any]:
        if self._headers is None:
            form = urlencode({'grant_type': GRANT, 'assertion': self._account.assertion(time.time(), SCOPE)}).encode()
            status, raw = self._http.send('POST', self._account.token_uri, {'Content-Type': 'application/x-www-form-urlencoded'}, form)
            try:
                token = json.loads(raw).get('access_token')
            except (ValueError, AttributeError):
                token = None
            if status != 200 or not token:
                raise SheetReadError(f'Google Sheets sign-in failed ({status})')
            self._headers = {'Authorization': 'Bearer ' + str(token)}
        try:
            status, raw = self._http.send('GET', url, self._headers, b'')
        except OSError as error:
            raise SheetReadError(f'Google Sheets unreachable: {error.__class__.__name__}') from error
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
        if status != 200 or not isinstance(body, dict):
            raise SheetReadError(f'Google Sheets read failed ({status})')
        return body

    def read(self, spreadsheet_id: str, tab_title: str, year: int | None = None) -> ScheduleRequest:
        base = API + quote(spreadsheet_id, safe='')
        listing = self._get(base + '?fields=' + quote('sheets.properties(sheetId,title,gridProperties(rowCount,columnCount))', safe='(),.'))
        found = [s.get('properties') or {} for s in listing.get('sheets') or [] if (s.get('properties') or {}).get('title') == tab_title]
        if not found:
            raise SheetReadError('Google Sheets tab not found')
        sheet_id, grid = found[0].get('sheetId'), found[0].get('gridProperties') or {}
        rows, columns = grid.get('rowCount'), grid.get('columnCount')
        if not isinstance(sheet_id, int) or not isinstance(rows, int) or not isinstance(columns, int) or rows < 1 or columns < 1:
            raise SheetReadError('Google Sheets read failed (no tab size)')
        if rows * columns > MAX_CELLS:
            raise SheetReadError('Google Sheets read failed (too many cells)')
        title = "'" + tab_title.replace("'", "''") + "'"
        merges, fills = self._geometry(base, title, sheet_id)
        answer = self._get(base + '/values/' + quote(f'{title}!A1:{a1(rows - 1, columns - 1)}', safe='')
                           + '?majorDimension=ROWS&valueRenderOption=UNFORMATTED_VALUE&dateTimeRenderOption=SERIAL_NUMBER')
        values = answer.get('values') or []
        if not isinstance(values, list):
            raise SheetReadError('Google Sheets read failed (unexpected answer)')
        # Sheets trims trailing blanks, so the answered range, not the values, says how far the read reached.
        covered = _extent(answer.get('range'))
        read_rows, read_columns = (min(covered[0], rows), min(covered[1], columns)) if covered else (0, 0)
        cells = [[_cell(v) for v in row] for row in values]
        width = max(read_columns, max((len(row) for row in cells), default=0))
        cells = [row + [None] * (width - len(row)) for row in cells]
        cells += [[None] * width for _ in range(read_rows - len(cells))]
        return ScheduleRequest(
            source_scope=SourceScope(document_id=spreadsheet_id, tab_id=str(sheet_id), tab_title=tab_title),
            cells=cells, merges=merges, fills=fills, rows_total=rows, columns_total=columns, year=year)

    def _geometry(self, base: str, title: str, sheet_id: int) -> tuple[list[Merge] | None, list[CellFill]]:
        """The tab's merges and fills. A failed read gives no merge list (null), which the extractor reports as incomplete."""
        fields = 'sheets(properties(sheetId),merges,data(startRow,startColumn,rowData(values(effectiveFormat(backgroundColor)))))'
        try:
            answer = self._get(base + '?ranges=' + quote(title, safe='') + '&includeGridData=true&fields=' + quote(fields, safe='(),.'))
            sheet = next(s for s in answer.get('sheets') or [] if (s.get('properties') or {}).get('sheetId') == sheet_id)
            merges = [Merge(start_row=m.get('startRowIndex', 0), end_row=m['endRowIndex'],
                            start_col=m.get('startColumnIndex', 0), end_col=m['endColumnIndex']) for m in sheet.get('merges') or []]
            return merges, _fills(sheet)
        except (SheetReadError, StopIteration, KeyError, TypeError, ValueError, AttributeError):
            return None, []
