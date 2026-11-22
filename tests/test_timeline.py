"""Timeline link, restamp and list against the in-memory t6 database, a fake Resolve reader and a fake Resolve worker."""
import json
import time
import unittest
from typing import Any

from t6.deliverable import DeliverableStore
from t6.frameio.model import Version
from t6.ids import EntityKind, T6Id
from t6.media import MediaStore
from t6.preview import PreviewTokens
from t6.registry import Registered
from t6.queue_dismiss import JobDismissals
from t6.resolve.stamp_jobs import LEASE_EXPIRED, ResolveStampJobs
from t6.resolve.testing import FakeTimelineReader
from t6.resolve.timelines import ResolveTimeline
from t6.timeline import TimelineStore
from t6.verbs import Ambiguous, Done, Preview, Refused, catalogue
from t6.version import VersionStore
from test_person import MemoryRegistry

RESOLVE_REF = 'db/folder/show'
STAMP = 't6t_' + 'a' * 32
OTHER_STAMP = 't6t_' + 'b' * 32


class Reader:
    def versions(self, file_id: str) -> tuple[Version, ...]:
        return (Version(file_id=file_id, name='cut', stack_id='stack'),)


class TimelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = MemoryRegistry()
        self.addCleanup(self.registry.db.close)
        self.registry._new_id = lambda: str(T6Id.mint(EntityKind.PROJECT))
        self.tokens = PreviewTokens('secret')
        self.project = self.register('p')
        self.media = MediaStore(self.registry, self.tokens)
        self.resolve_media = self.link_resolve(self.project, RESOLVE_REF)
        self.resolve = FakeTimelineReader({RESOLVE_REF: [ResolveTimeline(uuid='u1', name='Ep1 v1', stamp=STAMP)]})
        self.store = TimelineStore(self.registry, self.tokens, self.resolve)

    def register(self, page: str) -> str:
        project = self.registry.register(actor='setup', idempotency_key='setup-' + page, notion_page_id=page)
        assert isinstance(project, Registered)
        return project.project.id

    def link_resolve(self, project: str, ref: str) -> str:
        values = {'owner': project, 'kind': 'resolve-project', 'ref': ref, 'role': 'working'}
        preview = self.media.write('link', values, actor='person', idempotency_key='resolve-' + project)
        assert isinstance(preview, Preview)
        done = self.media.write('link', values, actor='person', idempotency_key='resolve-' + project, confirm=preview.token)
        assert isinstance(done, Done)
        return str(done.result['media_id'])

    def link(self, name: str, stamp: str, key: str, project: str | None = None, store: TimelineStore | None = None) -> Done:
        values = {'project': project or self.project, 'timeline': name, 'stamp': stamp}
        store = store or self.store
        preview = store.write(values, actor='alice', idempotency_key=key)
        assert isinstance(preview, Preview), preview
        done = store.write(values, actor='alice', idempotency_key=key, confirm=preview.token)
        assert isinstance(done, Done), done
        return done

    def jobs(self) -> list[dict[str, Any]]:
        return [json.loads(r[0]) for r in self.registry.db.execute("SELECT payload FROM internal_jobs WHERE kind='resolve.stamp' ORDER BY id")]

    def work(self, failure: str | None = None) -> None:
        """A fake Resolve worker: claim the next stamp job and report it written, or fail it with `failure`."""
        jobs = ResolveStampJobs(self.registry)
        claim = jobs.claim('resolve')
        assert claim is not None
        if failure:
            jobs.fail(claim.job_id, claim.token, failure, 'resolve')
        else:
            jobs.report(claim.job_id, claim.token, {'uuid': 'u', 'name': str(claim.payload['timeline']), 'stamp': str(claim.payload['stamp'])}, 'resolve')

    def state(self, stamp: str) -> tuple[str, str | None]:
        return next((t['stamp_state'], t['stamp_reason']) for t in self.store.list(self.project) if t['id'] == stamp)

    def test_confirm_records_the_timeline_pending_and_queues_one_stamp_job(self) -> None:
        values = {'project': self.project, 'timeline': 'Ep1 v1', 'stamp': STAMP}
        preview = self.store.write(values, actor='alice', idempotency_key='one')
        assert isinstance(preview, Preview)
        self.assertEqual(self.jobs(), [])
        done = self.store.write(values, actor='alice', idempotency_key='one', confirm=preview.token)
        assert isinstance(done, Done)
        self.assertEqual(done.result['stamp_state'], 'pending')
        self.assertIsInstance(done.result['stamp_job'], int)
        self.assertEqual(self.state(STAMP), ('pending', None))
        self.assertEqual(self.jobs(), [{'project': self.project, 'resolve_project': RESOLVE_REF, 'timeline': 'Ep1 v1', 'stamp': STAMP}])
        # A retried confirm returns the first result and queues nothing more; nor does linking the stamp again.
        self.assertEqual(self.store.write(values, actor='alice', idempotency_key='one', confirm=preview.token), done)
        again = self.link('Ep1 v1', STAMP, 'two')
        self.assertEqual((again.result['created'], again.result['stamp_state'], again.result['stamp_job']), (False, 'pending', None))
        self.assertEqual(len(self.jobs()), 1)

    def test_the_stamp_job_marks_the_timeline_stamped_or_failed_and_a_failed_one_is_linked_again(self) -> None:
        self.link('Ep1 v1', STAMP, 'one')
        self.work()
        self.assertEqual(self.state(STAMP), ('stamped', None))
        self.resolve.add(RESOLVE_REF, ResolveTimeline(uuid='u2', name='Unstamped', stamp=None))
        values = {'project': self.project, 'timeline': 'Unstamped'}
        preview = self.store.write(values, actor='alice', idempotency_key='gen')
        assert isinstance(preview, Preview)
        stamp = preview.plan['stamp']
        self.assertIsInstance(self.store.write(values, actor='alice', idempotency_key='gen', confirm=preview.token), Done)
        self.work('Resolve is not running')
        self.assertEqual(self.state(stamp), ('failed', 'Resolve is not running'))
        # Linking the failed Timeline again (new key) queues it again.
        relinked = self.link('Unstamped', stamp, 'again')
        self.assertEqual((relinked.result['created'], relinked.result['stamp_state']), (False, 'pending'))
        self.assertEqual(self.state(stamp), ('pending', None))
        self.assertEqual([j['stamp'] for j in self.jobs()], [STAMP, stamp, stamp])
        self.work()
        self.assertEqual(self.state(stamp), ('stamped', None))

    def test_restamp_gives_the_duplicate_a_new_stamp_through_a_preview(self) -> None:
        self.link('Ep1 v1', STAMP, 'one')
        self.work()
        self.resolve.add(RESOLVE_REF, ResolveTimeline(uuid='u9', name='Ep1 v1 copy', stamp=STAMP))  # duplicated in Resolve
        values = {'project': self.project, 'timeline': 'Ep1 v1 copy', 'stamp': STAMP}
        preview = self.store.restamp(values, actor='alice', idempotency_key='re')
        assert isinstance(preview, Preview), preview
        new = preview.plan['stamp']
        T6Id.parse(new, EntityKind.TIMELINE)
        self.assertNotEqual(new, STAMP)
        self.assertEqual(preview.plan['replaces'], STAMP)
        again = self.store.restamp(values, actor='alice', idempotency_key='re')
        assert isinstance(again, Preview)
        self.assertEqual(again.plan['stamp'], new)
        self.assertEqual(len(self.jobs()), 1)
        done = self.store.restamp(values, actor='alice', idempotency_key='re', confirm=preview.token)
        assert isinstance(done, Done), done
        self.assertEqual((done.result['timeline_id'], done.result['stamp_state']), (new, 'pending'))
        self.assertEqual(self.jobs()[1], {'project': self.project, 'resolve_project': RESOLVE_REF, 'timeline': 'Ep1 v1 copy', 'stamp': new, 'replaces': STAMP, 'canonical': 'u'})
        self.assertEqual(self.store.restamp(values, actor='alice', idempotency_key='re', confirm=preview.token), done)
        self.assertEqual(len(self.jobs()), 2)
        self.assertEqual({t['id']: t['stamp_state'] for t in self.store.list(self.project)}, {STAMP: 'stamped', new: 'pending'})
        self.work()
        self.assertEqual(self.state(new), ('stamped', None))
        self.assertEqual([a.action for a in self.registry.activity(self.project)][0], 'timeline_restamp')

    def test_restamp_refusals(self) -> None:
        self.link('Ep1 v1', STAMP, 'one')

        def rule(**changes: str) -> str:
            outcome = self.store.restamp({'project': self.project, 'timeline': 'Ep1 v1 copy', 'stamp': STAMP, **changes}, actor='alice')
            assert isinstance(outcome, Refused), outcome
            return outcome.rule
        self.assertEqual(rule(timeline='Ep1 v1'), 'timeline')  # the canonical Timeline keeps its stamp
        self.assertEqual(rule(stamp=OTHER_STAMP), 'timeline_stamp')
        self.assertEqual(rule(stamp='nope'), 'stamp')
        self.assertEqual(rule(project=self.register('q')), 'timeline_stamp')
        self.assertEqual(rule(timeline=' '), 'input')
        self.assertEqual(len(self.store.list(self.project)), 1)

    def test_restamp_refuses_a_bad_project_and_an_inactive_resolve_project(self) -> None:
        self.link('Ep1 v1', STAMP, 'one')
        values = {'project': self.project, 'timeline': 'Ep1 v1 copy', 'stamp': STAMP}
        bad = self.store.restamp({**values, 'project': 'nope'}, actor='alice')
        assert isinstance(bad, Refused)
        self.assertEqual(bad.rule, 'project')
        self.registry.db.execute('UPDATE media_locations SET active=0 WHERE id=?', (self.resolve_media,))
        inactive = self.store.restamp(values, actor='alice')
        assert isinstance(inactive, Refused)
        self.assertEqual(inactive.rule, 'resolve_project')

    def test_restamp_refuses_the_canonical_by_its_verified_resolve_id_and_the_job_carries_that_id(self) -> None:
        self.link('Ep1 v1', STAMP, 'one')
        self.work()  # the fake worker reports Resolve ID 'u'
        renamed = self.store.restamp({'project': self.project, 'timeline': 'u', 'stamp': STAMP}, actor='alice')
        assert isinstance(renamed, Refused)
        self.assertEqual(renamed.rule, 'timeline')
        values = {'project': self.project, 'timeline': 'Ep1 v1 copy', 'stamp': STAMP}
        preview = self.store.restamp(values, actor='alice', idempotency_key='re')
        assert isinstance(preview, Preview), preview
        self.assertIsInstance(self.store.restamp(values, actor='alice', idempotency_key='re', confirm=preview.token), Done)
        self.assertEqual(self.jobs()[-1]['canonical'], 'u')

    def test_a_failed_restamp_linked_again_still_replaces_the_duplicated_stamp(self) -> None:
        self.link('Ep1 v1', STAMP, 'one')
        self.work()
        self.resolve.add(RESOLVE_REF, ResolveTimeline(uuid='u9', name='Ep1 v1 copy', stamp=STAMP))
        values = {'project': self.project, 'timeline': 'Ep1 v1 copy', 'stamp': STAMP}
        preview = self.store.restamp(values, actor='alice', idempotency_key='re')
        assert isinstance(preview, Preview), preview
        new = preview.plan['stamp']
        self.assertIsInstance(self.store.restamp(values, actor='alice', idempotency_key='re', confirm=preview.token), Done)
        self.work('Resolve is not running')
        relinked = self.link('Ep1 v1 copy', new, 'again')  # the copy still carries the stamp it replaces
        self.assertEqual(relinked.result['stamp_state'], 'pending')
        self.assertEqual(self.jobs()[-1], {'project': self.project, 'resolve_project': RESOLVE_REF, 'timeline': 'Ep1 v1 copy', 'stamp': new, 'replaces': STAMP, 'canonical': 'u'})

    def test_a_stamp_job_whose_worker_stopped_fails_its_timeline_and_is_never_run_again(self) -> None:
        self.link('Ep1 v1', STAMP, 'one')
        claim = ResolveStampJobs(self.registry).claim('resolve')
        assert claim is not None
        later = time.time() + 301  # the lease ran out: the worker stopped mid-job
        self.registry._clock = lambda: later
        self.assertEqual(self.state(STAMP), ('failed', LEASE_EXPIRED))
        self.assertIsNone(ResolveStampJobs(self.registry).claim('resolve'))
        self.assertEqual([j['job'] for j in JobDismissals(self.registry, self.tokens).failed()], [claim.job_id])
        relinked = self.link('Ep1 v1', STAMP, 'again')
        self.assertEqual((relinked.result['created'], relinked.result['stamp_state']), (False, 'pending'))
        self.work()
        self.assertEqual(self.state(STAMP), ('stamped', None))

    def test_a_stamped_timeline_without_its_marker_is_not_verified(self) -> None:
        self.link('Ep1 v1', STAMP, 'one')
        self.work()
        self.store.reader = FakeTimelineReader({RESOLVE_REF: [ResolveTimeline(uuid='u1', name='Ep1 v1', stamp=None)]})  # the marker is gone
        refused = self.store.write({'project': self.project, 'timeline': 'Ep1 v1', 'stamp': STAMP}, actor='alice', idempotency_key='gone')
        assert isinstance(refused, Refused), refused
        self.assertEqual(refused.rule, 'stamp')

    def test_linking_a_failed_timeline_again_without_a_stamp_keeps_its_stamp(self) -> None:
        self.resolve.add(RESOLVE_REF, ResolveTimeline(uuid='u2', name='Unstamped', stamp=None))
        values = {'project': self.project, 'timeline': 'Unstamped'}
        preview = self.store.write(values, actor='alice', idempotency_key='gen')
        assert isinstance(preview, Preview)
        stamp = preview.plan['stamp']
        self.assertIsInstance(self.store.write(values, actor='alice', idempotency_key='gen', confirm=preview.token), Done)
        self.work('Resolve is not running')
        again = self.store.write(values, actor='alice', idempotency_key='gen2')
        assert isinstance(again, Preview), again
        self.assertEqual(again.plan['stamp'], stamp)
        done = self.store.write(values, actor='alice', idempotency_key='gen2', confirm=again.token)
        assert isinstance(done, Done), done
        self.assertEqual((done.result['created'], done.result['stamp_state']), (False, 'pending'))
        self.assertEqual([t['id'] for t in self.store.list(self.project) if t['id'] != STAMP], [stamp])

    def test_a_failed_timeline_linked_again_under_its_new_name_updates_the_label_with_audit(self) -> None:
        self.resolve.add(RESOLVE_REF, ResolveTimeline(uuid='u2', name='Unstamped', stamp=None))
        values = {'project': self.project, 'timeline': 'Unstamped'}
        preview = self.store.write(values, actor='alice', idempotency_key='gen')
        assert isinstance(preview, Preview)
        stamp = preview.plan['stamp']
        self.assertIsInstance(self.store.write(values, actor='alice', idempotency_key='gen', confirm=preview.token), Done)
        self.work('Resolve is not running')
        self.resolve.rename(RESOLVE_REF, 'u2', 'Renamed')
        self.link('Renamed', stamp, 'again')
        self.assertEqual(next(t['canonical']['name'] for t in self.store.list(self.project) if t['id'] == stamp), 'Renamed')
        self.assertEqual(self.jobs()[-1]['timeline'], 'Renamed')
        audit = self.registry.audit(stamp)
        self.assertEqual([a.action for a in audit], ['timeline_link', 'timeline_link'])
        self.assertEqual(audit[-1].before, {'label': 'Unstamped', 'stamp_state': 'failed', 'stamp_reason': 'Resolve is not running'})

    def test_link_records_a_timeline_once_and_list_shows_its_canonical_copy(self) -> None:
        values = {'project': self.project, 'timeline': 'Ep1 v1', 'stamp': STAMP}
        preview = self.store.write(values, actor='alice', idempotency_key='one')
        assert isinstance(preview, Preview)
        self.assertEqual(self.store.list(self.project), [])
        done = self.store.write(values, actor='alice', idempotency_key='one', confirm=preview.token)
        assert isinstance(done, Done)
        self.assertEqual(done.result['timeline_id'], STAMP)
        self.assertTrue(done.result['created'])
        # Replay of the same key gives the same result; a new key for the same stamp creates nothing.
        replay = self.store.write(values, actor='alice', idempotency_key='one')
        assert isinstance(replay, Preview)
        self.assertEqual(self.store.write(values, actor='alice', idempotency_key='one', confirm=replay.token), done)
        again = self.link('Ep1 v1', STAMP, 'two')
        self.assertFalse(again.result['created'])
        listed = self.store.list(self.project)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]['id'], STAMP)
        self.assertEqual(listed[0]['stamp'], STAMP)
        self.assertEqual(listed[0]['canonical'], {'resolve_project': RESOLVE_REF, 'resolve_project_media': self.resolve_media, 'name': 'Ep1 v1', 'active': True})
        self.assertEqual([a.action for a in self.registry.activity(self.project)][0], 'timeline_link')

    def test_renaming_the_resolve_timeline_does_not_break_the_link(self) -> None:
        self.link('Ep1 v1', STAMP, 'one')
        self.resolve.rename(RESOLVE_REF, 'u1', 'Ep1 locked')
        self.assertEqual([t['id'] for t in self.store.list(self.project)], [STAMP])
        # The stamp still finds the same Timeline under the new name; nothing new is recorded.
        self.assertFalse(self.link('Ep1 locked', STAMP, 'two').result['created'])
        self.assertEqual(len(self.store.list(self.project)), 1)
        # The old name is only a label that no longer names a timeline in Resolve.
        refused = self.store.write({'project': self.project, 'timeline': 'Ep1 v1', 'stamp': STAMP}, actor='alice', idempotency_key='three')
        self.assertIsInstance(refused, Refused)
        assert isinstance(refused, Refused)
        self.assertEqual(refused.rule, 'timeline')

    def test_a_timeline_can_be_named_by_its_resolve_id(self) -> None:
        self.assertEqual(self.link('u1', STAMP, 'one').result['timeline_id'], STAMP)

    def test_refusals_leave_nothing_recorded(self) -> None:
        def rule(values: dict[str, Any], store: TimelineStore | None = None) -> str:
            outcome = (store or self.store).write(values, actor='alice', idempotency_key=None)
            assert isinstance(outcome, Refused), outcome
            return outcome.rule
        base = {'project': self.project, 'timeline': 'Ep1 v1', 'stamp': STAMP}
        self.assertEqual(rule({**base, 'stamp': 'not-a-stamp'}), 'stamp')
        self.assertEqual(rule({**base, 'stamp': 't6m_' + 'a' * 32}), 'stamp')
        self.assertEqual(rule({**base, 'project': 'nope'}), 'project')
        self.assertEqual(rule({**base, 'project': 't6p_' + '0' * 24}), 'project')
        self.assertEqual(rule({**base, 'stamp': OTHER_STAMP}), 'stamp')  # the marker in Resolve says another stamp
        self.resolve.add(RESOLVE_REF, ResolveTimeline(uuid='u2', name='Unstamped', stamp=None))
        self.assertEqual(rule({**base, 'timeline': 'Unstamped'}), 'stamp')  # written by hand first
        self.assertEqual(rule({**base, 'timeline': 'Missing'}), 'timeline')
        other = self.register('q')  # a Project with no Resolve project
        self.assertEqual(rule({**base, 'project': other}), 'resolve_project')
        self.assertEqual(self.store.list(self.project), [])

    def test_a_duplicated_timeline_with_the_same_stamp_is_refused(self) -> None:
        self.resolve.add(RESOLVE_REF, ResolveTimeline(uuid='u9', name='Ep1 v1 copy', stamp=STAMP))
        refused = self.store.write({'project': self.project, 'timeline': 'Ep1 v1', 'stamp': STAMP}, actor='alice', idempotency_key='dup')
        assert isinstance(refused, Refused)
        self.assertEqual(refused.rule, 'timeline_stamp')
        self.assertEqual(self.store.list(self.project), [])

    def test_one_timeline_per_stamp_across_projects(self) -> None:
        self.link('Ep1 v1', STAMP, 'one')
        other = self.register('q')
        self.link_resolve(other, 'db/folder/other')
        self.resolve.add('db/folder/other', ResolveTimeline(uuid='x1', name='Copy', stamp=STAMP))
        refused = self.store.write({'project': other, 'timeline': 'Copy', 'stamp': STAMP}, actor='alice', idempotency_key='two')
        assert isinstance(refused, Refused)
        self.assertEqual(refused.rule, 'timeline_stamp')
        self.assertEqual(self.store.list(other), [])
        self.assertEqual(len(self.store.list(self.project)), 1)

    def test_confirming_after_another_link_records_nothing_new(self) -> None:
        values = {'project': self.project, 'timeline': 'Ep1 v1', 'stamp': STAMP}
        preview = self.store.write(values, actor='alice', idempotency_key='first')
        assert isinstance(preview, Preview)
        self.link('Ep1 v1', STAMP, 'second')
        # Same Project and stamp: the confirm records nothing new.
        done = self.store.write(values, actor='alice', idempotency_key='first', confirm=preview.token)
        assert isinstance(done, Done)
        self.assertFalse(done.result['created'])
        self.assertEqual(len(self.store.list(self.project)), 1)

    def test_an_ambiguous_name_asks_the_person(self) -> None:
        self.resolve.add(RESOLVE_REF, ResolveTimeline(uuid='u2', name='Ep1 v1', stamp=OTHER_STAMP))
        outcome = self.store.write({'project': self.project, 'timeline': 'Ep1 v1', 'stamp': STAMP}, actor='alice', idempotency_key='amb')
        assert isinstance(outcome, Ambiguous)
        self.assertEqual({c['uuid'] for c in outcome.candidates}, {'u1', 'u2'})

    def test_without_a_resolve_reader_the_stamp_is_recorded_as_unchecked(self) -> None:
        store = TimelineStore(self.registry, self.tokens)
        preview = store.write({'project': self.project, 'timeline': 'Anything', 'stamp': STAMP}, actor='alice', idempotency_key='x')
        assert isinstance(preview, Preview)
        self.assertFalse(preview.plan['verified'])
        self.link('Anything', STAMP, 'y', store=store)
        self.assertEqual(self.store.list(self.project)[0]['canonical']['name'], 'Anything')

    def test_link_without_a_stamp_creates_one_and_shows_it_in_the_preview(self) -> None:
        self.resolve.add(RESOLVE_REF, ResolveTimeline(uuid='u2', name='Unstamped', stamp=None))
        values = {'project': self.project, 'timeline': 'Unstamped'}
        preview = self.store.write(values, actor='alice', idempotency_key='gen')
        assert isinstance(preview, Preview), preview
        stamp = preview.plan['stamp']
        T6Id.parse(stamp, EntityKind.TIMELINE)
        self.assertTrue(preview.plan['stamp_generated'])
        # The same key shows the same stamp again, so the one written by hand in Resolve stays right.
        again = self.store.write(values, actor='alice', idempotency_key='gen')
        assert isinstance(again, Preview)
        self.assertEqual(again.plan['stamp'], stamp)
        done = self.store.write(values, actor='alice', idempotency_key='gen', confirm=preview.token)
        assert isinstance(done, Done), done
        self.assertEqual(done.result['timeline_id'], stamp)
        self.assertEqual([t['id'] for t in self.store.list(self.project)], [stamp])
        # Idempotent by key: confirming the other preview of the key, or replaying it, records nothing new.
        replay = self.store.write(values, actor='alice', idempotency_key='gen', confirm=again.token)
        self.assertEqual(replay, done)
        self.assertEqual(len(self.store.list(self.project)), 1)

    def test_the_timeline_stamped_by_hand_with_the_previewed_stamp_is_accepted_at_confirm_and_a_retry_returns_the_first_result(self) -> None:
        self.resolve.add(RESOLVE_REF, ResolveTimeline(uuid='u2', name='Unstamped', stamp=None))
        values = {'project': self.project, 'timeline': 'Unstamped'}
        preview = self.store.write(values, actor='alice', idempotency_key='hand')
        assert isinstance(preview, Preview), preview
        stamp = preview.plan['stamp']
        self.store.reader = FakeTimelineReader({RESOLVE_REF: [ResolveTimeline(uuid='u2', name='Unstamped', stamp=stamp)]})  # the person wrote the stamp by hand
        again = self.store.write(values, actor='alice', idempotency_key='hand')
        assert isinstance(again, Preview), again  # asking again with the stamp in place is the same preview
        done = self.store.write(values, actor='alice', idempotency_key='hand', confirm=preview.token)
        assert isinstance(done, Done), done
        self.assertEqual((done.result['timeline_id'], done.result['verified']), (stamp, True))
        self.assertEqual(self.store.write(values, actor='alice', idempotency_key='hand', confirm=preview.token), done)
        self.assertEqual(len(self.store.list(self.project)), 1)

    def test_a_generated_stamp_is_still_refused_when_the_timeline_carries_another_stamp(self) -> None:
        values = {'project': self.project, 'timeline': 'Ep1 v1'}
        preview = self.store.write(values, actor='alice', idempotency_key='other')
        assert isinstance(preview, Refused)
        self.assertEqual(preview.rule, 'stamp')

    def test_a_timeline_that_already_has_a_stamp_is_not_given_a_new_one(self) -> None:
        refused = self.store.write({'project': self.project, 'timeline': 'Ep1 v1'}, actor='alice', idempotency_key='has')
        assert isinstance(refused, Refused)
        self.assertEqual(refused.rule, 'stamp')

    def test_list_refuses_an_unknown_project(self) -> None:
        with self.assertRaises(LookupError):
            self.store.list('t6p_' + 'f' * 24)

    def test_a_version_may_name_its_source_timeline(self) -> None:
        deliverables = DeliverableStore(self.registry, self.tokens)
        rows = [{'page_id': 'd', 'project_page': 'p', 'code': 'FTR'}]
        deliverable = deliverables.migrate(rows, actor='setup', apply=True, digest=deliverables.migrate(rows, actor='setup')['digest'])['entries'][0]['t6_id']
        versions = VersionStore(self.registry, self.tokens, Reader())

        def add(file: str, timeline: str | None, key: str) -> Any:
            values: dict[str, Any] = {'deliverable': deliverable, 'frameio_file': file, 'note': None}
            if timeline is not None:
                values['timeline'] = timeline
            preview = versions.write('add', values, actor='alice', idempotency_key=key)
            if not isinstance(preview, Preview):
                return preview
            return versions.write('add', values, actor='alice', idempotency_key=key, confirm=preview.token)

        self.assertEqual(add('plain', None, 'k0').__class__, Done)
        plain = versions.for_file('plain')
        assert plain is not None
        self.assertIsNone(plain['source_timeline_id'])
        unknown = add('f1', STAMP, 'k1')
        assert isinstance(unknown, Refused)
        self.assertEqual(unknown.rule, 'timeline')
        self.link('Ep1 v1', STAMP, 'link')
        done = add('f2', STAMP, 'k2')
        assert isinstance(done, Done)
        self.assertEqual(done.result['source_timeline_id'], STAMP)
        record = versions.get(done.result['version_id'])
        assert record is not None
        self.assertEqual(record['source_timeline_id'], STAMP)
        # A Timeline of another Project cannot be the source.
        other = self.register('q')
        self.link_resolve(other, 'db/folder/other')
        self.resolve.add('db/folder/other', ResolveTimeline(uuid='x1', name='Other', stamp=OTHER_STAMP))
        self.link('Other', OTHER_STAMP, 'link-other', project=other)
        foreign = add('f3', OTHER_STAMP, 'k3')
        assert isinstance(foreign, Refused)
        self.assertEqual(foreign.rule, 'timeline')

    def test_verbs_are_in_the_catalogue_with_staff_write_and_reader_read(self) -> None:
        entries = {(v['noun'], v['verb']): v for v in catalogue()}
        link, listing = entries[('timeline', 'link')], entries[('timeline', 'list')]
        self.assertEqual(link['role'], 'staff')
        self.assertFalse(link['read_only'])
        self.assertEqual(set(link['input_schema']['properties']), {'project', 'timeline', 'stamp', 'idempotency_key', 'confirm'})
        self.assertNotIn('stamp', link['input_schema'].get('required', []))
        self.assertEqual((listing['role'], listing['read_only']), ('reader', True))
        restamp = entries[('timeline', 'restamp')]
        self.assertEqual((restamp['role'], restamp['read_only']), ('staff', False))
        self.assertEqual(set(restamp['input_schema']['required']), {'project', 'timeline', 'stamp'})
        self.assertEqual(listing['output_schema']['$defs']['TimelineEntry']['properties']['stamp_state']['enum'], ['pending', 'stamped', 'failed'])
        self.assertIn('timeline', entries[('version', 'add')]['input_schema']['properties'])


if __name__ == '__main__':
    unittest.main()
