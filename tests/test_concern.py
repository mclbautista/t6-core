import sqlite3
import unittest
from typing import Any

from t6.concern_scan import ConcernScanner
from t6.concern import ConcernStore, WEEK
from t6.ids import EntityKind, T6Id
from t6.preview import PreviewTokens
from t6.registry import Registered
from t6.verbs import Done, Preview, Refused, catalogue
from test_person import MemoryRegistry


class ConcernTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = MemoryRegistry()
        self.addCleanup(self.registry.db.close)
        self.now = 1000000.0
        self.registry._clock = lambda: self.now
        self.registry._new_id = lambda: str(T6Id.mint(EntityKind.PROJECT))
        project = self.registry.register(actor='setup', idempotency_key='setup', notion_page_id='project')
        assert isinstance(project, Registered)
        self.project = project.project.id
        with self.registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES ('coordinator', 'staff', '[]', '[]', NULL, 0, 1)")
        self.store = ConcernStore(self.registry, PreviewTokens('secret'))

    def write(self, verb: str, values: dict[str, Any], key: str) -> Any:
        preview = self.store.write(verb, values, actor='person', idempotency_key=key)
        assert isinstance(preview, Preview)
        done = self.store.write(verb, values, actor='person', idempotency_key=key, confirm=preview.token)
        assert isinstance(done, Done)
        retry = self.store.write(verb, values, actor='person', idempotency_key=key)
        assert isinstance(retry, Preview)
        self.assertEqual(done, self.store.write(verb, values, actor='person', idempotency_key=key, confirm=retry.token))
        return done.result

    def raise_concern(self) -> str:
        return str(self.write('raise', {'project': self.project, 'owner': 'coordinator', 'waiting_on': 'client', 'anchor': 'Waiting kay client — raw line', 'as_of': '2026-11-01'}, 'raise')['concern_id'])

    def _get(self, concern: str) -> dict[str, Any]:
        record = self.store.get(concern)
        assert record is not None
        return record

    def test_lifecycle_never_goes_quiet_or_expires_and_append_only(self) -> None:
        concern = self.raise_concern()
        self.now += 10 * WEEK
        self.assertEqual(self._get(concern)['state'], 'open')
        self.write('note', {'concern': concern, 'text': 'May update'}, 'note')
        self.write('resolve', {'concern': concern}, 'resolve')
        self.write('reopen', {'concern': concern}, 'reopen')
        record = self._get(concern)
        self.assertEqual(record['age_seconds'], 10 * WEEK)
        self.assertEqual(record['anchor'], 'Waiting kay client — raw line')
        self.assertEqual(record['state'], 'open')
        actions = [a.action for a in self.registry.activity(self.project) if a.subject_id == concern]
        for action in ('raise', 'note', 'resolve', 'reopen'):
            self.assertEqual(actions.count('concern_' + action), 1)
        with self.registry._transaction() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM audit WHERE project_id = ?', (concern,)).fetchone()[0], len(actions))
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE concern_notes SET text = 'changed'")
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute('DELETE FROM concern_notes')
            self.assertEqual(db.execute("SELECT COUNT(*) FROM internal_jobs WHERE kind = 'concern_silence'").fetchone()[0], 0)

    def test_manual_resolve_blocks_stale_preview(self) -> None:
        concern = self.raise_concern()
        stale = self.store.write('resolve', {'concern': concern}, actor='person', idempotency_key='stale')
        assert isinstance(stale, Preview)
        self.write('note', {'concern': concern, 'text': 'Still waiting'}, 'fresh-note')
        refused = self.store.write('resolve', {'concern': concern}, actor='person', idempotency_key='stale', confirm=stale.token)
        self.assertIsInstance(refused, Refused)
        self.write('resolve', {'concern': concern}, 'closed')
        self.assertEqual(self._get(concern)['state'], 'closed')

    def snooze(self, concern: str, key: str = 'snooze', actor: str = 'coordinator') -> Any:
        preview = self.store.write('snooze', {'concern': concern}, actor=actor, idempotency_key=key)
        if not isinstance(preview, Preview):
            return preview
        return self.store.write('snooze', {'concern': concern}, actor=actor, idempotency_key=key, confirm=preview.token)

    def test_a_human_snoozes_and_reopen_wakes_it(self) -> None:
        concern = self.raise_concern()
        done = self.snooze(concern)
        self.assertIsInstance(done, Done)
        record = self._get(concern)
        self.assertEqual(record['state'], 'snoozed')
        self.assertEqual([c['id'] for c in self.store.open_concerns()], [concern])
        self.assertIsInstance(self.snooze(concern, 'again'), Refused)
        self.write('reopen', {'concern': concern}, 'wake')
        self.assertEqual(self._get(concern)['state'], 'open')
        actions = [a.action for a in self.registry.activity(self.project) if a.subject_id == concern]
        self.assertEqual(actions.count('concern_snooze'), 1)

    def test_snooze_is_human_only(self) -> None:
        from t6.person import AUTO_ACTOR, SERVICES, INTERNAL_ACTORS
        concern = self.raise_concern()
        for actor in (AUTO_ACTOR, *sorted(SERVICES), *sorted(INTERNAL_ACTORS)):
            refused = self.snooze(concern, 'k-' + actor, actor)
            self.assertIsInstance(refused, Refused, actor)
            self.assertEqual(refused.rule, 'human_only')
        self.assertEqual(self._get(concern)['state'], 'open')

    def test_a_closed_or_missing_concern_cannot_be_snoozed(self) -> None:
        concern = self.raise_concern()
        self.write('resolve', {'concern': concern}, 'resolve')
        self.assertIsInstance(self.snooze(concern), Refused)
        self.assertIsInstance(self.snooze('t6c_missing', 'missing'), Refused)

    def test_a_note_keeps_a_snoozed_concern_snoozed(self) -> None:
        concern = self.raise_concern()
        self.snooze(concern)
        self.write('note', {'concern': concern, 'text': 'Update'}, 'note')
        self.assertEqual(self._get(concern)['state'], 'snoozed')

    def test_jo_done_closes_linked_concern_and_manual_reopen_sticks(self) -> None:
        from t6.job_order import JobOrderStore
        jo = str(T6Id.mint(EntityKind.JOB_ORDER))
        with self.registry._transaction() as db:
            db.execute('INSERT INTO job_orders VALUES (?, ?, NULL, NULL, ?)', (jo, self.project, '{"status":"In progress"}'))
        concern = self.write('raise', {'project': self.project, 'owner': 'coordinator', 'waiting_on': 'editor', 'anchor': 'Waiting on editor', 'as_of': '2026-11-01', 'settles_id': jo}, 'linked')['concern_id']
        orders = JobOrderStore(self.registry, self.store.tokens)
        preview = orders.write('close', {'jo': jo}, actor='person', idempotency_key='close-jo')
        assert isinstance(preview, Preview)
        self.now += 100
        self.assertIsInstance(orders.write('close', {'jo': jo}, actor='person', idempotency_key='close-jo', confirm=preview.token), Done)
        record = self.store.get(concern)
        assert record is not None
        self.assertEqual((record['state'], record['age_seconds']), ('closed', 100))
        self.assertTrue(any(a.actor == 'Sixto (auto)' and a.subject_id == concern for a in self.registry.activity(self.project)))
        self.write('reopen', {'concern': concern}, 'reopen-linked')
        from t6.concern import settle_linked
        with self.registry._transaction() as db:
            settle_linked(db, jo, self.now)
        record = self.store.get(concern)
        assert record is not None
        self.assertEqual((record['state'], record['age_seconds']), ('open', 100))

    def test_approval_grant_closes_linked_concern(self) -> None:
        from t6.approval import Approvals
        approvals = Approvals(self.registry, self.store.tokens)
        person = str(T6Id.mint(EntityKind.PERSON))
        with self.registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES (?, 'staff', '[]', '[]', NULL, 0, 1)", (person,))
        request = {'gate': 'grade', 'target': self.project, 'tapper_rule': 'person:' + person}
        preview = approvals.write('request', request, actor=person, idempotency_key='approval-request')
        assert isinstance(preview, Preview)
        done = approvals.write('request', request, actor=person, idempotency_key='approval-request', confirm=preview.token)
        assert isinstance(done, Done)
        approval = done.result['approval_id']
        concern = self.write('raise', {'project': self.project, 'owner': 'coordinator', 'waiting_on': 'client', 'anchor': 'Waiting on client', 'as_of': '2026-11-01', 'settles_id': approval}, 'approval-concern')['concern_id']
        decision = {'approval': approval}
        preview = approvals.write('grant', decision, actor=person, idempotency_key='approval-grant')
        assert isinstance(preview, Preview)
        self.assertIsInstance(approvals.write('grant', decision, actor=person, idempotency_key='approval-grant', confirm=preview.token), Done)
        record = self.store.get(concern)
        assert record is not None
        self.assertEqual(record['state'], 'closed')

    def test_raise_at_psr_lists_every_open_unsnoozed_concern_oldest_evidence_first(self) -> None:
        old = self.raise_concern()
        self.now += 6 * WEEK
        recent = str(self.write('raise', {'project': self.project, 'owner': 'coordinator', 'waiting_on': 'lab', 'anchor': 'Waiting on lab', 'as_of': '2026-11-01'}, 'recent')['concern_id'])
        listed = self.store.raise_at_psr(self.project)
        self.assertEqual([(c['id'], c['owner_id'], c['waiting_on'], c['evidence_at']) for c in listed], [(old, 'coordinator', 'client', 1000000.0), (recent, 'coordinator', 'lab', self.now)])
        self.assertEqual([c['age_seconds'] for c in listed], [6 * WEEK, 0])
        self.assertEqual(self.store.raise_at_psr('other-project'), [])
        # Newer evidence moves a Concern after the other one, and never hides it.
        self.now += WEEK
        self.write('note', {'concern': old, 'text': 'May update'}, 'note')
        self.assertEqual([c['id'] for c in self.store.raise_at_psr()], [recent, old])

    def test_raise_at_psr_skips_snoozed_and_closed_and_lists_a_woken_one_again(self) -> None:
        snoozed, closed = self.raise_concern(), str(self.write('raise', {'project': self.project, 'owner': 'coordinator', 'waiting_on': 'lab', 'anchor': 'Waiting on lab', 'as_of': '2026-11-01'}, 'other')['concern_id'])
        self.snooze(snoozed)
        self.write('resolve', {'concern': closed}, 'resolve')
        self.assertEqual(self.store.raise_at_psr(), [])
        self.write('reopen', {'concern': snoozed}, 'wake')
        self.assertEqual([c['id'] for c in self.store.raise_at_psr()], [snoozed])

    def test_validation_and_catalogue(self) -> None:
        result = self.store.write('raise', {'project': self.project, 'owner': 'coordinator', 'waiting_on': 'lab', 'anchor': ' ', 'as_of': '2026-01-01'}, actor='person')
        self.assertIsInstance(result, Refused)
        elsewhere = self.store.write('raise', {'project': self.project, 'owner': 'coordinator', 'waiting_on': 'lab', 'anchor': 'Client has not sent the KDM', 'as_of': '2026-01-01'}, actor='person')
        assert isinstance(elsewhere, Refused)
        self.assertEqual(elsewhere.rule, 'waiting_on')
        for line, party in (('Client has not sent the KDM', 'CLIENT'), ('For approval ni direk', 'direk'), ('Wala pa yung files kay client', 'client')):
            self.assertIsInstance(self.store.write('raise', {'project': self.project, 'owner': 'coordinator', 'waiting_on': party, 'anchor': line, 'as_of': '2026-01-01'}, actor='person'), Preview)
        self.assertTrue({'raise', 'note', 'resolve', 'reopen', 'list'} <= {v['verb'] for v in catalogue() if v['noun'] == 'concern'})

    def test_date_sanity_follows_data_and_keeps_age(self) -> None:
        dates = {'playdate': '2026-12-10', 'submission_deadline': '2026-12-01', 'archival_deadline': '2026-11-20'}
        ids = self.store.reconcile_dates(self.project, 'coordinator', dates)
        self.assertEqual(len(ids), 2)
        first = ids['submission_before_playdate']
        self.assertEqual(self.store.get(first)['state'], 'open')  # type: ignore[index]
        self.now += 100
        dates['submission_deadline'] = '2026-12-15'
        self.store.reconcile_dates(self.project, 'coordinator', dates)
        self.assertEqual(self.store.get(first)['state'], 'closed')  # type: ignore[index]
        dates['submission_deadline'] = '2026-12-01'
        self.assertEqual(self.store.reconcile_dates(self.project, 'coordinator', dates)['submission_before_playdate'], first)
        record = self.store.get(first)
        assert record is not None
        self.assertEqual((record['state'], record['age_seconds']), ('open', 100))
        self.assertIsInstance(self.store.write('resolve', {'concern': first}, actor='person'), Refused)

    def test_filled_awaited_date_closes_concern_and_manual_reopen_sticks(self) -> None:
        from t6.notion_edits import NotionEdits
        raised = {'project': self.project, 'owner': 'coordinator', 'waiting_on': 'client', 'anchor': 'Client, waiting for the playdate', 'as_of': '2026-11-01', 'settles_date': 'playdate'}
        concern = self.write('raise', raised, 'awaited-date')['concern_id']
        edits = NotionEdits(self.registry)
        dates: dict[str, str | None] = {'playdate': None, 'submission_deadline': '2026-12-01', 'archival_deadline': None}
        edits.project_dates('project', dates, [])
        self.assertEqual(self.store.get(concern)['state'], 'open')  # type: ignore[index]
        self.now += 100
        dates['playdate'] = '2026-12-10'
        edits.project_dates('project', dates, [])
        record = self.store.get(concern)
        assert record is not None
        self.assertEqual((record['state'], record['age_seconds']), ('closed', 100))
        self.assertTrue(any(a.actor == 'Sixto (auto)' and a.subject_id == concern for a in self.registry.activity(self.project)))
        self.write('reopen', {'concern': concern}, 'reopen-awaited')
        edits.project_dates('project', dates, [])
        self.assertEqual(self.store.get(concern)['state'], 'open')  # type: ignore[index]
        both = {**raised, 'settles_id': 't6j_0123456789abcdef01234567'}
        self.assertIsInstance(self.store.write('raise', both, actor='person'), Refused)

    def test_notion_project_date_observation_reconciles_rules(self) -> None:
        from t6.notion_edits import NotionEdits
        with self.registry._transaction() as db:
            db.execute("INSERT INTO bindings (system, page_id, project_id, bound_at, active) VALUES ('notion', 'coordinator-page', 'coordinator', ?, 1)", (self.now,))
        edits = NotionEdits(self.registry)
        dates = {'playdate': '2026-12-10', 'submission_deadline': '2026-12-01', 'archival_deadline': None}
        result = edits.project_dates('project', dates, ['coordinator-page'])
        concern = result['concerns']['submission_before_playdate']
        self.assertEqual(self.store.get(concern)['state'], 'open')  # type: ignore[index]
        dates['submission_deadline'] = '2026-12-11'
        edits.project_dates('project', dates, ['coordinator-page'])
        self.assertEqual(self.store.get(concern)['state'], 'closed')  # type: ignore[index]

    def test_brief_reply_is_the_only_chat_text_kept(self) -> None:
        concern = self.raise_concern()
        self.write('record-message', {'concern': concern, 'chat': '-100', 'message_id': '42'}, 'brief')
        self.assertEqual(self.store.for_message('-100', '42'), [concern])
        self.assertEqual(self.store.for_message('-100', '43'), [])
        ignored = self.store.write('note', {'concern': concern, 'text': 'unrelated chat text', 'from_reply': '-100:43', 'message_id': '45'}, actor='replier', idempotency_key='ignored')
        self.assertEqual(ignored, Done(result={'ignored': True}))
        values = {'concern': concern, 'text': 'The client replied', 'from_reply': '-100:42', 'message_id': '44'}
        preview = self.store.write('note', values, actor='replier', idempotency_key='reply')
        assert isinstance(preview, Preview)
        result = self.store.write('note', values, actor='replier', idempotency_key='reply', confirm=preview.token)
        self.assertIsInstance(result, Done)
        record = self.store.get(concern)
        assert record is not None
        self.assertEqual([(n['text'], n['actor'], n['message_id']) for n in record['notes']], [('The client replied', 'replier', '44')])
        with self.registry._transaction() as db:
            dump = '\n'.join(db.iterdump())
            self.assertNotIn('unrelated chat text', dump)

    def test_record_message_keeps_the_date_of_the_brief_when_given(self) -> None:
        concern = self.raise_concern()
        self.write('record-message', {'concern': concern, 'chat': '-100', 'message_id': '1', 'brief_date': '2026-11-16'}, 'dated')
        self.write('record-message', {'concern': concern, 'chat': '-100', 'message_id': '2'}, 'undated')
        with self.registry._transaction() as db:
            self.assertEqual(db.execute('SELECT message_id, brief_date FROM concern_brief_messages ORDER BY message_id').fetchall(), [('1', '2026-11-16'), ('2', None)])
        refused = self.store.write('record-message', {'concern': concern, 'chat': '-100', 'message_id': '3', 'brief_date': 'Monday'}, actor='person', idempotency_key='bad-date')
        assert isinstance(refused, Refused)
        self.assertEqual(refused.rule, 'input')

    def test_brief_can_carry_multiple_concerns(self) -> None:
        first = self.raise_concern()
        second = self.write('raise', {'project': self.project, 'owner': 'coordinator', 'waiting_on': 'studio', 'anchor': 'Waiting on studio', 'as_of': '2026-11-01'}, 'second')['concern_id']
        for concern in (first, second):
            self.write('record-message', {'concern': concern, 'chat': 'chat', 'message_id': '1'}, 'mapping-' + concern)
        self.assertEqual(set(self.store.for_message('chat', '1')), {first, second})

    def raise_flag(self, line: str, **values: Any) -> str:
        key = 'flag-' + line + str(values)
        raised = {'project': self.project, 'anchor': line, 'as_of': '2026-11-16', 'source': 'flag_rule', **values}
        done = self.store.write('raise', raised, actor='reports', idempotency_key=key)
        assert isinstance(done, Preview)
        done = self.store.write('raise', raised, actor='reports', idempotency_key=key, confirm=done.token)
        assert isinstance(done, Done)
        return str(done.result['concern_id'])

    def test_a_concern_may_have_no_owner_and_no_waiting_on(self) -> None:
        concern = self.raise_flag('No editor is named')
        record = self._get(concern)
        self.assertEqual((record['owner_id'], record['waiting_on'], record['text'], record['source'], record['closed_at']), (None, '', 'No editor is named', 'flag_rule', None))
        with self.registry._transaction() as db:
            self.assertEqual(db.execute('SELECT text_key FROM concerns WHERE id = ?', (concern,)).fetchone()[0], 'no editor is named')
        other = self.raise_flag('Playdate is missing', text='  Playdate   IS missing ', owner='coordinator')
        self.assertEqual((self._get(other)['text'], self._get(other)['anchor'], self._get(other)['owner_id']), ('  Playdate   IS missing ', 'Playdate is missing', 'coordinator'))
        self.assertEqual(self._get(self.raise_concern())['source'], 'person')

    def test_person_raise_needs_an_owner_a_named_party_and_a_known_source(self) -> None:
        base = {'project': self.project, 'waiting_on': 'client', 'anchor': 'Waiting on client', 'as_of': '2026-11-01'}
        refused = self.store.write('raise', base, actor='person')
        assert isinstance(refused, Refused)
        self.assertEqual(refused.rule, 'input')
        refused = self.store.write('raise', {**base, 'owner': 'coordinator', 'waiting_on': 'lab'}, actor='person')
        assert isinstance(refused, Refused)
        self.assertEqual(refused.rule, 'waiting_on')
        refused = self.store.write('raise', {**base, 'owner': 'coordinator', 'source': 'chat'}, actor='person')
        assert isinstance(refused, Refused)
        self.assertEqual(refused.rule, 'source')
        refused = self.store.write('raise', {**base, 'owner': 'nobody'}, actor='person')
        assert isinstance(refused, Refused)
        self.assertEqual(refused.rule, 'owner')
        self.assertIsInstance(self.store.write('raise', {**base, 'owner': 'coordinator', 'waiting_on': None}, actor='person'), Preview)

    def test_a_concern_that_is_not_closed_is_unique_by_project_and_text(self) -> None:
        first = self.raise_flag('Missing role')
        refused = self.store.write('raise', {'project': self.project, 'anchor': 'x', 'text': 'MISSING   role', 'as_of': '2026-11-16', 'source': 'leave'}, actor='reports')
        assert isinstance(refused, Refused)
        self.assertEqual(refused.rule, 'duplicate')
        self.write('resolve', {'concern': first}, 'close-first')
        second = self.raise_flag('Missing role', as_of='2026-11-20')
        self.assertNotEqual(first, second)
        refused = self.store.write('reopen', {'concern': first}, actor='person')
        assert isinstance(refused, Refused)
        self.assertEqual(refused.rule, 'duplicate')

    def test_closed_at_is_written_by_every_path_that_closes_and_cleared_by_reopen(self) -> None:
        resolved = self.raise_concern()
        self.now += 10
        self.write('resolve', {'concern': resolved}, 'resolve-it')
        self.assertEqual(self._get(resolved)['closed_at'], self.now)
        self.now += 10
        self.write('reopen', {'concern': resolved}, 'reopen-it')
        self.assertIsNone(self._get(resolved)['closed_at'])
        ids = self.store.reconcile_dates(self.project, 'coordinator', {'playdate': '2026-12-10', 'submission_deadline': '2026-12-01', 'archival_deadline': None})
        self.now += 10
        self.store.reconcile_dates(self.project, 'coordinator', {'playdate': '2026-12-10', 'submission_deadline': '2026-12-11', 'archival_deadline': None})
        settled = self._get(ids['submission_before_playdate'])
        self.assertEqual((settled['state'], settled['closed_at'], settled['source']), ('closed', self.now, 'date_rule'))
        self.now += 10
        self.store.reconcile_dates(self.project, 'coordinator', {'playdate': '2026-12-10', 'submission_deadline': '2026-12-01', 'archival_deadline': None})
        self.assertIsNone(self._get(ids['submission_before_playdate'])['closed_at'])
        pending = self.raise_flag('Line to close')
        with self.registry._transaction() as db:
            db.execute('INSERT INTO concern_pm_pending VALUES (?, ?, ?, ?, ?)', (pending, '2026-11-17', 'quote', self.now, 1))
        self.now += 10
        ConcernScanner(self.store).apply_pending(self.project)
        self.assertEqual((self._get(pending)['state'], self._get(pending)['closed_at']), ('closed', self.now))

    def test_open_concerns_are_every_state_but_closed_oldest_first_with_the_chronic_mark(self) -> None:
        rows = (('t6c_a', 'open', 30.0), ('t6c_b', 'closed', 10.0), ('t6c_c', 'expired', 20.0), ('t6c_d', 'quiet', 40.0))
        self.now = 100 * 86400.0
        with self.registry._transaction() as db:
            for concern, state, days in rows:
                db.execute("INSERT INTO concerns (id, project_id, owner_id, waiting_on, anchor, as_of, opened_at, evidence_at, state) VALUES (?, ?, 'coordinator', 'client', ?, '2026-11-01', ?, 0, ?)", (concern, self.project, 'Waiting on client ' + concern, self.now - days * 86400, state))
        listed = self.store.open_concerns(self.project)
        self.assertEqual([(c['id'], c['state'], c['chronic']) for c in listed], [('t6c_d', 'quiet', True), ('t6c_a', 'open', True), ('t6c_c', 'expired', False)])
        self.assertEqual(listed[2]['text'], 'Waiting on client t6c_c')
        self.assertEqual(set(listed[0]) - {'owner_id', 'waiting_on'}, {'id', 'project_id', 'text', 'source', 'anchor', 'as_of', 'opened_at', 'evidence_at', 'state', 'settles_id', 'age_seconds', 'chronic'})
        self.assertEqual(self.store.open_concerns('elsewhere'), [])
        self.assertEqual(len(self.store.open_concerns()), 3)


class ConcernQueueTests(unittest.TestCase):
    def test_raising_and_noting_submit_no_timers(self) -> None:
        import tempfile
        from pathlib import Path
        from t6.registry.sqlite import SqliteProjectRegistry

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'db'
            registry = SqliteProjectRegistry(path, clock=lambda: 1000000.0)
            project = registry.register(actor='setup', idempotency_key='setup', notion_page_id='project')
            assert isinstance(project, Registered)
            with registry._transaction() as db:
                db.execute("INSERT INTO persons VALUES ('coordinator', 'staff', '[]', '[]', NULL, 0, 1)")
            store = ConcernStore(registry, PreviewTokens('secret'))
            values = {'project': project.project.id, 'owner': 'coordinator', 'waiting_on': 'client', 'anchor': 'waiting on client', 'as_of': '2026-01-01'}
            preview = store.write('raise', values, actor='person', idempotency_key='key')
            assert isinstance(preview, Preview)
            result = store.write('raise', values, actor='person', idempotency_key='key', confirm=preview.token)
            assert isinstance(result, Done)
            with registry._transaction() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM internal_jobs').fetchone()[0], 0)
