"""Person commands against fake Notion and an in-memory t6 database."""
import io
import json
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

from t6.cli import main
from t6.notion.generated import ContactsRow
from t6.notion.testing import FakeNotion, rich_text, title
from t6.registry.core import normalize_page_id
from t6.registry.sqlite import SqliteProjectRegistry, _migrate


class MemoryRegistry(SqliteProjectRegistry):
    def __init__(self) -> None:
        self.db = sqlite3.connect(':memory:', isolation_level=None)
        self._clock = time.time
        with self._transaction() as db:
            _migrate(db)

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield self.db
        except BaseException:
            self.db.execute('ROLLBACK')
            raise
        else:
            self.db.execute('COMMIT')


class PersonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = MemoryRegistry()
        self.addCleanup(self.registry.db.close)
        self.fake = FakeNotion()
        self.fake.add_snapshot_data_source('contacts', 'contacts')
        row = ContactsRow
        self.first = self.fake.add_page('contacts', {
            row.notion_name('name'): title('Maria Santos'),
            row.notion_name('aliases'): rich_text('Ria'),
            row.notion_name('telegram_user_id'): rich_text('42'),
        })['id']
        self.second = self.fake.add_page('contacts', {row.notion_name('name'): title('Ben Cruz')})['id']
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sources = Path(self.tmp.name) / 'sources.json'
        self.sources.write_text(json.dumps({'contacts': 'contacts'}))
        self.env = {'T6_NOTION_TOKEN': 'fake', 'T6_PREVIEW_SIGNING_KEY': 'test'}

    def command(self, *args: str) -> dict:  # type: ignore[type-arg]
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, self.env, clear=True), \
             patch('t6.registry.SqliteProjectRegistry', return_value=self.registry), \
             patch('t6.cli._open_registry', return_value=self.registry), \
             redirect_stdout(out), redirect_stderr(err):
            status = main(list(args), open_notion=lambda token: self.fake.client())
        self.assertIn(status, (0, 2), err.getvalue())
        self.assertTrue(out.getvalue(), err.getvalue())
        return json.loads(out.getvalue())  # type: ignore[no-any-return]

    def migrate(self, *args: str) -> dict:  # type: ignore[type-arg]
        return self.command('admin', 'person', 'migrate', '--data-sources', str(self.sources), *args)

    def apply(self, preview: dict) -> dict:  # type: ignore[type-arg]
        path = Path(self.tmp.name) / 'preview.json'
        path.write_text(json.dumps(preview))
        return self.migrate('--apply', '--preview', str(path))

    def confirmed(self, verb: str, *args: str) -> dict:  # type: ignore[type-arg]
        preview = self.command('person', verb, *args)
        self.assertEqual(preview['outcome'], 'preview')
        return self.command('person', verb, *args, '--idempotency-key', preview['plan']['idempotency_key'], '--confirm', preview['token'])

    def test_migration_rebinding_and_search_parity(self) -> None:
        preview = self.migrate()
        self.assertEqual(len(preview['entries']), 2)
        self.assertEqual(len(preview['review']), 2)
        self.assertTrue(all(e['kind'] is None and e['problem'] == 'Kind is unknown' for e in preview['review']))
        applied = self.apply(preview)
        ids = {e['page_id']: e['t6_id'] for e in applied['entries']}
        self.assertTrue(all(value.startswith('t6n_') for value in ids.values()))
        second_apply = self.apply(preview)
        self.assertEqual([e['action'] for e in second_apply['entries']], ['keep', 'keep'])
        rerun = self.migrate()
        self.assertEqual([e['action'] for e in rerun['entries']], ['keep', 'keep'])
        self.assertEqual([e['t6_id'] for e in self.apply(rerun)['entries']], [e['t6_id'] for e in rerun['entries']])
        for mention in ('Ria', '42', 'Maria Santoz Cruz', 'no match'):
            contacts = self.command('contacts', 'find', mention, '--data-sources', str(self.sources))
            people = self.command('person', 'find', mention, '--data-sources', str(self.sources))
            for item in ([people['match']] if people['match'] else []) + people['candidates']:
                self.assertEqual(item.pop('t6_id'), ids[normalize_page_id(item['id'])])
            self.assertEqual(people, contacts)
        conflict = self.command('person', 'rebind', '--person-id', ids[normalize_page_id(self.first)], '--page-id', self.second)
        self.assertEqual((conflict['outcome'], conflict['rule']), ('refused', 'binding'))
        replacement = self.fake.add_page('contacts', {ContactsRow.notion_name('name'): title('Renamed Maria')})['id']
        result = self.confirmed('rebind', '--person-id', ids[normalize_page_id(self.first)], '--page-id', replacement)
        self.assertEqual(result['result']['person_id'], ids[normalize_page_id(self.first)])
        self.assertEqual(self.registry.bound_entity('notion', self.first), ids[normalize_page_id(self.first)])
        self.assertEqual(self.registry.bound_entity('notion', replacement), ids[normalize_page_id(self.first)])
        with self.registry._transaction() as db:
            self.assertEqual(db.execute("SELECT active FROM bindings WHERE page_id = ?", (normalize_page_id(self.first),)).fetchone()[0], 0)

    def test_telegram_refusal_names_holder(self) -> None:
        applied = self.apply(self.migrate())
        ids = {e['page_id']: e['t6_id'] for e in applied['entries']}
        result = self.command('person', 'set-telegram', '--person-id', ids[normalize_page_id(self.second)], '--telegram-id', '42')
        self.assertEqual((result['outcome'], result['rule']), ('refused', 'telegram_id'))
        self.assertIn(ids[normalize_page_id(self.first)], result['reason'])
