import unittest

from t6.ids import EntityKind, T6Id
from t6.media import DatabaseMediaLinks, MediaStore
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
        self.assertIn('instagantt-project', properties['kind']['enum'])
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
        kinds = ['drive', 'nas-path', 'frameio-project', 'frameio-folder', 'resolve-project', 'timeline', 'instagantt-project']
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

    def test_an_instagantt_project_id_as_exported_links_through_its_own_kind(self) -> None:
        from t6.instagantt import link_timelines, parse_export
        from t6.media import active_links
        export = 'Project,Project ID,Task Name,Start Date,End Date\nShow Alpha,pr_8Kd2mXq41ZtYw,Cut,2026-01-05,2026-02-13\n'
        media = linked(self.registry, self.store, self.first, 'instagantt-project', 'pr_8Kd2mXq41ZtYw', 'working')
        self.assertEqual([l.id for l in active_links(self.registry, 'instagantt-project', 'working')], [media])
        report = link_timelines(parse_export(export), DatabaseMediaLinks(self.registry))
        self.assertEqual([(l.timeline.project, l.project_id) for l in report.linked], [('Show Alpha', self.first)])
        self.assertEqual(report.unmapped, ())

    def test_the_same_instagantt_id_as_kind_timeline_is_refused_at_media_link(self) -> None:
        from t6.verbs import Refused
        values = {'owner': self.first, 'kind': 'timeline', 'ref': 'pr_8Kd2mXq41ZtYw', 'role': 'working'}
        refused = self.store.write('link', values, actor='person', idempotency_key='bad')
        assert isinstance(refused, Refused)
        self.assertEqual(refused.rule, 'ref')

    def test_an_instagantt_project_belongs_to_one_project_and_the_refusal_names_the_holder(self) -> None:
        from t6.verbs import Refused
        linked(self.registry, self.store, self.first, 'instagantt-project', 'pr_one', 'working')
        values = {'owner': self.second, 'kind': 'instagantt-project', 'ref': 'pr_one', 'role': 'working'}
        refused = self.store.write('link', values, actor='person', idempotency_key='second')
        assert isinstance(refused, Refused)
        self.assertEqual(refused.rule, 'instagantt_project')
        self.assertIn(self.first, refused.reason)
        other = {'owner': self.second, 'kind': 'instagantt-project', 'ref': 'pr_two', 'role': 'working'}
        self.assertIsInstance(self.store.write('link', other, actor='person', idempotency_key='other'), Preview)

    def test_find_external_maps_both_old_instagantt_kinds_to_the_instagantt_project_kind(self) -> None:
        from t6.media import find_external
        media = linked(self.registry, self.store, self.second, 'instagantt-project', 'pr_x', 'working')
        for kind in ('Project', 'Timeline'):
            found = find_external(self.registry, 'Instagantt', kind, 'pr_x')
            assert found is not None
            self.assertEqual((found.id, found.kind, found.project_id), (media, 'instagantt-project', self.second))
        self.assertIsNone(find_external(self.registry, 'Instagantt', 'project', 'missing'))

    def test_migration_maps_active_instagantt_rows_to_the_instagantt_project_kind(self) -> None:
        rows = [
            {'page_id': 'ig-project', 'system': 'Instagantt', 'kind': 'project', 'ref': 'pr_a', 'active': True, 'projects': ['page-one'], 'deliverables': []},
            {'page_id': 'ig-timeline', 'system': 'Instagantt', 'kind': 'timeline', 'ref': 'pr_b', 'active': True, 'projects': ['page-two'], 'deliverables': []},
            {'page_id': 'ig-off', 'system': 'Instagantt', 'kind': 'project', 'ref': 'pr_c', 'active': False, 'projects': ['page-one'], 'deliverables': []},
            {'page_id': 'ig-taken', 'system': 'Instagantt', 'kind': 'project', 'ref': 'pr_a', 'active': True, 'projects': ['page-two'], 'deliverables': []},
        ]
        preview = self.store.migrate(rows, actor='person')
        self.assertEqual({e['page_id']: e['action'] for e in preview['entries']},
                         {'ig-project': 'register', 'ig-timeline': 'register', 'ig-off': 'review', 'ig-taken': 'review'})
        self.assertEqual({e['page_id']: e['reason'] for e in preview['review']},
                         {'ig-off': 'Deactivated in External Links', 'ig-taken': 'Instagantt project already belongs to another Project'})
        self.store.migrate(rows, actor='person', apply=True, digest=preview['digest'])
        from t6.media import active_links
        self.assertEqual(sorted((l.ref, l.role, l.project_id) for l in active_links(self.registry, 'instagantt-project')),
                         sorted([('pr_a', 'working', self.first), ('pr_b', 'working', self.second)]))
        self.assertEqual(active_links(self.registry, 'timeline'), ())

    def test_a_row_migrated_earlier_and_deactivated_later_goes_to_review_and_is_not_unlinked(self) -> None:
        from t6.media import active_links
        row = {'page_id': 'ig-project', 'system': 'Instagantt', 'kind': 'project', 'ref': 'pr_a', 'active': True, 'projects': ['page-one'], 'deliverables': []}
        first = self.store.migrate([row], actor='person')
        self.store.migrate([row], actor='person', apply=True, digest=first['digest'])
        (kept,) = active_links(self.registry, 'instagantt-project')
        later = self.store.migrate([{**row, 'active': False}], actor='person')
        (entry,) = later['entries']
        self.assertEqual((entry['action'], entry['reason']), ('review', 'Deactivated after migration'))
        self.assertEqual(later['review'], [entry])
        self.store.migrate([{**row, 'active': False}], actor='person', apply=True, digest=later['digest'])
        self.assertEqual([link.id for link in active_links(self.registry, 'instagantt-project')], [kept.id])

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


class FrameioProjectLinkTests(unittest.TestCase):
    """`frameio project link` and `unlink` wrap the Media location rules for kind frameio-project."""

    def setUp(self) -> None:
        from t6.registry import Rotation
        self.registry = MemoryRegistry()
        self.addCleanup(self.registry.db.close)
        self.registry._new_id = lambda: str(T6Id.mint(EntityKind.PROJECT))
        first = self.registry.register(actor='setup', idempotency_key='a', notion_page_id='page-one')
        second = self.registry.register(actor='setup', idempotency_key='b', notion_page_id='page-two')
        assert isinstance(first, Registered) and isinstance(second, Registered)
        self.first, self.second = first.project.id, second.project.id
        self.store = MediaStore(self.registry, PreviewTokens('secret'))
        self.rotation = Rotation

    def write(self, verb: str, project: str, ref: str, key: str, actor: str = 'person') -> Done:
        values = {'project': project, 'frameio_project': ref}
        preview = self.store.frameio_project_write(verb, values, actor=actor, idempotency_key=key)
        assert isinstance(preview, Preview)
        done = self.store.frameio_project_write(verb, values, actor=actor, idempotency_key=key, confirm=preview.token)
        assert isinstance(done, Done)
        return done

    def audit_actions(self) -> list[str]:
        with self.registry._transaction() as db:
            return [row[0] for row in db.execute("SELECT action FROM audit WHERE action LIKE 'frameio_project_%' ORDER BY id")]

    def test_link_previews_then_confirms_as_a_frameio_project_location_and_is_idempotent(self) -> None:
        from t6.media import active_links
        values = {'project': self.first, 'frameio_project': 'fp1'}
        preview = self.store.frameio_project_write('link', values, actor='person', idempotency_key='k')
        assert isinstance(preview, Preview)
        self.assertEqual(active_links(self.registry, 'frameio-project'), ())
        done = self.store.frameio_project_write('link', values, actor='person', idempotency_key='k', confirm=preview.token)
        assert isinstance(done, Done)
        (link,) = active_links(self.registry, 'frameio-project', 'frameio-project')
        self.assertEqual((link.id, link.project_id, link.ref), (done.result['media_id'], self.first, 'fp1'))
        replay = self.store.frameio_project_write('link', values, actor='person', idempotency_key='k')
        assert isinstance(replay, Preview)
        again = self.store.frameio_project_write('link', values, actor='person', idempotency_key='k', confirm=replay.token)
        self.assertEqual(again, done)
        self.assertEqual(self.audit_actions(), ['frameio_project_link'])

    def test_second_show_is_refused_naming_the_holder_and_a_show_may_hold_many(self) -> None:
        from t6.media import active_links
        from t6.verbs import Refused
        self.write('link', self.first, 'fp1', 'one')
        self.write('link', self.first, 'fp2', 'two')
        self.assertEqual({l.ref for l in active_links(self.registry, 'frameio-project')}, {'fp1', 'fp2'})
        refused = self.store.frameio_project_write('link', {'project': self.second, 'frameio_project': 'fp1'}, actor='person', idempotency_key='three')
        assert isinstance(refused, Refused)
        self.assertEqual(refused.rule, 'frameio_project')
        self.assertIn(self.first, refused.reason)

    def test_an_unlink_retried_with_the_same_key_returns_the_original_result(self) -> None:
        self.write('link', self.first, 'fp1', 'one')
        done = self.write('unlink', self.first, 'fp1', 'two')
        values = {'project': self.first, 'frameio_project': 'fp1'}
        replay = self.store.frameio_project_write('unlink', values, actor='person', idempotency_key='two')
        assert isinstance(replay, Preview)
        again = self.store.frameio_project_write('unlink', values, actor='person', idempotency_key='two', confirm=replay.token)
        assert isinstance(again, Done)
        self.assertEqual(again.result, done.result)
        self.assertEqual(self.audit_actions(), ['frameio_project_link', 'frameio_project_unlink'])
        from t6.verbs import Refused
        other = self.store.frameio_project_write('unlink', values, actor='person', idempotency_key='fresh')
        self.assertIsInstance(other, Refused)  # a new key still sees that nothing is linked

    def test_the_frameio_project_ref_is_trimmed_and_must_have_the_shape_of_an_id(self) -> None:
        from t6.media import active_links
        from t6.verbs import Refused
        for bad in ('', '   ', 'two words', 'fp/1', 'fp1?x=1', 'a' * 129):
            refused = self.store.frameio_project_write('link', {'project': self.first, 'frameio_project': bad}, actor='person', idempotency_key='bad')
            self.assertIsInstance(refused, Refused, bad)
            refused = self.store.write('link', {'owner': self.first, 'kind': 'frameio-project', 'ref': bad, 'role': 'frameio-project'}, actor='person', idempotency_key='bad2')
            self.assertIsInstance(refused, Refused, bad)
        self.write('link', self.first, ' fp1 ', 'one')
        self.assertEqual([l.ref for l in active_links(self.registry, 'frameio-project')], ['fp1'])
        self.write('unlink', self.first, ' fp1', 'two')
        self.assertEqual(active_links(self.registry, 'frameio-project'), ())

    def test_a_padded_frameio_ref_cannot_have_a_second_holder_by_either_verb(self) -> None:
        from t6.verbs import Refused
        self.write('link', self.first, 'fp1', 'one')
        padded = self.store.frameio_project_write('link', {'project': self.second, 'frameio_project': ' fp1'}, actor='person', idempotency_key='two')
        assert isinstance(padded, Refused)
        self.assertEqual(padded.rule, 'frameio_project')
        media = self.store.write('link', {'owner': self.second, 'kind': 'frameio-project', 'ref': ' fp1', 'role': 'frameio-project'}, actor='person', idempotency_key='three')
        assert isinstance(media, Refused)
        self.assertEqual(media.rule, 'frameio_project')
        self.assertIn(self.first, media.reason)

    def test_moving_is_unlink_then_link_as_two_audited_steps(self) -> None:
        from t6.media import active_links
        self.write('link', self.first, 'fp1', 'one')
        self.write('unlink', self.first, 'fp1', 'two')
        self.assertEqual(active_links(self.registry, 'frameio-project'), ())
        self.write('link', self.second, 'fp1', 'three')
        (link,) = active_links(self.registry, 'frameio-project')
        self.assertEqual(link.project_id, self.second)
        self.assertEqual(self.audit_actions(), ['frameio_project_link', 'frameio_project_unlink', 'frameio_project_link'])
        with self.registry._transaction() as db:
            activity = [r[0] for r in db.execute("SELECT action FROM activity WHERE action LIKE 'frameio_project_%'")]
        self.assertEqual(len(activity), 3)

    def test_unlink_of_something_not_linked_and_bad_input_are_refused(self) -> None:
        from t6.verbs import Refused
        for verb, values in (('unlink', {'project': self.first, 'frameio_project': 'nope'}), ('link', {'project': self.first, 'frameio_project': ' '}),
                             ('link', {'project': 'not-an-id', 'frameio_project': 'fp1'}), ('move', {'project': self.first, 'frameio_project': 'fp1'})):
            refused = self.store.frameio_project_write(verb, values, actor='person', idempotency_key='x' + verb)
            self.assertIsInstance(refused, Refused, (verb, values))

    def test_rotation_change_leaves_links_in_place(self) -> None:
        from t6.media import active_links
        self.write('link', self.first, 'fp1', 'one')
        self.registry.set_rotation(self.first, self.rotation.IN, actor='service', idempotency_key='in', expected_revision=1)
        self.registry.set_rotation(self.first, self.rotation.OUT, actor='service', idempotency_key='out', expected_revision=2)
        (link,) = active_links(self.registry, 'frameio-project')
        self.assertEqual((link.project_id, link.ref), (self.first, 'fp1'))
