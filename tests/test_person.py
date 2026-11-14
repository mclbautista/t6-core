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

from cli_api_fake import client_patch
from t6.cli import main
from t6.notion.generated import ContactsRow
from t6.notion.testing import FakeNotion, rich_text, title
from t6.person import People
from t6.preview import PreviewTokens
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
        with self.registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES ('test-actor', 'staff', '[]', '[]', NULL, 0, 1)")
            db.execute("INSERT INTO person_permissions VALUES ('test-actor', 'operator')")
        self.fake = FakeNotion()
        patcher = client_patch(lambda _token: self.fake.client())
        patcher.start()
        self.addCleanup(patcher.stop)
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
        self.env = {'T6_NOTION_TOKEN': 'fake', 'T6_PREVIEW_SIGNING_KEY': 'test', 'T6_PERSON_ID': 'test-actor'}
        from t6.verbs import Done, Refused
        def person_get(inputs: object) -> object:
            person = People(self.registry, PreviewTokens('test')).get(getattr(inputs, 'person_id'))
            return Done(result={'person': person}) if person else Refused(rule='person', reason='Person not found')
        api_person = patch('t6.client.Client.person_get', side_effect=person_get)
        api_person.start()
        self.addCleanup(api_person.stop)

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
            self.assertEqual(people, contacts)
            for item in ([people['match']] if people['match'] else []) + people['candidates']:
                self.assertEqual(item.pop('t6_id'), ids[normalize_page_id(item['id'])])
        conflict = self.command('person', 'rebind', '--person-id', ids[normalize_page_id(self.first)], '--page-id', self.second)
        self.assertEqual((conflict['outcome'], conflict['rule']), ('refused', 'binding'))

        replacement = self.fake.add_page('contacts', {ContactsRow.notion_name('name'): title('Renamed Maria')})['id']
        result = self.confirmed('rebind', '--person-id', ids[normalize_page_id(self.first)], '--page-id', replacement)
        self.assertEqual(result['result']['person_id'], ids[normalize_page_id(self.first)])
        self.assertEqual(self.registry.bound_entity('notion', self.first), ids[normalize_page_id(self.first)])
        self.assertEqual(self.registry.bound_entity('notion', replacement), ids[normalize_page_id(self.first)])
        with self.registry._transaction() as db:
            self.assertEqual(db.execute("SELECT active FROM bindings WHERE page_id = ?", (normalize_page_id(self.first),)).fetchone()[0], 0)

    def test_migration_seeds_operators_after_import(self) -> None:
        self.env['T6_OPERATOR_IDS'] = '42,99'
        result = self.apply(self.migrate())
        person_id = next(e['t6_id'] for e in result['entries'] if e['telegram_id'] == 42)
        person = People(self.registry, PreviewTokens('test')).get(person_id)
        assert person is not None
        self.assertIn('operator', person['permissions'])
        self.assertEqual(result['operator_seed']['unmatched'], [99])

    def test_migration_repairs_a_seed_recorded_before_any_person_existed(self) -> None:
        self.env['T6_OPERATOR_IDS'] = '42'
        with self.registry._transaction() as db:
            db.execute("DELETE FROM person_permissions WHERE person_id='test-actor'")
            db.execute("DELETE FROM persons WHERE id='test-actor'")
            db.execute("INSERT INTO permission_seed VALUES (1, '[42]')")
        result = self.apply(self.migrate())
        person_id = next(e['t6_id'] for e in result['entries'] if e['telegram_id'] == 42)
        person = People(self.registry, PreviewTokens('test')).get(person_id)
        assert person is not None
        self.assertIn('operator', person['permissions'])

    def test_migrated_person_can_be_classified_as_staff(self) -> None:
        person_id = self.apply(self.migrate())['entries'][0]['t6_id']
        people = People(self.registry, PreviewTokens('test'))
        self.assertIsNotNone(people.authorize(person_id, 'reader'))
        result = self.confirmed('classify', '--person-id', person_id, '--kind', 'staff')
        self.assertEqual(result['outcome'], 'done')
        self.assertIsNone(people.authorize(person_id, 'reader'))
        self.assertEqual(self.registry.activity(person_id)[0].action, 'person_classify')

    def test_shift_preview_confirm_deadline_and_history(self) -> None:
        applied = self.apply(self.migrate())
        person_id = applied['entries'][0]['t6_id']
        args = (person_id, '--pattern', 'night', '--start', '19:00', '--end', '04:00')
        preview = self.command('person', 'shift', *args)
        self.assertEqual(preview['outcome'], 'preview')
        self.assertIsNone(self.registry.db.execute('SELECT * FROM person_shifts').fetchone())
        result = self.command('person', 'shift', *args, '--idempotency-key', preview['plan']['idempotency_key'], '--confirm', preview['token'])
        self.assertEqual(result['outcome'], 'done')
        result = self.command('deadline', 'end of shift', '--person', person_id, '--posted', '2026-09-01T22:00:00+08:00')
        self.assertEqual(result['deadline'], '2026-09-02T04:00:00+08:00')
        self.assertEqual(self.registry.db.execute("SELECT count(*) FROM audit WHERE project_id = ? AND action = 'person_shift'", (person_id,)).fetchone()[0], 1)
        self.assertEqual(self.registry.db.execute("SELECT count(*) FROM activity WHERE project_id = ? AND action = 'person_shift'", (person_id,)).fetchone()[0], 1)
        self.assertEqual(self.command('person', 'shift', person_id, '--pattern', 'night', '--start', '25:00', '--end', '04:00')['outcome'], 'refused')

    def test_day_shift_and_unset_shift(self) -> None:
        applied = self.apply(self.migrate())
        person_id = applied['entries'][0]['t6_id']
        fallback = self.command('deadline', 'end of shift', '--person', person_id, '--posted', '2026-09-01T10:00:00+08:00')
        self.assertEqual(fallback, self.command('deadline', 'end of shift', '--posted', '2026-09-01T10:00:00+08:00'))
        self.confirmed('shift', person_id, '--pattern', 'day', '--start', '10:00', '--end', '19:00')
        result = self.command('deadline', 'end of shift', '--person', person_id, '--posted', '2026-09-01T10:00:00+08:00')
        self.assertEqual(result['deadline'], '2026-09-01T19:00:00+08:00')

    def test_deadline_person_uses_api(self) -> None:
        from t6.verbs import Done
        out = io.StringIO()
        with patch('t6.client.Client.person_get', return_value=Done(result={'person': {'shift': {'pattern': 'day', 'start': '10:00', 'end': '19:00'}}})) as get, redirect_stdout(out):
            status = main(['deadline', 'end of shift', '--person', 'remote-person', '--posted', '2026-09-01T10:00:00+08:00'])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(out.getvalue())['deadline'], '2026-09-01T19:00:00+08:00')
        self.assertEqual(get.call_args.args[0].person_id, 'remote-person')

    def test_cli_grant_revoke_and_get_permissions(self) -> None:
        applied = self.apply(self.migrate())
        person = applied['entries'][0]['t6_id']
        for action in ('grant', 'revoke'):
            preview = self.command('person', action, person, 'bookings')
            self.assertEqual(preview['outcome'], 'preview')
            result = self.command('person', action, person, 'bookings', '--idempotency-key',
                                  preview['plan']['idempotency_key'], '--confirm', preview['token'])
            self.assertEqual(result['outcome'], 'done')
            shown = self.command('person', 'get', '--person-id', person, '--data-sources', str(self.sources))
            self.assertEqual('bookings' in shown['result']['permissions'], action == 'grant')

    def test_cli_refuses_missing_and_unprivileged_identity(self) -> None:
        with self.registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES ('ordinary', 'staff', '[]', '[]', NULL, 0, 1)")
        self.env['T6_PERSON_ID'] = 'ordinary'
        result = self.command('person', 'grant', 'test-actor', 'bookings')
        self.assertEqual(result['reason'], 'needs operator')
        self.env.pop('T6_PERSON_ID')
        result = self.command('person', 'grant', 'test-actor', 'bookings')
        self.assertEqual(result['rule'], 'permission')

    def test_telegram_refusal_names_holder(self) -> None:
        applied = self.apply(self.migrate())
        ids = {e['page_id']: e['t6_id'] for e in applied['entries']}
        result = self.command('person', 'set-telegram', '--person-id', ids[normalize_page_id(self.second)], '--telegram-id', '42')
        self.assertEqual((result['outcome'], result['rule']), ('refused', 'telegram_id'))
        self.assertIn(ids[normalize_page_id(self.first)], result['reason'])
