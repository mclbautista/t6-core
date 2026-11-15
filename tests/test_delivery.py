import unittest
from typing import Any

from t6.notion.testing import package_spec_properties
from t6.verbs import Done, Preview, Refused, catalogue
from test_package import PackageTests


class DeliveryTests(PackageTests):
    def deliver(self, carrier: str, key: str, ref: str | None = None) -> dict[str, Any]:
        values = {'deliverable': 'delivery', 'carrier': carrier, 'ref': ref}
        preview = self.store.deliver(values, actor='sender', idempotency_key=key)
        assert isinstance(preview, Preview)
        result = self.store.deliver(values, actor='sender', idempotency_key=key, confirm=preview.token)
        assert isinstance(result, Done)
        retry = self.store.deliver(values, actor='sender', idempotency_key=key)
        assert isinstance(retry, Preview)
        self.assertEqual(result, self.store.deliver(values, actor='sender', idempotency_key=key, confirm=retry.token))
        assert isinstance(result.result, dict)
        return result.result

    def test_carriers_and_idempotency(self) -> None:
        self.fake._pages[self.page['id']]['properties'] = package_spec_properties(package_kind='delivery', version='1', advisory_items=('Reference',))
        for carrier in ('drive', 'frameio-share', 'transfer-link'):
            result = self.deliver(carrier, carrier, 'reference')
            package = self.store.get(result['package_id'])
            assert package is not None
            self.assertEqual((package['kind'], package['recipient'], package['carrier'], package['ref']), ('delivery', 'client', carrier, 'reference'))
            self.assertEqual(result['missing'], ['Reference'])
        with self.registry._transaction() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM milestones WHERE milestone="delivered"').fetchone()[0], 3)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM delivery_carriers').fetchone()[0], 3)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit WHERE action='deliverable_deliver'").fetchone()[0], 3)
        self.assertIsInstance(self.store.deliver({'deliverable': 'delivery', 'carrier': 'other'}, actor='sender'), Refused)
        self.assertIn(('deliverable', 'deliver'), {(v['noun'], v['verb']) for v in catalogue()})

    def test_blocking_item_can_be_checked_on_prepared_delivery(self) -> None:
        self.fake._pages[self.page['id']]['properties'] = package_spec_properties(package_kind='delivery', version='1', blocking_items=('Master',), advisory_items=('Reference',))
        values = {'deliverable': 'delivery', 'carrier': 'drive', 'ref': None}
        blocked = self.store.deliver(values, actor='sender')
        assert isinstance(blocked, Refused)
        self.assertEqual(blocked.rule, 'checklist')
        package = self.write('prepare', {'deliverable': 'delivery', 'kind': 'delivery', 'to': 'client', 'recipient': 'client'}, 'prepare-delivery')['package_id']
        blocked = self.store.deliver(values, actor='sender')
        assert isinstance(blocked, Refused)
        self.assertEqual(blocked.rule, 'checklist')
        self.write('check', {'package': package, 'item': 'Master'}, 'check-master')
        self.assertEqual(self.deliver('drive', 'deliver')['package_id'], package)

    def waiting_concern(self, deliverable: str, milestone: str) -> str:
        with self.registry._transaction() as db:
            project = db.execute('SELECT project_id FROM deliverables WHERE id = ?', (deliverable,)).fetchone()[0]
            db.execute("INSERT INTO concerns (id, project_id, owner_id, waiting_on, anchor, as_of, opened_at, evidence_at, state) VALUES ('concern', ?, 'owner', 'editor', 'Waiting', '2026-11-01', 0, 0, 'open')", (project,))
            db.execute("INSERT INTO concern_milestones VALUES ('concern', ?, ?)", (deliverable, milestone))
        return 'concern'

    def concern_state(self) -> str:
        with self.registry._transaction() as db:
            state: str = db.execute("SELECT state FROM concerns WHERE id = 'concern'").fetchone()[0]
        return state

    def test_delivery_settles_a_concern_waiting_on_delivered(self) -> None:
        self.fake._pages[self.page['id']]['properties'] = package_spec_properties(package_kind='delivery', version='1')
        with self.registry._transaction() as db:
            deliverable = db.execute('SELECT id FROM deliverables').fetchone()[0]
        self.waiting_concern(deliverable, 'delivered')
        self.deliver('drive', 'settle')
        self.assertEqual(self.concern_state(), 'closed')


if __name__ == '__main__':
    unittest.main()
