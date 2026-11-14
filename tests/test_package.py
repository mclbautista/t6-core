import unittest
from typing import Any

from t6.ids import EntityKind, T6Id
from t6.notion.package_specs import PackageSpecReader
from t6.notion.testing import FakeNotion, package_spec_properties
from t6.package import PackageStore
from t6.preview import PreviewTokens
from t6.registry import Registered
from t6.verbs import Done, Preview, Refused, catalogue
from test_person import MemoryRegistry


class PackageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = MemoryRegistry()
        self.addCleanup(self.registry.db.close)
        self.registry._new_id = lambda: str(T6Id.mint(EntityKind.PROJECT))
        project = self.registry.register(actor='setup', idempotency_key='setup', notion_page_id='project')
        assert isinstance(project, Registered)
        self.project = project.project.id
        with self.registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES ('sender', 'staff', '[]', '[]', NULL, 0, 1)")
            db.execute("INSERT INTO persons VALUES ('recipient', 'staff', '[]', '[]', NULL, 0, 1)")
            db.execute('INSERT INTO deliverables VALUES (?, ?, ?, NULL, NULL, NULL)', ('delivery', self.project, 'FTR'))
        self.fake = FakeNotion()
        self.fake.add_data_source('specs')
        self.page = self.fake.add_page('specs', package_spec_properties(package_kind='Color', version='1', advisory_items=('Reference',), blocking_items=('EDL',)))
        self.store = PackageStore(self.registry, PreviewTokens('secret'), PackageSpecReader(self.fake.client(), 'specs'))

    def write(self, verb: str, values: dict[str, Any], key: str, actor: str = 'sender') -> dict[str, Any]:
        preview = self.store.write(verb, values, actor=actor, idempotency_key=key)
        assert isinstance(preview, Preview)
        result = self.store.write(verb, values, actor=actor, idempotency_key=key, confirm=preview.token)
        assert isinstance(result, Done)
        retry = self.store.write(verb, values, actor=actor, idempotency_key=key)
        assert isinstance(retry, Preview)
        self.assertEqual(result, self.store.write(verb, values, actor=actor, idempotency_key=key, confirm=retry.token))
        assert isinstance(result.result, dict)
        return result.result

    def test_handoff_snapshot_checklist_and_recipient(self) -> None:
        values = {'deliverable': 'delivery', 'kind': 'Color', 'to': 'Color', 'recipient': 'recipient'}
        package = self.write('prepare', values, 'prepare')['package_id']
        record = self.store.get(package)
        assert record is not None
        self.assertFalse(record['kasado'])
        self.assertEqual(record['spec']['version'], '1')
        self.fake._pages[self.page['id']]['properties'] = package_spec_properties(package_kind='Color', version='2', advisory_items=('Changed',))
        current = self.store.get(package)
        assert current is not None
        self.assertEqual(current['spec']['version'], '1')
        blocked = self.store.write('handoff', {'package': package}, actor='sender')
        assert isinstance(blocked, Refused)
        self.assertIn('EDL', blocked.reason)
        self.write('check', {'package': package, 'item': 'EDL'}, 'check')
        record = self.store.get(package)
        assert record is not None
        self.assertTrue(record['kasado'])
        tick = next(item for item in record['checklist'] if item['text'] == 'EDL')
        self.assertEqual(tick['checked_by'], 'sender')
        self.assertIsNotNone(tick['checked_at'])
        self.write('handoff', {'package': package}, 'handoff')
        self.assertTrue(any('Reference' in entry.summary for entry in self.registry.activity(self.project) if entry.action == 'package_handoff'))
        self.assertEqual([p['id'] for p in self.store.list(missing=True)], [package])
        self.assertIsInstance(self.store.write('accept', {'package': package}, actor='sender'), Refused)
        self.assertIsInstance(self.store.write('reject', {'package': package, 'reason': ''}, actor='recipient'), Refused)
        self.write('accept', {'package': package}, 'accept', actor='recipient')
        self.assertEqual(self.store.list(missing=True), [])

    def test_registered_list_and_check_use_authenticated_person(self) -> None:
        from t6.person import People
        from t6.verbs import PackageCheckInput, PackageListInput, VERBS, invoke
        values = {'deliverable': 'delivery', 'kind': 'Color', 'to': 'Color', 'recipient': 'recipient'}
        package = self.write('prepare', values, 'prepare')['package_id']
        people = People(self.registry, PreviewTokens('secret'))
        check = next(v for v in VERBS if (v.noun, v.verb) == ('package', 'check'))
        listing = next(v for v in VERBS if (v.noun, v.verb) == ('package', 'list'))
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {'T6_PREVIEW_SIGNING_KEY': 'secret'}):
            outcome = invoke(check, PackageCheckInput(package=package, item='EDL'), actor='sender', people=people, open_notion=lambda _: None)
            self.assertIsInstance(outcome, Preview)
            outcome = invoke(listing, PackageListInput(missing=True), actor='recipient', people=people, open_notion=lambda _: None)
            assert isinstance(outcome, Done)
            self.assertEqual(outcome.result['packages'][0]['id'], package)
            self.assertIsInstance(invoke(check, PackageCheckInput(package=package, item='EDL'), actor='unknown', people=people, open_notion=lambda _: None), Refused)

    def test_advisory_only_and_stale_preview(self) -> None:
        self.fake._pages[self.page['id']]['properties'] = package_spec_properties(package_kind='Color', version='2', advisory_items=('Reference',))
        values = {'deliverable': 'delivery', 'kind': 'Color', 'to': 'Color', 'recipient': 'recipient'}
        package = self.write('prepare', values, 'prepare')['package_id']
        current = self.store.get(package)
        assert current is not None
        self.assertTrue(current['kasado'])
        stale = self.store.write('handoff', {'package': package}, actor='sender', idempotency_key='stale')
        assert isinstance(stale, Preview)
        self.write('check', {'package': package, 'item': 'Reference'}, 'check')
        self.assertIsInstance(self.store.write('handoff', {'package': package}, actor='sender', idempotency_key='stale', confirm=stale.token), Refused)
        self.write('handoff', {'package': package}, 'handoff')
        self.write('reject', {'package': package, 'reason': 'Incorrect'}, 'reject', actor='recipient')
        current = self.store.get(package)
        assert current is not None
        self.assertEqual(current['reason'], 'Incorrect')
        self.assertTrue({'prepare', 'check', 'handoff', 'accept', 'reject', 'list'} <= {v['verb'] for v in catalogue() if v['noun'] == 'package'})
