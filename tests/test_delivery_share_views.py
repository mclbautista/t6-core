from unittest.mock import Mock

from t6.frameio.share_views import DeliveryShareViews
from t6.frameio.worker import FrameioWorker
from t6.notion.testing import package_spec_properties
from t6.worker.client import WorkerJob
from test_delivery import DeliveryTests


class DeliveryShareViewTests(DeliveryTests):
    def test_viewed_jobs_only_record_delivery_shares_once(self) -> None:
        self.fake._pages[self.page['id']]['properties'] = package_spec_properties(package_kind='delivery', version='1')
        package = self.deliver('frameio-share', 'delivery-share', 'share-1')['package_id']
        self.deliver('transfer-link', 'other-carrier', 'share-2')
        worker = FrameioWorker(Mock(), Mock(), lambda text, key: None,
                               record_share_view=DeliveryShareViews(self.registry).record)
        handler = worker.handlers()['frameio.share_viewed']

        def view(event: str, share: str) -> None:
            handler(WorkerJob(1, 'frameio', 'frameio.share_viewed', event,
                              {'event_id': event, 'share_id': share}, 'token', 1))

        view('event-1', 'share-1')
        view('event-1', 'share-1')
        view('event-2', 'unknown')
        view('event-3', 'share-2')
        entries = [entry for entry in self.registry.activity(self.project) if entry.action == 'delivery_share_viewed']
        self.assertEqual(len(entries), 1)
        self.assertEqual((entries[0].subject_id, entries[0].summary), (package, 'client viewed the delivery share'))
        self.assertEqual(worker.ignored_share_views, 2)
        with self.registry._transaction() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM delivery_share_views').fetchone()[0], 1)
