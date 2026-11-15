import unittest

from t6.ids import EntityKind, T6Id
from t6.media import MediaStore
from t6.preview import PreviewTokens
from t6.registry import Registered
from t6.verbs import Done, Preview
from test_person import MemoryRegistry


class MediaTests(unittest.TestCase):
    def test_link_list_and_unlink(self) -> None:
        registry = MemoryRegistry()
        self.addCleanup(registry.db.close)
        registry._new_id = lambda: str(T6Id.mint(EntityKind.PROJECT))
        project = registry.register(actor='setup', idempotency_key='setup')
        assert isinstance(project, Registered)
        store = MediaStore(registry, PreviewTokens('secret'))
        values = {'owner': project.project.id, 'kind': 'nas-path', 'ref': '/media', 'role': 'master'}
        preview = store.write('link', values, actor='person', idempotency_key='link')
        self.assertIsInstance(preview, Preview)
        assert isinstance(preview, Preview)
        self.assertEqual(store.list(project.project.id), [])
        result = store.write('link', values, actor='person', idempotency_key='link', confirm=preview.token)
        self.assertIsInstance(result, Done)
        assert isinstance(result, Done)
        identifier = result.result['media_id']
        self.assertEqual(store.list(project.project.id)[0]['id'], identifier)
        unlink = store.write('unlink', {'media': identifier}, actor='person', idempotency_key='unlink')
        assert isinstance(unlink, Preview)
        self.assertIsInstance(store.write('unlink', {'media': identifier}, actor='person', idempotency_key='unlink', confirm=unlink.token), Done)
        self.assertEqual(store.list(project.project.id), [])

    def test_migration_uses_page_bindings_and_reviews_frameio_and_chats(self) -> None:
        registry = MemoryRegistry()
        self.addCleanup(registry.db.close)
        registry._new_id = lambda: str(T6Id.mint(EntityKind.PROJECT))
        project = registry.register(actor='setup', idempotency_key='setup', notion_page_id='project-page')
        assert isinstance(project, Registered)
        store = MediaStore(registry, PreviewTokens('secret'))
        rows = [
            {'page_id': 'resolve-row', 'system': 'resolve', 'kind': 'project', 'ref': 'db/project', 'projects': ['project-page'], 'deliverables': []},
            {'page_id': 'frame-row', 'system': 'Frame.io', 'kind': 'project', 'ref': 'frame', 'projects': ['project-page'], 'deliverables': []},
            {'page_id': 'chat-row', 'system': 'Telegram', 'kind': 'chat', 'ref': 'chat', 'projects': ['project-page'], 'deliverables': []},
        ]
        preview = store.migrate(rows, actor='person')
        self.assertEqual(store.list(project.project.id), [])
        self.assertEqual({r['reason'] for r in preview['review']}, {'Frame.io links are not migrated', 'Telegram chats are not Media locations'})
        result = store.migrate(rows, actor='person', apply=True, digest=preview['digest'])
        self.assertEqual(len(store.list(project.project.id)), 1)
        store.migrate(rows, actor='person', apply=True, digest=preview['digest'])
        self.assertEqual(len(store.list(project.project.id)), 1)
        self.assertEqual(result['entries'][0]['page_id'], 'chat-row')

    def test_one_off_migration_reader_retains_unclassifiable_rows(self) -> None:
        from t6.notion.testing import FakeNotion, external_link_properties
        from t6.media_migration import read_external_links_once_for_migration
        fake = FakeNotion()
        fake.add_data_source('links')
        fake.add_page('links', external_link_properties(system='Frame.io', kind='project', external_id='ref', projects=['page']))
        fake.add_page('links', external_link_properties(system='Telegram', kind='chat', external_id='ref', projects=['page']))
        fake.add_page('links', external_link_properties(system='resolve', kind='project', external_id='db/show', projects=['page']))
        with fake.client() as client:
            rows = read_external_links_once_for_migration(client, 'links')
        self.assertEqual({row['system'] for row in rows}, {'Frame.io', 'Telegram', 'resolve'})
        self.assertEqual(len(rows), 3)

    def test_migration_sends_a_deactivated_row_to_review(self) -> None:
        registry = MemoryRegistry()
        self.addCleanup(registry.db.close)
        registry._new_id = lambda: str(T6Id.mint(EntityKind.PROJECT))
        project = registry.register(actor='setup', idempotency_key='setup', notion_page_id='project-page')
        assert isinstance(project, Registered)
        store = MediaStore(registry, PreviewTokens('secret'))
        rows = [
            {'page_id': 'live-row', 'system': 'resolve', 'kind': 'project', 'ref': 'db/live', 'projects': ['project-page'], 'deliverables': [], 'active': True},
            {'page_id': 'off-row', 'system': 'resolve', 'kind': 'project', 'ref': 'db/off', 'projects': ['project-page'], 'deliverables': [], 'active': False},
        ]
        preview = store.migrate(rows, actor='person')
        self.assertEqual([(r['page_id'], r['reason']) for r in preview['review']], [('off-row', 'Deactivated in External Links')])
        store.migrate(rows, actor='person', apply=True, digest=preview['digest'])
        self.assertEqual([location['ref'] for location in store.list(project.project.id)], ['db/live'])

    def test_one_off_migration_reader_carries_the_active_checkbox(self) -> None:
        from t6.notion.testing import FakeNotion, external_link_properties
        from t6.media_migration import read_external_links_once_for_migration
        fake = FakeNotion()
        fake.add_data_source('links')
        fake.add_page('links', external_link_properties(system='resolve', kind='project', external_id='on', projects=['page'], active=True))
        fake.add_page('links', external_link_properties(system='resolve', kind='project', external_id='off', projects=['page'], active=False))
        with fake.client() as client:
            rows = read_external_links_once_for_migration(client, 'links')
        self.assertEqual({row['ref']: row['active'] for row in rows}, {'on': True, 'off': False})

    def test_link_input_schema_lists_kinds_and_roles(self) -> None:
        from t6.media import KINDS, ROLES
        from t6.verbs import catalogue
        entry, = [v for v in catalogue() if (v['noun'], v['verb']) == ('media', 'link')]
        properties = entry['input_schema']['properties']
        self.assertEqual(set(properties['kind']['enum']), KINDS)
        self.assertEqual(set(properties['role']['enum']), ROLES)

    def test_all_kinds_and_roles_and_retry(self) -> None:
        registry = MemoryRegistry()
        self.addCleanup(registry.db.close)
        registry._new_id = lambda: str(T6Id.mint(EntityKind.PROJECT))
        project = registry.register(actor='setup', idempotency_key='setup')
        assert isinstance(project, Registered)
        deliverable = str(T6Id.mint(EntityKind.DELIVERABLE))
        with registry._transaction() as db:
            db.execute('INSERT INTO deliverables VALUES (?, ?, ?, NULL, NULL, NULL)', (deliverable, project.project.id, 'FTR'))
        store = MediaStore(registry, PreviewTokens('secret'))
        kinds = ['drive', 'nas-path', 'frameio-project', 'frameio-folder', 'resolve-project', 'timeline']
        roles = ['master', 'backup', 'review', 'working', 'frameio-project']
        for index, kind in enumerate(kinds):
            role = 'review' if kind == 'frameio-folder' else roles[(index - (index > 3)) % len(roles)]
            owner = deliverable if kind == 'frameio-folder' else project.project.id
            ref = str(T6Id.mint(EntityKind.DRIVE if kind == 'drive' else EntityKind.TIMELINE)) if kind in ('drive', 'timeline') else 'ref-' + kind
            values = {'owner': owner, 'kind': kind, 'ref': ref, 'role': role}
            key = 'link-' + kind
            preview = store.write('link', values, actor='person', idempotency_key=key)
            assert isinstance(preview, Preview)
            result = store.write('link', values, actor='person', idempotency_key=key, confirm=preview.token)
            assert isinstance(result, Done)
            self.assertIn({'id': result.result['media_id'], **values}, store.list(owner))
            replay = store.write('link', values, actor='person', idempotency_key=key)
            assert isinstance(replay, Preview)
            self.assertEqual(store.write('link', values, actor='person', idempotency_key=key, confirm=replay.token), result)
            unlink = store.write('unlink', {'media': result.result['media_id']}, actor='person', idempotency_key='unlink-' + kind)
            assert isinstance(unlink, Preview)
            self.assertIsInstance(store.write('unlink', {'media': result.result['media_id']}, actor='person', idempotency_key='unlink-' + kind, confirm=unlink.token), Done)
        self.assertEqual(store.list(project.project.id), [])
        self.assertEqual(store.list(deliverable), [])

    def test_changed_link_and_token_mismatch_are_refused(self) -> None:
        from t6.verbs import Refused
        registry = MemoryRegistry()
        self.addCleanup(registry.db.close)
        registry._new_id = lambda: str(T6Id.mint(EntityKind.PROJECT))
        project = registry.register(actor='setup', idempotency_key='setup')
        assert isinstance(project, Registered)
        store = MediaStore(registry, PreviewTokens('secret'))
        values = {'owner': project.project.id, 'kind': 'nas-path', 'ref': '/media', 'role': 'master'}
        first = store.write('link', values, actor='person', idempotency_key='first')
        second = store.write('link', values, actor='person', idempotency_key='second')
        assert isinstance(first, Preview) and isinstance(second, Preview)
        self.assertIsInstance(store.write('link', {**values, 'ref': '/other'}, actor='person', idempotency_key='first', confirm=first.token), Refused)
        self.assertIsInstance(store.write('link', values, actor='other', idempotency_key='first', confirm=first.token), Refused)
        self.assertIsInstance(store.write('link', values, actor='person', idempotency_key='first', confirm=first.token), Done)
        self.assertIsInstance(store.write('link', values, actor='person', idempotency_key='second', confirm=second.token), Refused)
        self.assertIsInstance(store.write('link', values, actor='person', idempotency_key='first', confirm=first.token), Refused)
        self.assertEqual(len(store.list(project.project.id)), 1)

    def test_deliverable_lists_shared_project_locations(self) -> None:
        registry = MemoryRegistry()
        self.addCleanup(registry.db.close)
        registry._new_id = lambda: str(T6Id.mint(EntityKind.PROJECT))
        project = registry.register(actor='setup', idempotency_key='setup')
        assert isinstance(project, Registered)
        deliverable = str(T6Id.mint(EntityKind.DELIVERABLE))
        with registry._transaction() as db:
            db.execute('INSERT INTO deliverables VALUES (?, ?, ?, NULL, NULL, NULL)', (deliverable, project.project.id, 'FTR'))
        store = MediaStore(registry, PreviewTokens('secret'))
        values = {'owner': project.project.id, 'kind': 'frameio-project', 'ref': 'frame-project', 'role': 'frameio-project'}
        preview = store.write('link', values, actor='person', idempotency_key='link')
        assert isinstance(preview, Preview)
        result = store.write('link', values, actor='person', idempotency_key='link', confirm=preview.token)
        assert isinstance(result, Done)
        self.assertEqual([location['id'] for location in store.list(deliverable)], [result.result['media_id']])

    def test_migrate_project_and_its_deliverable_relation(self) -> None:
        registry = MemoryRegistry()
        self.addCleanup(registry.db.close)
        registry._new_id = lambda: str(T6Id.mint(EntityKind.PROJECT))
        project = registry.register(actor='setup', idempotency_key='setup', notion_page_id='project-page')
        assert isinstance(project, Registered)
        deliverable = str(T6Id.mint(EntityKind.DELIVERABLE))
        with registry._transaction() as db:
            db.execute('INSERT INTO deliverables VALUES (?, ?, ?, NULL, NULL, NULL)', (deliverable, project.project.id, 'FTR'))
            db.execute("INSERT INTO bindings (system, page_id, project_id, bound_at, active) VALUES ('notion', 'deliverable-page', ?, 0, 1)", (deliverable,))
        store = MediaStore(registry, PreviewTokens('secret'))
        rows = [{'page_id': 'row', 'system': 'resolve', 'kind': 'project', 'ref': 'db/project', 'projects': ['project-page'], 'deliverables': ['deliverable-page']}]
        preview = store.migrate(rows, actor='person')
        self.assertEqual(preview['review'], [])
        self.assertEqual(preview['entries'][0]['owner'], deliverable)


def linked(registry: 'MemoryRegistry', store: MediaStore, owner: str, kind: str, ref: str, role: str, again: str = '') -> str:
    values = {'owner': owner, 'kind': kind, 'ref': ref, 'role': role}
    key = 'link-' + kind + ref + role + again
    preview = store.write('link', values, actor='person', idempotency_key=key)
    assert isinstance(preview, Preview)
    result = store.write('link', values, actor='person', idempotency_key=key, confirm=preview.token)
    assert isinstance(result, Done)
    return str(result.result['media_id'])


class ReaderTests(unittest.TestCase):
    """Readers outside the verbs ask for Media locations by kind and role."""

    def setUp(self) -> None:
        self.registry = MemoryRegistry()
        self.addCleanup(self.registry.db.close)
        self.registry._new_id = lambda: str(T6Id.mint(EntityKind.PROJECT))
        first = self.registry.register(actor='setup', idempotency_key='one', notion_page_id='page-one')
        second = self.registry.register(actor='setup', idempotency_key='two', notion_page_id='page-two')
        assert isinstance(first, Registered) and isinstance(second, Registered)
        self.first, self.second = first.project.id, second.project.id
        self.store = MediaStore(self.registry, PreviewTokens('secret'))

    def test_active_links_filter_by_kind_and_role_and_carry_the_notion_pages(self) -> None:
        from t6.media import active_links
        frame = linked(self.registry, self.store, self.first, 'frameio-project', 'fp1', 'frameio-project')
        resolve = linked(self.registry, self.store, self.second, 'resolve-project', 'db/show', 'working')
        linked(self.registry, self.store, self.second, 'resolve-project', 'db/old', 'master')
        self.assertEqual([(l.id, l.project_id, l.ref, l.pages) for l in active_links(self.registry, 'frameio-project')],
                         [(frame, self.first, 'fp1', ('page-one',))])
        self.assertEqual([l.id for l in active_links(self.registry, 'resolve-project', 'working')], [resolve])
        self.assertEqual(len(active_links(self.registry, 'resolve-project')), 2)
        self.assertEqual(active_links(self.registry, 'timeline'), ())

    def test_an_unlinked_location_is_not_read_and_a_relink_is_read_again(self) -> None:
        from t6.media import active_links
        media = linked(self.registry, self.store, self.first, 'frameio-project', 'fp1', 'frameio-project')
        unlink = self.store.write('unlink', {'media': media}, actor='person', idempotency_key='off')
        assert isinstance(unlink, Preview)
        self.store.write('unlink', {'media': media}, actor='person', idempotency_key='off', confirm=unlink.token)
        self.assertEqual(active_links(self.registry, 'frameio-project'), ())
        self.assertEqual(linked(self.registry, self.store, self.first, 'frameio-project', 'fp1', 'frameio-project', 'again'), media)
        self.assertEqual(len(active_links(self.registry, 'frameio-project')), 1)

    def test_locations_stay_attached_after_the_notion_page_is_replaced(self) -> None:
        from t6.media import active_links
        linked(self.registry, self.store, self.first, 'frameio-project', 'fp1', 'frameio-project')
        with self.registry._transaction() as db:
            db.execute("UPDATE bindings SET active=0 WHERE page_id='page-one'")
            db.execute("INSERT INTO bindings (system, page_id, project_id, bound_at, active) VALUES ('notion', 'page-new', ?, 1, 1)", (self.first,))
        (link,) = active_links(self.registry, 'frameio-project')
        self.assertEqual((link.project_id, link.pages), (self.first, ('page-new', 'page-one')))

    def test_a_frameio_project_belongs_to_one_project_and_the_refusal_names_the_holder(self) -> None:
        from t6.verbs import Refused
        linked(self.registry, self.store, self.first, 'frameio-project', 'fp1', 'frameio-project')
        values = {'owner': self.second, 'kind': 'frameio-project', 'ref': 'fp1', 'role': 'frameio-project'}
        refused = self.store.write('link', values, actor='person', idempotency_key='second')
        assert isinstance(refused, Refused)
        self.assertEqual(refused.rule, 'frameio_project')
        self.assertIn(self.first, refused.reason)

    def test_find_external_answers_the_old_external_link_lookup_from_media_locations(self) -> None:
        from t6.media import find_external
        media = linked(self.registry, self.store, self.second, 'resolve-project', 'db/show', 'working')
        found = find_external(self.registry, 'Resolve', 'Project', 'db/show')
        assert found is not None
        self.assertEqual((found.id, found.project_id, found.pages), (media, self.second, ('page-two',)))
        self.assertIsNone(find_external(self.registry, 'resolve', 'project', 'missing'))
        self.assertIsNone(find_external(self.registry, 'telegram', 'chat', 'db/show'))

    def test_t6_core_has_no_use_of_the_external_links_reader(self) -> None:
        import re
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent / 'src'
        pattern = re.compile(r'notion\.external_links|\bExternalLinks\b|\bExternalLink\b')
        offenders = [str(path) for path in root.rglob('*.py') if pattern.search(path.read_text())]
        self.assertEqual(offenders, [])
        with self.assertRaises(ImportError):
            __import__('t6.notion.external_links')
