"""JO identity and writes at the registry boundary."""
from dataclasses import dataclass
from pathlib import Path
import tempfile
import unittest

from t6.ids import EntityKind, T6Id
from t6.preview import PreviewTokens
from t6.registry import Registered
from test_person import MemoryRegistry


@dataclass
class LegacyOrder:
    page_id: str
    telegram_key: str | None
    project_id: str | None
    name: str = 'Same name'


class JobOrderStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.registry = MemoryRegistry()
        self.registry._new_id = lambda: str(T6Id.mint(EntityKind.PROJECT))
        self.addCleanup(self.registry.db.close)
        project = self.registry.register(actor='setup', idempotency_key='setup', notion_page_id='project-page')
        assert isinstance(project, Registered)
        self.project = project.project.id
        from t6.job_order import JobOrderStore
        self.orders = JobOrderStore(self.registry, PreviewTokens('secret'))

    def test_migration_uses_page_and_telegram_key_and_is_repeatable(self) -> None:
        rows = [LegacyOrder('page-a', '1:2', 'project-page'), LegacyOrder('page-b', None, 'project-page')]
        preview = self.orders.migrate(rows, actor='alice')
        applied = self.orders.migrate(rows, actor='alice', apply=True, digest=preview['digest'])
        ids = {entry['page_id']: entry['t6_id'] for entry in applied['entries']}
        self.assertNotEqual(ids['page-a'], ids['page-b'])
        self.assertTrue(ids['page-a'].startswith('t6j_'))
        found = self.orders.get('page-a')
        assert found is not None
        self.assertEqual(found['project_id'], self.project)
        intents_before = self.registry.pending_sync()
        audit_before = self.registry.audit(ids['page-a'])
        again = self.orders.migrate(rows, actor='alice', apply=True, digest=preview['digest'])
        self.assertEqual([e['action'] for e in again['entries']], ['keep', 'keep'])
        self.assertEqual(self.registry.pending_sync(), intents_before)
        self.assertEqual(self.registry.audit(ids['page-a']), audit_before)
        replacement = [LegacyOrder('page-c', '1:2', 'project-page')]
        plan = self.orders.migrate(replacement, actor='alice')
        self.orders.migrate(replacement, actor='alice', apply=True, digest=plan['digest'])
        current = self.orders.get('page-c')
        assert current is not None
        self.assertEqual({b['page_id']: b['active'] for b in current['bindings']}, {'page-a': False, 'page-c': True})
        for page in ('page-c', 'page-a'):
            found = self.orders.get(page)
            assert found is not None
            self.assertEqual(found['id'], ids['page-a'])

    def test_raise_preview_confirm_and_replay_queue_one_create(self) -> None:
        from t6.verbs import Done, Preview, Refused
        values = {'fields': {'project_id': 'project-page', 'name': 'SHOW - AE - JO #1', 'telegram_key': '3:4'}}
        preview = self.orders.write('raise', values, actor='alice', idempotency_key='raise')
        self.assertIsInstance(preview, Preview)
        assert isinstance(preview, Preview)
        self.assertIsNone(self.orders.get('3:4'))
        done = self.orders.write('raise', values, actor='alice', idempotency_key='raise', confirm=preview.token)
        assert isinstance(done, Done)
        identifier = done.result['jo_id']
        found = self.orders.get(identifier)
        assert found is not None
        self.assertEqual(found['project_id'], self.project)
        replay_preview = self.orders.write('raise', values, actor='alice', idempotency_key='raise')
        assert isinstance(replay_preview, Preview)
        self.assertEqual(done, self.orders.write('raise', values, actor='alice', idempotency_key='raise', confirm=replay_preview.token))
        self.assertIsInstance(self.orders.write('raise', values, actor='alice', idempotency_key='raise', confirm=preview.token), Refused)
        intents = [i for i in self.registry.pending_sync() if i.project_id == identifier]
        self.assertEqual(len(intents), 1)
        self.assertEqual(self.registry.activity(self.project)[0].subject_id, identifier)
        self.assertEqual(self.registry.audit(identifier)[0].action, 'jo_raise')

    def test_assign_reassign_and_note_record_project_activity(self) -> None:
        from t6.person import People
        from t6.verbs import Done, Preview, Refused
        people = People(self.registry, PreviewTokens('secret'))
        person_ids = []
        for index in range(2):
            values = {'page_id': f'person-{index}', 'kind': 'staff'}
            preview = people.write('add', values, actor='alice', idempotency_key=f'p{index}')
            assert isinstance(preview, Preview)
            done = people.write('add', values, actor='alice', idempotency_key=f'p{index}', confirm=preview.token)
            assert isinstance(done, Done)
            person_ids.append(done.result['person_id'])
        raise_values = {'fields': {'project_id': 'project-page', 'name': 'SHOW - AE - JO #1'}}
        preview = self.orders.write('raise', raise_values, actor='alice', idempotency_key='r')
        assert isinstance(preview, Preview)
        done = self.orders.write('raise', raise_values, actor='alice', idempotency_key='r', confirm=preview.token)
        assert isinstance(done, Done)
        jo = done.result['jo_id']
        for verb, values in [('assign', {'jo': jo, 'person': person_ids[0]}),
                             ('reassign', {'jo': jo, 'person': person_ids[1], 'reason': 'handover'}),
                             ('note', {'jo': jo, 'text': 'Check master'})]:
            preview = self.orders.write(verb, values, actor='alice', idempotency_key=verb)
            assert isinstance(preview, Preview)
            self.assertIsInstance(self.orders.write(verb, values, actor='alice', idempotency_key=verb, confirm=preview.token), Done)
        refusal = self.orders.write('assign', {'jo': jo, 'person': person_ids[1]}, actor='alice')
        assert isinstance(refusal, Refused)
        self.assertEqual(refusal.rule, 'assignee')
        found = self.orders.get(jo)
        assert found is not None
        self.assertEqual(found['assignee_id'], person_ids[1])
        self.assertEqual(found['notes'][0]['text'], 'Check master')
        self.assertEqual([e.action for e in self.registry.activity(self.project)[:4]], ['jo_note', 'jo_reassign', 'jo_assign', 'jo_raise'])
        self.assertEqual([e.action for e in self.registry.audit(jo)], ['jo_raise', 'jo_assign', 'jo_reassign', 'jo_note'])

    def test_assignment_preview_rejects_intervening_assignment(self) -> None:
        from t6.verbs import Done, Preview, Refused
        with self.registry._transaction() as db:
            for person in ('first', 'second'):
                db.execute("INSERT INTO persons VALUES (?, 'staff', '[]', '[]', NULL, 0, 1)", (person,))
        values = {'fields': {'project_id': 'project-page', 'name': 'JO'}}
        preview = self.orders.write('raise', values, actor='alice', idempotency_key='raise')
        assert isinstance(preview, Preview)
        done = self.orders.write('raise', values, actor='alice', idempotency_key='raise', confirm=preview.token)
        assert isinstance(done, Done)
        jo = done.result['jo_id']
        pending = self.orders.write('assign', {'jo': jo, 'person': 'first'}, actor='alice', idempotency_key='a')
        assert isinstance(pending, Preview)
        self.assertIsNone(pending.plan['before_assignee'])
        other = self.orders.write('assign', {'jo': jo, 'person': 'second'}, actor='alice', idempotency_key='b')
        assert isinstance(other, Preview)
        self.assertIsInstance(self.orders.write('assign', {'jo': jo, 'person': 'second'}, actor='alice', idempotency_key='b', confirm=other.token), Done)
        result = self.orders.write('assign', {'jo': jo, 'person': 'first'}, actor='alice', idempotency_key='a', confirm=pending.token)
        assert isinstance(result, Refused)
        self.assertEqual(result.rule, 'assignee_changed')

    def test_cli_raise_create_alias_and_get(self) -> None:
        import contextlib
        import io
        import json
        import os
        from unittest.mock import patch
        from t6.cli import main
        from cli_api_fake import client_patch
        from t6.notion.testing import FakeNotion
        fake = FakeNotion()
        fake.add_snapshot_data_source('jos', 'job_orders')
        root = Path(self.tmp.name)
        fields = root / 'jo.json'
        fields.write_text(json.dumps({'project_id': 'project-page', 'name': 'SHOW - AE - JO #1'}))
        sources = root / 'sources.json'
        sources.write_text(json.dumps({'job_orders': 'jos'}))
        from t6.person import People
        actor = str(T6Id.mint(EntityKind.PERSON))
        with self.registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES (?, 'staff', '[]', '[]', NULL, 0, 1)", (actor,))
            db.execute("INSERT INTO person_permissions VALUES (?, 'coordinator')", (actor,))
        people_for_cli = People(self.registry, PreviewTokens('secret'))
        env = {'T6_NOTION_TOKEN': 'fake', 'T6_PREVIEW_SIGNING_KEY': 'secret', 'T6_PERSON_ID': actor}
        def command(*args: str) -> dict:  # type: ignore[type-arg]
            out = io.StringIO()
            with patch.dict(os.environ, env, clear=True), patch('t6.cli._open_registry', return_value=self.registry), patch('t6.verbs._people', return_value=people_for_cli), client_patch(lambda _: fake.client()), contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(list(args), open_notion=lambda _: fake.client()), 0)
            return json.loads(out.getvalue())  # type: ignore[no-any-return]
        args = ('--file', str(fields), '--data-sources', str(sources), '--idempotency-key', 'same')
        preview = command('jo', 'raise', *args)
        self.assertEqual(preview['outcome'], 'preview')
        done = command('jo', 'create', *args, '--confirm', preview['token'])
        self.assertEqual(done['outcome'], 'done')
        got = command('jo', 'get', done['result']['jo_id'], '--data-sources', str(sources))
        self.assertEqual(got['id'], done['result']['jo_id'])
        self.assertEqual(got['name'], 'SHOW - AE - JO #1')
        self.assertEqual(fake.page_count(), 0)
        from t6.person import People
        from t6.verbs import Done, Preview
        people = People(self.registry, PreviewTokens('secret'))
        person_values = {'page_id': 'contact', 'kind': 'staff'}
        p = people.write('add', person_values, actor='setup', idempotency_key='person')
        assert isinstance(p, Preview)
        person = people.write('add', person_values, actor='setup', idempotency_key='person', confirm=p.token)
        assert isinstance(person, Done)
        jo_id = done['result']['jo_id']
        assign = ('jo', 'assign', jo_id, person.result['person_id'])
        assignment = command(*assign)
        self.assertEqual(command(*assign, '--idempotency-key', assignment['plan']['idempotency_key'], '--confirm', assignment['token'])['outcome'], 'done')
        note_args = ('jo', 'note', jo_id, 'A note')
        note = command(*note_args)
        command(*note_args, '--idempotency-key', note['plan']['idempotency_key'], '--confirm', note['token'])
        got = command('jo', 'get', jo_id, '--data-sources', str(sources))
        self.assertEqual(got['assignee_id'], person.result['person_id'])
        self.assertEqual(got['notes'][0]['text'], 'A note')

    def test_admin_migration_cli_previews_and_repeats(self) -> None:
        import contextlib
        import io
        import json
        import os
        from unittest.mock import patch
        from t6.cli import main
        from t6.notion.generated import JobOrdersRow
        from t6.notion.testing import FakeNotion, rich_text, title
        fake = FakeNotion()
        fake.add_snapshot_data_source('jos', 'job_orders')
        page = fake.add_page('jos', {JobOrdersRow.notion_name('name'): title('SHOW - AE - JO #2'), JobOrdersRow.notion_name('telegram_key'): rich_text('1:3')})['id']
        root = Path(self.tmp.name)
        sources = root / 'sources.json'
        sources.write_text(json.dumps({'job_orders': 'jos'}))
        def run(*args: str) -> dict:  # type: ignore[type-arg]
            out = io.StringIO()
            with patch.dict(os.environ, {'T6_NOTION_TOKEN': 'fake'}, clear=True), patch('t6.registry.SqliteProjectRegistry', return_value=self.registry), contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(['admin', 'jo', 'migrate', '--data-sources', str(sources), *args], open_notion=lambda _: fake.client()), 0)
            return json.loads(out.getvalue())  # type: ignore[no-any-return]
        preview = run()
        plan = root / 'plan.json'
        plan.write_text(json.dumps(preview))
        applied = run('--apply', '--preview', str(plan))
        self.assertEqual(self.registry.bound_entity('notion', page), applied['entries'][0]['t6_id'])
        self.assertEqual(run('--apply', '--preview', str(plan))['entries'][0]['action'], 'keep')

    def test_migration_reviews_conflicts_without_matching_names(self) -> None:
        duplicate_key = [LegacyOrder('page-x', '1:2', 'project-page'), LegacyOrder('page-y', '1:2', 'project-page')]
        preview = self.orders.migrate(duplicate_key, actor='alice')
        self.assertEqual([e['action'] for e in preview['entries']], ['review', 'review'])
        applied = self.orders.migrate(duplicate_key, actor='alice', apply=True, digest=preview['digest'])
        self.assertTrue(all(e['t6_id'] is None for e in applied['entries']))
        self.assertIsNone(self.orders.get('page-x'))
        changed = [LegacyOrder('page-x', '1:3', 'project-page')]
        with self.assertRaises(ValueError):
            self.orders.migrate(changed, actor='alice', apply=True, digest=preview['digest'])

    def test_migration_rejects_changed_telegram_key_on_bound_page(self) -> None:
        original = [LegacyOrder('page-a', '1:2', 'project-page')]
        plan = self.orders.migrate(original, actor='alice')
        self.orders.migrate(original, actor='alice', apply=True, digest=plan['digest'])
        changed = self.orders.migrate([LegacyOrder('page-a', '1:3', 'project-page')], actor='alice')
        self.assertEqual(changed['entries'][0]['action'], 'review')

    def test_note_preview_rejects_changed_input_and_replay_is_idempotent(self) -> None:
        from t6.verbs import Done, Preview, Refused
        values = {'fields': {'project_id': 'project-page', 'name': 'SHOW - AE - JO #1'}}
        p = self.orders.write('raise', values, actor='alice', idempotency_key='raise')
        assert isinstance(p, Preview)
        created = self.orders.write('raise', values, actor='alice', idempotency_key='raise', confirm=p.token)
        assert isinstance(created, Done)
        note = {'jo': created.result['jo_id'], 'text': 'First'}
        p = self.orders.write('note', note, actor='alice', idempotency_key='note')
        assert isinstance(p, Preview)
        changed = self.orders.write('note', {**note, 'text': 'Changed'}, actor='alice', idempotency_key='note', confirm=p.token)
        assert isinstance(changed, Refused)
        self.assertEqual(changed.rule, 'token_mismatch')
        self.assertIsInstance(self.orders.write('note', note, actor='alice', idempotency_key='note', confirm=p.token), Done)
        replay = self.orders.write('note', note, actor='alice', idempotency_key='note')
        assert isinstance(replay, Preview)
        self.assertIsInstance(self.orders.write('note', note, actor='alice', idempotency_key='note', confirm=replay.token), Done)
        record = self.orders.get(created.result['jo_id'])
        assert record is not None
        self.assertEqual([item['text'] for item in record['notes']], ['First'])

    def test_migration_resolves_existing_assignee_by_person_binding(self) -> None:
        from t6.person import People
        from t6.verbs import Done, Preview, Refused
        people = People(self.registry, PreviewTokens('secret'))
        person_values = {'page_id': 'contact', 'kind': 'staff'}
        p = people.write('add', person_values, actor='alice', idempotency_key='p')
        assert isinstance(p, Preview)
        person = people.write('add', person_values, actor='alice', idempotency_key='p', confirm=p.token)
        assert isinstance(person, Done)
        from t6.domain import JobOrder
        order = JobOrder(page_id='page-a', name='SHOW - AE - JO #1', project_id='project-page', assigned_ids=('contact',))
        plan = self.orders.migrate([order], actor='alice')
        applied = self.orders.migrate([order], actor='alice', apply=True, digest=plan['digest'])
        jo = applied['entries'][0]['t6_id']
        record = self.orders.get(jo)
        assert record is not None
        self.assertEqual(record['assignee_id'], person.result['person_id'])
        same = self.orders.write('assign', {'jo': jo, 'person': person.result['person_id']}, actor='alice')
        assert isinstance(same, Refused)
        self.assertEqual(same.rule, 'assignee')

    def test_raise_resolves_one_assigned_contact_to_a_person(self) -> None:
        from t6.person import People
        from t6.verbs import Done, Preview, Refused
        people = People(self.registry, PreviewTokens('secret'))
        person_values = {'page_id': 'contact', 'kind': 'staff'}
        p = people.write('add', person_values, actor='alice', idempotency_key='person')
        assert isinstance(p, Preview)
        person = people.write('add', person_values, actor='alice', idempotency_key='person', confirm=p.token)
        assert isinstance(person, Done)
        values = {'fields': {'project_id': 'project-page', 'name': 'SHOW - AE - JO #1', 'assigned_ids': ['contact']}}
        p = self.orders.write('raise', values, actor='alice', idempotency_key='raise')
        assert isinstance(p, Preview)
        raised = self.orders.write('raise', values, actor='alice', idempotency_key='raise', confirm=p.token)
        assert isinstance(raised, Done)
        jo = raised.result['jo_id']
        record = self.orders.get(jo)
        assert record is not None
        self.assertEqual(record['assignee_id'], person.result['person_id'])
        same = self.orders.write('assign', {'jo': jo, 'person': person.result['person_id']}, actor='alice')
        self.assertIsInstance(same, Refused)

    def test_migration_reviews_telegram_key_with_conflicting_project(self) -> None:
        plan = self.orders.migrate([LegacyOrder('page-a', '1:2', 'project-page')], actor='alice')
        self.orders.migrate([LegacyOrder('page-a', '1:2', 'project-page')], actor='alice', apply=True, digest=plan['digest'])
        other = self.registry.register(actor='setup', idempotency_key='other', notion_page_id='other-project')
        assert isinstance(other, Registered)
        plan = self.orders.migrate([LegacyOrder('replacement', '1:2', 'other-project')], actor='alice')
        self.assertEqual(plan['entries'][0]['action'], 'review')
        self.assertIsNone(self.registry.bound_entity('notion', 'replacement'))

    def test_migration_can_link_a_previously_unresolved_project(self) -> None:
        rows = [LegacyOrder('page-a', '1:2', 'later-project')]
        preview = self.orders.migrate(rows, actor='alice')
        first = self.orders.migrate(rows, actor='alice', apply=True, digest=preview['digest'])
        jo_id = first['entries'][0]['t6_id']
        linked = self.registry.register(actor='setup', idempotency_key='later', notion_page_id='later-project')
        assert isinstance(linked, Registered)
        preview = self.orders.migrate(rows, actor='alice')
        self.orders.migrate(rows, actor='alice', apply=True, digest=preview['digest'])
        record = self.orders.get(jo_id)
        assert record is not None
        self.assertEqual(record['project_id'], linked.project.id)
