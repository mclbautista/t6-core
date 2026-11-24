"""The CRM Sheet Log: rows by Project, last contact, links by kind, reminders due."""
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any
from unittest.mock import patch

from t6.crm.log import CrmLog, due_reminders, links_by_kind, read_log

A = 't6p_' + 'a' * 24
B = 't6p_' + 'b' * 24
HEADER = ('Date', 'Project', 'Kind', 'Note', 'Link', 'By', 'Remind on', 'Done')


class FakeSheet:
    workbook = 'synthetic-crm'

    def __init__(self, log: Sequence[tuple[Any, ...]] | None, header: tuple[str, ...] = HEADER) -> None:
        self._tabs: dict[str, Sequence[tuple[Any, ...]]] = {'Projects': (('x',),)}
        if log is not None:
            self._tabs['Log'] = (header, *log)

    def tabs(self) -> Mapping[str, Sequence[tuple[Any, ...]]]:
        return self._tabs


def row(day: Any, project: Any, kind: Any = 'call', link: Any = None, remind: Any = None, done: Any = None) -> tuple[Any, ...]:
    return (day, project, kind, 'note', link, 'Synthetic', remind, done)


class ReadLogTests(unittest.TestCase):
    def test_rows_map_to_projects_by_id_and_the_rest_are_reported_not_guessed(self) -> None:
        log = read_log(FakeSheet([
            row(46000, f'Big Show · {A}'),
            row(datetime(2026, 11, 20, 9, 30), f'Other · {B}'),
            row(46001, 'No id here'),
            row(46002, None),
            row(46003, 't6p_' + 'c' * 24),
            row(None, f'Big Show · {A}'),
            (None,) * 8,
        ]), {A, B})
        self.assertEqual(sorted(log.by_project), [A, B])
        self.assertEqual(log.by_project[B][0].day, date(2026, 11, 20))
        self.assertEqual(log.by_project[A][0].day, date(2025, 12, 9))
        self.assertEqual([(u.row, u.reason) for u in log.unmapped],
                         [(4, 'no project'), (5, 'no project'), (6, 'unknown project'), (7, 'no date')])

    def test_columns_are_found_by_header_name_in_any_order(self) -> None:
        sheet = FakeSheet([(f'x · {A}', 46000, 'quote', 'https://example.invalid/q')], header=('Project', 'Date', 'Kind', 'Link'))
        log = read_log(sheet, {A})
        self.assertEqual((log.by_project[A][0].kind, log.by_project[A][0].link), ('quote', 'https://example.invalid/q'))

    def test_missing_tab_or_column_is_an_error(self) -> None:
        with self.assertRaises(ValueError):
            read_log(FakeSheet(None), {A})
        with self.assertRaises(ValueError):
            read_log(FakeSheet([], header=('Date', 'Kind')), {A})


class DerivedTests(unittest.TestCase):
    def log(self) -> CrmLog:
        return read_log(FakeSheet([
            row(46000, A, 'call'),
            row(46010, A, 'quote', 'https://example.invalid/q1', remind=46020),
            row(46005, A, 'billing', 'https://example.invalid/b1', remind=46030, done=True),
            row(46006, A, 'message', 'https://mail.google.com/mail/u/0/#inbox/g1'),
            row(46007, A, 'other', 'https://example.invalid/o'),
            row(46008, A, 'quote', None),
            row(46100, B, 'billing', 'https://example.invalid/b2', remind=46200),
        ]), {A, B})

    def test_last_contact_is_the_later_of_newest_row_and_stage_change(self) -> None:
        log = self.log()
        newest = date(1899, 12, 30).fromordinal(date(1899, 12, 30).toordinal() + 46010)
        self.assertEqual(log.last_contact(A, lambda _: None), newest)
        later = date.fromordinal(newest.toordinal() + 3)
        self.assertEqual(log.last_contact(A, lambda _: later), later)
        self.assertEqual(log.last_contact(A, lambda _: date(2000, 1, 1)), newest)

    def test_last_contact_without_rows_is_the_stage_change_or_none(self) -> None:
        log = read_log(FakeSheet([]), {A})
        self.assertIsNone(log.last_contact(A, lambda _: None))
        self.assertEqual(log.last_contact(A, lambda _: date(2026, 1, 2)), date(2026, 1, 2))

    def test_links_by_kind_keeps_only_the_known_kinds_in_sheet_order(self) -> None:
        self.assertEqual(links_by_kind(self.log(), A), {
            'quote': ['https://example.invalid/q1'],
            'billing': ['https://example.invalid/b1'],
            'gmail_thread': ['https://mail.google.com/mail/u/0/#inbox/g1']})
        self.assertEqual(links_by_kind(self.log(), 't6p_' + 'd' * 24), {'quote': [], 'billing': [], 'gmail_thread': []})

    def test_due_reminders_are_quote_or_billing_due_today_or_earlier_and_not_done(self) -> None:
        log = self.log()
        base = date.fromordinal(date(1899, 12, 30).toordinal() + 46020)
        self.assertEqual([(r.project_id, r.kind) for r in due_reminders(log, base)], [(A, 'quote')])
        self.assertEqual(due_reminders(log, date.fromordinal(base.toordinal() - 1)), [])
        far = date.fromordinal(base.toordinal() + 500)
        self.assertEqual([(r.project_id, r.kind) for r in due_reminders(log, far)], [(A, 'quote'), (B, 'billing')])


class ConfigTests(unittest.TestCase):
    def test_off_without_the_setting_or_without_credentials(self) -> None:
        from t6.crm.config import crm_source
        self.assertIsNone(crm_source({}))
        self.assertIsNone(crm_source({'T6_CRM_SHEET_ID': '  '}))
        self.assertIsNone(crm_source({'T6_CRM_SHEET_ID': 'synthetic'}))  # the worker warns at startup

    def test_sheet_source_reads_with_the_readonly_scope(self) -> None:
        from t6.crm.config import crm_source
        from test_drive_sheets import SheetsHttp, account
        http = SheetsHttp()
        http.tab_rows = {'Log': [list(HEADER)]}
        with tempfile.NamedTemporaryFile('w', suffix='.json') as key, patch('t6.google.http.UrllibHttp', lambda: http), \
                patch('t6.google.http.ServiceAccount.from_json', lambda _: account()):
            key.write('{}')
            key.flush()
            source = crm_source({'T6_CRM_SHEET_ID': 'synthetic-crm', 'GOOGLE_APPLICATION_CREDENTIALS': key.name})
            assert source is not None
            self.assertEqual(source.workbook, 'synthetic-crm')
            self.assertEqual(read_log(source, {A}).by_project, {})
        self.assertEqual(http.scopes, ['https://www.googleapis.com/auth/spreadsheets.readonly'])
        self.assertEqual(http.ids, ['synthetic-crm'])
