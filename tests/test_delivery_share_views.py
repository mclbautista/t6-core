from t6.domain.events import JsonValue
from t6.frameio.share_views import DeliveryShareViews, share_viewed_handler
from t6.frameio.worker import FrameioWorker
from t6.notion.testing import package_spec_properties
from t6.worker.client import WorkerJob
from t6.worker.runner import Refused
from test_delivery import DeliveryTests
from test_frameio_worker import FakeFrameio, FakeTokens


def webhook_job(event: str, share: str | None, kind: str = 'share.viewed') -> WorkerJob:
    """A job as the edge queues it: the Frame.io webhook body, keyed by the event."""
    body: dict[str, JsonValue] = {'type': kind, 'account': {'id': 'account-1'}, 'project': {'id': 'frameio-project-1'}, 'user': {'id': 'viewer-1'}}
    if share is not None:
        body['resource'] = {'id': share, 'type': 'share'}
    return WorkerJob(1, 'main', 'frameio.share_viewed', f'edge:/webhook/frameio/shares:{event}', body, 'token', 1)


class DeliveryShareViewTests(DeliveryTests):
    def test_viewed_jobs_only_record_delivery_shares_once(self) -> None:
        self.fake._pages[self.page['id']]['properties'] = package_spec_properties(package_kind='delivery', version='1')
        package = self.deliver('frameio-share', 'delivery-share', 'share-1')['package_id']
        self.deliver('transfer-link', 'other-carrier', 'share-2')
        handler = share_viewed_handler(DeliveryShareViews(self.registry))

        self.assertEqual(handler(webhook_job('event-1', 'share-1')), {'recorded': True})
        self.assertEqual(handler(webhook_job('event-1', 'share-1')), {'recorded': True})
        self.assertEqual(handler(webhook_job('event-2', 'unknown')), {'recorded': False})
        self.assertEqual(handler(webhook_job('event-3', 'share-2')), {'recorded': False})
        entries = [entry for entry in self.registry.activity(self.project) if entry.action == 'delivery_share_viewed']
        self.assertEqual(len(entries), 1)
        self.assertEqual((entries[0].subject_id, entries[0].summary), (package, 'client viewed the delivery share'))
        with self.registry._transaction() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM delivery_share_views').fetchone()[0], 1)

    def test_other_share_events_and_bodies_without_a_share_write_nothing(self) -> None:
        handler = share_viewed_handler(DeliveryShareViews(self.registry))
        self.assertEqual(handler(webhook_job('event-4', 'share-1', kind='share.updated')), {'ignored': 'share.updated'})
        with self.assertRaisesRegex(Refused, 'share id'):
            handler(webhook_job('event-5', None))
        self.assertFalse([entry for entry in self.registry.activity(self.project) if entry.action == 'delivery_share_viewed'])

    def test_the_frameio_worker_has_no_share_view_handler(self) -> None:
        # Only the API and the main worker open the t6 database; the Frame.io worker holds the token and no data.
        worker = FrameioWorker(FakeTokens(), FakeFrameio(), lambda text, key: None)
        self.assertNotIn('frameio.share_viewed', worker.handlers())
