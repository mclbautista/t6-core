"""Drive custody and assignment verbs on the t6 database."""
import io
import json
import os
import unittest
from contextlib import redirect_stdout
from dataclasses import asdict
from datetime import date
from typing import Any
from unittest.mock import patch

from t6.drives import Assignment, Drive, DriveStatus, FormFactor, Ref, Role, SourceRow
from t6.drives.importer import ReviewItem
from t6.drives.sqlite import SqliteDriveStore
from t6.ids import EntityKind, T6Id
from t6.person import People
from t6.preview import PreviewTokens
from t6.registry import Registered
from t6.verbs import VERBS, Ambiguous, Done, Preview, Refused, catalogue, invoke
from test_person import MemoryRegistry

CONTACT = '12345678123412341234123456789abc'


class DriveStoreSeamTests(unittest.TestCase):
    def test_drive_verbs_call_the_store_only_through_the_drive_store_protocol(self) -> None:
        """The in-memory fake and the SQLite store are interchangeable behind DriveStore, so the verb flow may
        call only what the protocol names (preview bookkeeping lives in the verb flow, not on the store)."""
        import ast
        import inspect
        from t6 import drive_verbs
        from t6.drives import DriveStore
        protocol = {name for name in dir(DriveStore) if not name.startswith('_')}
        used = set()
        for node in ast.walk(ast.parse(inspect.getsource(drive_verbs))):
            if isinstance(node, ast.Attribute):
                owner = node.value
                if (isinstance(owner, ast.Name) and owner.id == 'store') or (isinstance(owner, ast.Attribute) and owner.attr == 'store'):
                    used.add(node.attr)
        self.assertEqual(sorted(used - protocol), [])

    def test_the_in_memory_fake_and_the_sqlite_store_have_the_same_public_surface(self) -> None:
        from t6.drives.testing import InMemoryDriveStore
        def public(cls: type) -> set[str]:
            return {name for name in dir(cls) if not name.startswith('_')}
        self.assertEqual(public(InMemoryDriveStore) ^ public(SqliteDriveStore), set())


class DriveVerbTests(unittest.TestCase):
    def setUp(self) -> None:
        env = patch.dict(os.environ, {'T6_PREVIEW_SIGNING_KEY': 'test'})
        env.start()
        self.addCleanup(env.stop)
        self.registry = MemoryRegistry()
        self.addCleanup(self.registry.db.close)
        self.people = People(self.registry, PreviewTokens('test'))
        with self.registry._transaction() as db:
            for person in ('keeper', 'reader'):
                db.execute("INSERT INTO persons VALUES (?, 'staff', '[]', '[]', NULL, 0, 1)", (person,))
            db.execute("INSERT INTO person_permissions VALUES ('keeper', 'drives')")
        self.registry._new_id = lambda: str(T6Id.mint(EntityKind.PROJECT))
        self.projects = []
        for key in ('one', 'two'):
            project = self.registry.register(actor='setup', idempotency_key=key)
            assert isinstance(project, Registered)
            self.projects.append(project.project.id)
        self.store = SqliteDriveStore(self.registry)
        self.drives = [self.store.add_drive(Drive(serial=f'SYNTH-{n}', label=f'Synthetic {n}')).id for n in range(3)]

    def call(self, noun: str, verb: str, values: dict[str, Any], actor: str = 'keeper') -> Any:
        entry = next(e for e in VERBS if (e.noun, e.verb) == (noun, verb))
        return invoke(entry, entry.input_model.model_validate(values), actor=actor, people=self.people, open_notion=lambda _: None)

    def write(self, verb: str, values: dict[str, Any], key: str | None = None) -> Any:
        key = key or f'{verb}-{sorted(values.items(), key=str)}'
        preview = self.call('drive', verb, {**values, 'idempotency_key': key})
        self.assertIsInstance(preview, Preview)
        result = self.call('drive', verb, {**values, 'idempotency_key': key, 'confirm': preview.token})
        self.assertIsInstance(result, Done)
        return result

    def read(self, verb: str, values: dict[str, Any] | None = None, actor: str = 'reader') -> Any:
        done = self.call('drive', verb, values or {}, actor=actor)
        self.assertIsInstance(done, Done, done)
        return done.result

    def seed_inventory(self) -> None:
        """Drive 0 is in custody, flagged and out for the first Project; 1 is released and out for the second; 2 is untouched."""
        a, b, _ = self.drives
        one, two = self.projects
        self.store.set_review(a, needs_review=True, note='check serial')
        self.store.set_suspected_serial(a, 'MAYBE-9')
        self.write('receive', {'drive': a, 'project': one, 'on': '2026-11-01'})
        self.write('assign', {'drive': a, 'project': one, 'label': 'A1', 'role': 'master', 'start': '2026-11-01'})
        self.write('release', {'drives': [b], 'project': two, 'party': 'Invented Studio', 'party_kind': 'studio', 'on': '2026-11-02'})
        self.write('assign', {'drive': b, 'project': two, 'label': 'B1', 'role': 'backup', 'start': '2026-11-01'})

    def test_drive_list_shows_each_drive_with_its_derived_status(self) -> None:
        self.seed_inventory()
        listed = self.read('list')['drives']
        self.assertEqual([(d['id'], d['status']) for d in listed], list(zip(self.drives, ('in custody', 'released', 'unknown'))))
        first = listed[0]
        self.assertEqual((first['label'], first['serial'], first['suspected_serial'], first['needs_review'], first['review_note']),
                         ('Synthetic 0', 'SYNTH-0', 'MAYBE-9', True, 'check serial'))

    def test_drive_list_carries_each_drives_open_assignments_with_their_numbers(self) -> None:
        self.seed_inventory()
        a, b, c = self.drives
        one = self.projects[0]
        self.store.import_row('row-1', Drive(serial='SYNTH-T'), Assignment(label='T', project=Ref(text='Invented Show'), role=Role.SWING, start=date(2026, 1, 1), holder=None), [])
        self.write('assign', {'drive': a, 'project': one, 'label': 'A2', 'role': 'backup', 'start': '2026-11-03'})
        self.write('unassign', {'drive': a, 'project': one, 'label': 'A1', 'end': '2026-11-04'})
        listed = {d['id']: d['open_assignments'] for d in self.read('list')['drives']}
        text_only = self.store.find_by_serial('SYNTH-T')
        assert text_only is not None
        self.assertEqual([(x['number'], x['label'], x['project']['page_id'], x['role'], x['start'], x['holder']) for x in listed[a]], [(2, 'A2', one, 'backup', '2026-11-03', None)])
        self.assertEqual([(x['number'], x['label'], x['project']['page_id'], x['role']) for x in listed[b]], [(1, 'B1', self.projects[1], 'backup')])
        self.assertEqual(listed[c], [])
        self.assertEqual([(x['label'], x['project']['page_id'], x['project']['text'], x['role']) for x in listed[text_only.id]], [('T', None, 'Invented Show', 'swing')])

    def test_drive_list_filters_are_the_inventory_screen_filters_and_combine_with_and(self) -> None:
        self.seed_inventory()
        a, b, c = self.drives
        one, two = self.projects

        def ids(**values: Any) -> list[str]:
            return [d['id'] for d in self.read('list', values)['drives']]
        self.assertEqual(ids(q='maybe'), [a])  # suspected serial
        self.assertEqual(ids(q='SYNTHETIC 1'), [b])  # label, any case
        self.assertEqual(ids(q=b), [b])  # Drive ID
        self.assertEqual(ids(q='synthetic'), [a, b, c])
        self.assertEqual(ids(q='check serial'), [])  # the review note is not searched
        self.assertEqual(ids(serial=' synth-1 '), [b])
        self.assertEqual(ids(serial='MAYBE-9'), [])  # exact serial is accepted serials only
        self.assertEqual(ids(needs_review=True), [a])
        self.assertEqual(ids(needs_review=False), [b, c])
        self.assertEqual(ids(project=one), [a])
        self.assertEqual(ids(project=two), [b])
        self.assertEqual(ids(status='unknown'), [c])
        self.assertEqual(ids(status='released'), [b])
        self.assertEqual(ids(needs_review=False, status='released'), [b])
        self.assertEqual(ids(q='synthetic', project=two, status='in custody'), [])
        self.write('unassign', {'drive': a, 'project': one, 'end': '2026-11-10'})
        self.assertEqual(ids(project=one), [])  # an ended Assignment is not open use

    def test_drive_get_shows_the_facts_status_events_numbered_assignments_sources_and_deliveries(self) -> None:
        drive, one, two = self.drives[0], *self.projects
        self.write('receive', {'drive': drive, 'project': one, 'party': CONTACT, 'party_kind': 'contact', 'on': '2026-11-01'})
        self.write('release', {'drives': [drive], 'project': one, 'party': 'Invented Studio', 'party_kind': 'studio', 'on': '2026-11-02'})
        self.write('assign', {'drive': drive, 'project': one, 'label': 'A1', 'role': 'master', 'start': '2026-11-01'})
        self.write('assign', {'drive': drive, 'project': two, 'label': 'A2', 'role': 'backup', 'start': '2026-11-03'})
        self.write('unassign', {'drive': drive, 'project': one, 'end': '2026-11-10'})
        self.store.import_row('row-1', Drive(serial='SYNTH-0'), None, [], source=SourceRow('book.xlsx', 'Sheet1', 7))
        self.store.link_delivery('t6g_1', [drive])
        got = self.read('get', {'drive': drive})
        self.assertEqual((got['drive']['id'], got['drive']['serial'], got['drive']['status']), (drive, 'SYNTH-0', 'released'))
        self.assertEqual([e['kind'] for e in got['events']], ['received', 'released'])
        self.assertEqual(got['events'][0]['party'], {'kind': 'contact', 'ref': {'page_id': CONTACT, 'text': None}})
        self.assertEqual(got['events'][1]['party']['ref'], {'page_id': None, 'text': 'Invented Studio'})
        self.assertEqual((got['events'][0]['on'], got['events'][0]['project']), ('2026-11-01', {'page_id': one, 'text': None}))
        self.assertEqual([(a['number'], a['label'], a['role'], a['end'], a['open']) for a in got['assignments']],
                         [(1, 'A1', 'master', '2026-11-10', False), (2, 'A2', 'backup', None, True)])
        self.assertEqual(got['assignments'][1]['project'], {'page_id': two, 'text': None})
        self.assertEqual(got['sources'], [{'workbook': 'book.xlsx', 'sheet': 'Sheet1', 'row': 7}])
        self.assertEqual(got['deliveries'], ['t6g_1'])
        missing = self.call('drive', 'get', {'drive': 't6d_' + '0' * 32}, actor='reader')
        self.assertIsInstance(missing, Refused)
        self.assertEqual(missing.rule, 'drive')

    def test_drive_find_matches_an_accepted_or_a_suspected_serial_in_any_spelling(self) -> None:
        a, b, c = self.drives
        self.store.set_suspected_serial(b, 'SYNTH-0')
        self.store.set_suspected_serial(c, 'maybe-1 (old)')
        found = self.read('find', {'serial': ' synth-0 '})
        self.assertEqual([(m['id'], m['match']) for m in found['matches']], [(a, 'accepted'), (b, 'suspected')])
        self.assertEqual([(m['id'], m['match']) for m in self.read('find', {'serial': 'MAYBE-1'})['matches']], [(c, 'suspected')])
        self.assertEqual(self.read('find', {'serial': 'nothing'})['matches'], [])

    def test_drive_shipments_lists_each_shipment_and_drive_shipment_shows_its_drives_and_release_events(self) -> None:
        one, two = self.projects
        first = self.write('release', {'drives': self.drives[:2], 'project': one, 'party': 'Invented Studio', 'party_kind': 'studio', 'on': '2026-11-02'}).result['shipment_id']
        second = self.write('release', {'drives': self.drives[2:], 'project': two, 'party': CONTACT, 'party_kind': 'contact', 'on': '2026-11-05'}).result['shipment_id']
        listed = self.read('shipments')['shipments']
        self.assertEqual([(s['shipment_id'], s['on'], s['drive_count']) for s in listed], [(first, '2026-11-02', 2), (second, '2026-11-05', 1)])
        self.assertEqual(listed[0]['party'], {'kind': 'studio', 'ref': {'page_id': None, 'text': 'Invented Studio'}})
        self.assertEqual(listed[0]['project'], {'page_id': one, 'text': None})
        got = self.read('shipment', {'shipment': first})
        self.assertEqual(got['shipment_id'], first)
        self.assertEqual([r['drive']['id'] for r in got['releases']], self.drives[:2])
        self.assertEqual([(r['event']['kind'], r['event']['on'], r['drive']['status']) for r in got['releases']], [('released', '2026-11-02', 'released')] * 2)
        unknown = self.call('drive', 'shipment', {'shipment': 'shipment-none'}, actor='reader')
        self.assertIsInstance(unknown, Refused)
        self.assertEqual(unknown.rule, 'shipment')

    def test_drive_exceptions_lists_the_import_review_queue(self) -> None:
        self.assertEqual(self.read('exceptions'), {'exceptions': []})
        self.store.record_exceptions([ReviewItem(sheet='Sheet1', row=3, reason='unreadable', detail='(unreadable)', workbook='book.xlsx')])
        self.assertEqual(self.read('exceptions')['exceptions'],
                         [{'workbook': 'book.xlsx', 'sheet': 'Sheet1', 'row': 3, 'reason': 'unreadable', 'detail': '(unreadable)'}])

    def test_the_reads_are_open_reader_verbs_and_the_fact_writes_need_the_drives_permission(self) -> None:
        for verb in ('list', 'get', 'find', 'shipments', 'shipment', 'exceptions'):
            entry = next(e for e in VERBS if (e.noun, e.verb) == ('drive', verb))
            self.assertEqual((entry.role, entry.read_only), ('reader', True), verb)
        for verb in ('add', 'edit', 'promote-serial'):
            entry = next(e for e in VERBS if (e.noun, e.verb) == ('drive', verb))
            self.assertEqual((entry.role, entry.read_only), ('drives', False), verb)
        for verb, values in (('add', {'label': 'X'}), ('edit', {'drive': self.drives[0], 'label': 'X'}), ('promote-serial', {'drive': self.drives[0]})):
            refused = self.call('drive', verb, values, actor='reader')
            self.assertIsInstance(refused, Refused)
            self.assertIn('drives', refused.reason)
        self.assertEqual(len(self.store.list_drives()), 3)

    def test_the_drive_reads_call_the_store_only_through_the_drive_store_protocol(self) -> None:
        import ast
        import inspect
        import re
        from t6 import drive_reads
        from t6.drives import DriveStore
        protocol = {name for name in dir(DriveStore) if not name.startswith('_')}
        tree = ast.parse(inspect.getsource(drive_reads))
        used = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == 'store'}
        self.assertEqual(sorted(used - protocol), [])
        self.assertFalse([n for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str) and re.match(r'\s*(SELECT|INSERT|UPDATE|DELETE)', n.value)])

    def test_custody_sequence_derives_status(self) -> None:
        drive, project = self.drives[0], self.projects[0]
        self.write('receive', {'drive': drive, 'project': project, 'party': CONTACT, 'party_kind': 'contact', 'on': '2026-11-01'})
        self.assertIs(self.store.status(drive), DriveStatus.IN_CUSTODY)
        released = self.write('release', {'drives': [drive], 'project': project, 'party': 'Invented Studio', 'party_kind': 'studio', 'on': '2026-11-02'})
        self.assertIs(self.store.status(drive), DriveStatus.RELEASED)
        self.assertTrue(released.result['shipment_id'])
        self.write('return', {'drive': drive, 'project': project, 'on': '2026-11-03'})
        self.assertIs(self.store.status(drive), DriveStatus.IN_CUSTODY)
        self.write('retire', {'drive': drive, 'project': project, 'reason': 'dead', 'on': '2026-11-04'})
        self.assertIs(self.store.status(drive), DriveStatus.RETIRED)
        events = self.store.list_events(drive)
        self.assertEqual([e.kind.value for e in events], ['received', 'released', 'returned', 'retired'])
        self.assertEqual(events[1].party.ref.text, 'Invented Studio')  # type: ignore[union-attr]
        self.assertEqual(events[0].party.ref.page_id, CONTACT)  # type: ignore[union-attr]
        self.assertEqual(events[3].reason, 'dead')

    def custody(self, drive: str, kind: str, project: str | None = None) -> None:
        """Put the Drive's custody log at the end of a path that reaches `kind`: none, received, released, returned or retired."""
        project = project or self.projects[0]
        steps = {'none': [], 'received': ['receive'], 'released': ['release'], 'returned': ['release', 'return'], 'retired': ['retire']}[kind]
        for verb in steps:
            values: dict[str, Any] = {'drives': [drive]} if verb == 'release' else {'drive': drive}
            self.write(verb, {**values, 'project': project, 'party': 'Invented Studio', 'party_kind': 'studio', 'reason': 'dead'})

    def test_custody_verbs_refuse_a_step_that_cannot_follow_the_status_and_write_nothing(self) -> None:
        project = self.projects[0]
        cases = [('receive', 'received', 'in custody'), ('receive', 'retired', 'retired'),
                 ('release', 'released', 'released'), ('release', 'retired', 'retired'),
                 ('return', 'none', 'no custody events'), ('return', 'received', 'in custody'), ('return', 'retired', 'retired'),
                 ('retire', 'retired', 'retired')]
        for verb, before, said in cases:
            with self.subTest(verb=verb, before=before):
                drive = self.store.add_drive(Drive(serial=f'CASE-{verb}-{before}', label=f'Case {verb} {before}')).id
                self.custody(drive, before)
                events = self.store.list_events(drive)
                values: dict[str, Any] = {'drives': [drive]} if verb == 'release' else {'drive': drive}
                refused = self.call('drive', verb, {**values, 'project': project, 'party': 'Invented Studio', 'party_kind': 'studio', 'reason': 'dead', 'idempotency_key': 'k'})
                self.assertIsInstance(refused, Refused)
                self.assertEqual(refused.rule, 'custody')
                self.assertIn(drive, refused.reason)
                self.assertIn(said, refused.reason)
                self.assertEqual(self.store.list_events(drive), events)

    def test_custody_verbs_allow_the_steps_that_can_follow(self) -> None:
        for verb, before in [('receive', 'none'), ('receive', 'released'), ('release', 'none'), ('release', 'received'),
                             ('release', 'returned'), ('return', 'released'), ('retire', 'none'), ('retire', 'received'), ('retire', 'released')]:
            with self.subTest(verb=verb, before=before):
                drive = self.store.add_drive(Drive(serial=f'OK-{verb}-{before}', label=f'Ok {verb} {before}')).id
                self.custody(drive, before)
                count = len(self.store.list_events(drive))
                values: dict[str, Any] = {'drives': [drive]} if verb == 'release' else {'drive': drive}
                self.write(verb, {**values, 'project': self.projects[0], 'party': 'Invented Studio', 'party_kind': 'studio', 'reason': 'dead'}, key=f'{verb}-{before}')
                self.assertEqual(len(self.store.list_events(drive)), count + 1)

    def test_a_release_with_one_drive_that_cannot_be_released_is_refused_whole_naming_each_such_drive(self) -> None:
        free, held, gone = self.drives
        self.custody(held, 'released')
        self.custody(gone, 'retired')
        refused = self.call('drive', 'release', {'drives': [free, held, gone], 'project': self.projects[0], 'party': 'Invented Studio', 'party_kind': 'studio', 'idempotency_key': 'k'})
        self.assertIsInstance(refused, Refused)
        self.assertEqual(refused.rule, 'custody')
        self.assertIn(held, refused.reason)
        self.assertIn(gone, refused.reason)
        self.assertNotIn(free, refused.reason)
        self.assertEqual(self.store.list_events(free), ())

    def test_the_custody_check_runs_again_at_confirm(self) -> None:
        drive, project = self.drives[0], self.projects[0]
        values = {'drive': drive, 'project': project, 'idempotency_key': 'late'}
        preview = self.call('drive', 'receive', values)
        self.assertIsInstance(preview, Preview)
        self.custody(drive, 'received')
        refused = self.call('drive', 'receive', {**values, 'confirm': preview.token})
        self.assertIsInstance(refused, Refused)
        self.assertEqual(refused.rule, 'custody')
        self.assertEqual(len(self.store.list_events(drive)), 1)

    def test_release_groups_a_shipment_and_links_a_free_text_delivery(self) -> None:
        project = self.projects[0]
        with self.registry._transaction() as db:
            db.execute("INSERT INTO deliverables VALUES ('t6v_1', ?, 'FTR', NULL, NULL, NULL)", (project,))
            db.execute("INSERT INTO packages VALUES ('t6g_1', 't6v_1', 'delivery', 'keeper', 'client', 'client', NULL, '{}', '[]', 'handed_off', NULL, 1)")
            db.execute("INSERT INTO delivery_carriers VALUES ('t6g_1', 'drive', 'free text drive')")
            db.execute("INSERT INTO packages VALUES ('t6g_2', 't6v_1', 'delivery', 'keeper', 'client', 'client', NULL, '{}', '[]', 'handed_off', NULL, 1)")
            db.execute("INSERT INTO delivery_carriers VALUES ('t6g_2', 'frameio-share', NULL)")
        values = {'drives': self.drives[:2], 'project': project, 'party': 'Invented Studio', 'party_kind': 'studio', 'on': '2026-11-02'}
        refused = self.call('drive', 'release', {**values, 'delivery': 't6g_2'})
        self.assertIsInstance(refused, Refused)
        self.assertEqual(refused.rule, 'delivery')
        done = self.write('release', {**values, 'delivery': 't6g_1'})
        shipment = done.result['shipment_id']
        self.assertEqual([d.id for d, _ in self.store.shipment_events(shipment)], self.drives[:2])
        with self.registry._transaction() as db:
            self.assertEqual(db.execute("SELECT drive_id FROM delivery_drives WHERE package_id='t6g_1' ORDER BY drive_id").fetchall(), [(d,) for d in sorted(self.drives[:2])])
            self.assertEqual(db.execute("SELECT ref FROM delivery_carriers WHERE package_id='t6g_1'").fetchone(), ('free text drive',))

    def test_assign_and_unassign_keep_history(self) -> None:
        drive, one, two = self.drives[0], *self.projects
        self.write('assign', {'drive': drive, 'project': one, 'label': 'A1', 'role': 'master', 'start': '2026-11-01'})
        self.assertEqual([(d.id, a.label) for d, a in self.store.open_assignments(self.ref(one))], [(drive, 'A1')])
        self.write('unassign', {'drive': drive, 'project': one, 'end': '2026-11-10'})
        self.assertEqual(self.store.open_assignments(self.ref(one)), [])
        self.write('assign', {'drive': drive, 'project': two, 'label': 'A1', 'role': 'swing', 'start': '2026-11-11'})
        history = self.store.assignment_history(drive)
        self.assertEqual([(a.label, a.role, str(a.start), str(a.end)) for a in history], [('A1', Role.MASTER, '2026-11-01', '2026-11-10'), ('A1', Role.SWING, '2026-11-11', 'None')])
        self.assertEqual(self.store.list_events(drive), ())

    def ref(self, project: str) -> Any:
        from t6.drives import Ref
        return Ref(page_id=project)

    def test_unassign_refuses_nothing_open_and_flags_ambiguity(self) -> None:
        drive, one = self.drives[0], self.projects[0]
        self.assertIsInstance(self.call('drive', 'unassign', {'drive': drive, 'project': one}), Refused)
        self.write('assign', {'drive': drive, 'project': one, 'label': 'A', 'role': 'master', 'start': '2026-11-01'}, 'a')
        self.write('assign', {'drive': drive, 'project': one, 'label': 'B', 'role': 'backup', 'start': '2026-11-01'}, 'b')
        ambiguous = self.call('drive', 'unassign', {'drive': drive, 'project': one})
        self.assertIsInstance(ambiguous, Ambiguous)
        self.assertEqual([(c['assignment'], c['label'], c['role']) for c in ambiguous.candidates], [(1, 'A', 'master'), (2, 'B', 'backup')])
        self.write('unassign', {'drive': drive, 'project': one, 'label': 'B', 'end': '2026-11-02'})
        self.assertEqual([a.label for _, a in self.store.open_assignments(self.ref(one))], ['A'])
        early = self.call('drive', 'unassign', {'drive': drive, 'project': one, 'label': 'A', 'end': '2026-10-01'})
        self.assertIsInstance(early, Refused)

    def test_assign_refuses_a_second_open_assignment_for_the_same_drive_project_and_role(self) -> None:
        drive, one, two = self.drives[0], *self.projects
        self.write('assign', {'drive': drive, 'project': one, 'label': 'A', 'role': 'master', 'start': '2026-11-01'})
        refused = self.call('drive', 'assign', {'drive': drive, 'project': one, 'label': 'B', 'role': 'master'})
        self.assertIsInstance(refused, Refused)
        self.assertEqual(refused.rule, 'assignment')
        self.write('assign', {'drive': drive, 'project': one, 'label': 'B', 'role': 'backup'})
        self.write('assign', {'drive': drive, 'project': two, 'label': 'B', 'role': 'master'})
        self.write('unassign', {'drive': drive, 'project': one, 'label': 'A', 'end': '2026-11-02'})
        self.write('assign', {'drive': drive, 'project': one, 'label': 'C', 'role': 'master'})

    def test_unassign_can_always_be_picked_by_role_or_by_assignment_number(self) -> None:
        drive, one = self.drives[0], self.projects[0]
        self.write('assign', {'drive': drive, 'project': one, 'label': 'A', 'role': 'master', 'start': '2026-11-01'}, 'a')
        self.write('assign', {'drive': drive, 'project': one, 'label': 'A', 'role': 'backup', 'start': '2026-11-01'}, 'b')
        ambiguous = self.call('drive', 'unassign', {'drive': drive, 'project': one, 'label': 'A'})
        self.assertIsInstance(ambiguous, Ambiguous)
        self.write('unassign', {'drive': drive, 'project': one, 'label': 'A', 'role': 'backup', 'end': '2026-11-02'})
        self.assertEqual([a.role for _, a in self.store.open_assignments(self.ref(one))], [Role.MASTER])
        # Rows from the old workbooks can repeat label and role: only the number picks then.
        self.store.import_row('row-1', Drive(serial='SYNTH-X'), Assignment(label='Z', project=self.ref(one), role=Role.SWING, start=date(2026, 1, 1)), [])
        other = self.store.find_by_serial('SYNTH-X')
        assert other is not None
        self.store.import_row('row-2', Drive(serial='SYNTH-X'), Assignment(label='Z', project=self.ref(one), role=Role.SWING, start=date(2026, 1, 2)), [])
        stuck = self.call('drive', 'unassign', {'drive': other.id, 'project': one, 'label': 'Z', 'role': 'swing'})
        self.assertIsInstance(stuck, Ambiguous)
        self.assertEqual([c['assignment'] for c in stuck.candidates], [1, 2])
        self.write('unassign', {'drive': other.id, 'project': one, 'assignment': 2, 'end': '2026-11-02'})
        self.assertEqual([a.start for a in self.store.list_assignments(other.id) if a.is_open], [date(2026, 1, 1)])
        self.assertEqual(self.call('drive', 'unassign', {'drive': other.id, 'project': one, 'assignment': 2}).rule, 'assignment')

    def test_unassign_by_number_ends_an_assignment_whatever_its_project_is_and_without_a_number_the_project_is_required(self) -> None:
        one = self.projects[0]
        self.store.import_row('row-1', Drive(serial='SYNTH-T'), Assignment(label='T', project=Ref(text='Invented Show'), role=Role.MASTER, start=date(2026, 1, 1)), [])
        text_only = self.store.find_by_serial('SYNTH-T')
        assert text_only is not None
        self.write('assign', {'drive': text_only.id, 'project': one, 'label': 'T2', 'role': 'backup', 'start': '2026-02-01'})
        self.assertEqual(self.call('drive', 'unassign', {'drive': text_only.id}).rule, 'input')
        self.assertEqual(self.call('drive', 'unassign', {'drive': text_only.id, 'assignment': 0}).rule, 'input')
        self.assertEqual(self.call('drive', 'unassign', {'drive': text_only.id, 'project': one, 'assignment': 1}).rule, 'assignment')
        done = self.write('unassign', {'drive': text_only.id, 'assignment': 1, 'end': '2026-03-01'})
        self.assertEqual((done.result['assignment'], done.result['label'], done.result['project_id']), (1, 'T', None))
        self.assertEqual([(a.label, a.end) for a in self.store.list_assignments(text_only.id)], [('T', date(2026, 3, 1)), ('T2', None)])
        self.assertEqual(self.call('drive', 'unassign', {'drive': text_only.id, 'assignment': 1}).rule, 'assignment')
        self.write('unassign', {'drive': text_only.id, 'assignment': 2, 'end': '2026-03-02'})  # a registered Project needs no name either
        self.assertEqual([a.end for a in self.store.list_assignments(text_only.id)], [date(2026, 3, 1), date(2026, 3, 2)])
        self.assertEqual([x.action for x in self.registry.activity(text_only.id)], ['drive_unassign'])  # no Project to file it under
        self.assertEqual([x.action for x in self.registry.activity(one)][:2], ['drive_unassign', 'drive_assign'])

    def test_assign_stores_remarks_and_drive_get_shows_them(self) -> None:
        drive, one = self.drives[0], self.projects[0]
        self.write('assign', {'drive': drive, 'project': one, 'label': 'A1', 'role': 'master', 'remarks': 'sticker on the left'}, 'a')
        self.write('assign', {'drive': drive, 'project': one, 'label': 'A2', 'role': 'backup'}, 'b')
        shown = self.read('get', {'drive': drive})['assignments']
        self.assertEqual([(a['label'], a['remarks']) for a in shown], [('A1', 'sticker on the left'), ('A2', None)])
        self.assertEqual(self.store.list_assignments(drive)[0].remarks, 'sticker on the left')

    def test_the_delivery_link_is_shown_on_the_delivery_and_named_in_the_audit_entry(self) -> None:
        from t6.package import PackageStore
        project = self.projects[0]
        with self.registry._transaction() as db:
            db.execute("INSERT INTO deliverables VALUES ('t6v_1', ?, 'FTR', NULL, NULL, NULL)", (project,))
            db.execute("INSERT INTO packages VALUES ('t6g_1', 't6v_1', 'delivery', 'keeper', 'client', 'client', NULL, '{}', '[]', 'handed_off', NULL, 1)")
            db.execute("INSERT INTO delivery_carriers VALUES ('t6g_1', 'drive', NULL)")
        packages = PackageStore(self.registry, PreviewTokens('test'))
        self.assertEqual(packages.get('t6g_1')['drives'], [])  # type: ignore[index]
        self.write('release', {'drives': self.drives[:2], 'project': project, 'party': 'Invented Studio', 'party_kind': 'studio', 'delivery': 't6g_1'})
        self.assertEqual(packages.get('t6g_1')['drives'], sorted(self.drives[:2]))  # type: ignore[index]
        for drive in self.drives[:2]:
            (entry,) = self.registry.audit(drive)
            self.assertEqual(entry.after['delivery_id'], 't6g_1')
        self.assertEqual(self.registry.activity(project)[0].action, 'drive_release')

    def test_drive_add_registers_a_drive_with_its_facts_after_preview_and_confirm(self) -> None:
        values = {'label': 'M07', 'serial': 'new-1', 'brand': 'Acme', 'capacity_tb': 4.0, 'form_factor': 'portable', 'purchase_date': '2026-01-05',
                  'needs_review': True, 'review_note': 'check label', 'idempotency_key': 'add-1'}
        preview = self.call('drive', 'add', values)
        self.assertIsInstance(preview, Preview)
        self.assertEqual(len(self.store.list_drives()), 3)
        done = self.call('drive', 'add', {**values, 'confirm': preview.token})
        self.assertIsInstance(done, Done)
        (added,) = done.result['drive_ids']
        drive = self.store.get_drive(added)
        assert drive is not None
        self.assertEqual((drive.label, drive.serial, drive.brand, drive.capacity_tb, drive.form_factor, drive.purchase_date, drive.needs_review, drive.review_note),
                         ('M07', 'NEW-1', 'Acme', 4.0, FormFactor.PORTABLE, date(2026, 1, 5), True, 'check label'))
        self.assertEqual(self.read('get', {'drive': added})['drive']['status'], 'unknown')
        (entry,) = self.registry.audit(added)
        self.assertEqual((entry.action, entry.actor, entry.before, entry.after['serial']), ('drive_add', 'keeper', None, 'NEW-1'))
        self.assertEqual([(a.action, a.actor) for a in self.registry.activity(added)], [('drive_add', 'keeper')])

    def test_drive_add_refuses_a_recorded_serial_naming_the_holder_and_a_missing_label(self) -> None:
        refused = self.call('drive', 'add', {'label': 'Copy', 'serial': ' synth-1 '})
        self.assertIsInstance(refused, Refused)
        self.assertEqual(refused.rule, 'serial')
        self.assertIn(self.drives[1], refused.reason)
        self.assertEqual(self.call('drive', 'add', {'label': ' '}).rule, 'input')
        self.assertEqual(self.call('drive', 'add', {'label': 'X', 'purchase_date': 'soon'}).rule, 'input')
        self.assertEqual(self.call('drive', 'add', {'label': 'X', 'serial': '(old)'}).rule, 'input')
        self.assertEqual(len(self.store.list_drives()), 3)

    def test_drive_add_is_idempotent_by_key(self) -> None:
        values = {'label': 'M08', 'serial': 'N-8', 'idempotency_key': 'add-same'}
        first = self.write('add', {'label': 'M08', 'serial': 'N-8'}, 'add-same')
        replay = self.call('drive', 'add', values)
        self.assertIsInstance(replay, Preview)
        self.assertEqual(self.call('drive', 'add', {**values, 'confirm': replay.token}), first)
        self.assertEqual(len(self.store.list_drives()), 4)
        self.assertEqual(self.call('drive', 'add', {'label': 'M09', 'serial': 'N-9', 'idempotency_key': 'add-same'}).rule, 'idempotency_key')

    def test_drive_edit_changes_only_the_named_facts_and_an_empty_value_clears(self) -> None:
        drive = self.drives[0]
        self.write('edit', {'drive': drive, 'label': 'M01', 'form_factor': 'powered desktop', 'serial': 'fixed-1', 'suspected_serial': 'maybe-2',
                            'needs_review': True, 'review_note': 'look again'})
        found = self.store.get_drive(drive)
        assert found is not None
        self.assertEqual((found.label, found.form_factor, found.serial, found.suspected_serial, found.needs_review, found.review_note),
                         ('M01', FormFactor.POWERED_DESKTOP, 'FIXED-1', 'MAYBE-2', True, 'look again'))
        self.write('edit', {'drive': drive, 'review_note': 'resolved'})
        found = self.store.get_drive(drive)
        assert found is not None
        self.assertEqual((found.label, found.needs_review, found.review_note), ('M01', True, 'resolved'))
        self.write('edit', {'drive': drive, 'suspected_serial': '', 'form_factor': '', 'review_note': '', 'needs_review': False})
        found = self.store.get_drive(drive)
        assert found is not None
        self.assertEqual((found.suspected_serial, found.form_factor, found.review_note, found.needs_review, found.serial), (None, None, None, False, 'FIXED-1'))
        entries = self.registry.audit(drive)
        self.assertEqual([e.action for e in entries], ['drive_edit'] * 3)
        self.assertEqual(((entries[0].before or {})['label'], entries[0].after['label']), ('Synthetic 0', 'M01'))
        self.assertEqual([a.action for a in self.registry.activity(drive)], ['drive_edit'] * 3)

    def test_drive_edit_refuses_what_the_drive_rules_or_the_store_would_refuse_and_changes_nothing(self) -> None:
        drive = self.drives[0]
        self.assertEqual(self.call('drive', 'edit', {'drive': drive}).rule, 'input')
        self.assertEqual(self.call('drive', 'edit', {'drive': 't6d_' + '0' * 32, 'label': 'X'}).rule, 'drive')
        held = self.call('drive', 'edit', {'drive': drive, 'serial': 'synth-1'})
        self.assertIsInstance(held, Refused)
        self.assertEqual(held.rule, 'serial')
        self.assertIn(self.drives[1], held.reason)
        only = self.write('add', {'label': 'Only label'}).result['drive_ids'][0]
        self.assertEqual(self.call('drive', 'edit', {'drive': only, 'label': ''}).rule, 'input')
        self.assertEqual(self.call('drive', 'edit', {'drive': drive, 'serial': '', 'label': ''}).rule, 'input')
        self.assertEqual([d.label for d in self.store.list_drives()], ['Synthetic 0', 'Synthetic 1', 'Synthetic 2', 'Only label'])
        self.assertEqual(self.store.get_drive(drive).serial, 'SYNTH-0')  # type: ignore[union-attr]

    def test_drive_edit_confirm_is_refused_when_the_drive_changed_after_the_preview(self) -> None:
        drive = self.drives[0]
        values = {'drive': drive, 'label': 'M01', 'idempotency_key': 'edit-late'}
        preview = self.call('drive', 'edit', values)
        self.assertIsInstance(preview, Preview)
        self.store.set_label(drive, 'Changed elsewhere')
        self.assertEqual(self.call('drive', 'edit', {**values, 'confirm': preview.token}).rule, 'drive_changed')
        self.assertEqual(self.store.get_drive(drive).label, 'Changed elsewhere')  # type: ignore[union-attr]

    def test_drive_edit_changes_every_fact_with_the_rules_of_drive_add_and_a_blank_clears(self) -> None:
        drive = self.drives[0]
        self.write('edit', {'drive': drive, 'description': 'invented case', 'brand': 'Invented Brand', 'model': 'IB-1', 'capacity_tb': 4.0, 'format': 'exFAT',
                            'owner_studio_id': 't6s_invented', 'purchase_date': '2026-03-04', 'warranty': 'to 2028', 'remarks': 'scuffed'})
        found = self.store.get_drive(drive)
        assert found is not None
        self.assertEqual((found.description, found.brand, found.model, found.capacity_tb, found.format, found.owner_studio_id, found.purchase_date, found.warranty, found.remarks),
                         ('invented case', 'Invented Brand', 'IB-1', 4.0, 'exFAT', 't6s_invented', date(2026, 3, 4), 'to 2028', 'scuffed'))
        self.write('edit', {'drive': drive, 'owner_studio_id': '', 'ownership_known': False, 'capacity_tb': '', 'purchase_date': '', 'brand': '', 'remarks': ' '})
        found = self.store.get_drive(drive)
        assert found is not None
        self.assertEqual((found.ownership.value, found.owner_studio_id, found.capacity_tb, found.purchase_date, found.brand, found.remarks, found.model), ('unknown', None, None, None, None, None, 'IB-1'))
        self.assertEqual(self.registry.audit(drive)[-1].after['brand'], None)

    def test_drive_edit_refuses_a_fact_that_drive_add_would_refuse_and_changes_nothing(self) -> None:
        drive = self.drives[0]
        before = asdict(self.store.get_drive(drive))  # type: ignore[arg-type]
        for values in ({'capacity_tb': 0}, {'capacity_tb': -2.5}, {'purchase_date': 'next week'}, {'ownership_known': False, 'owner_studio_id': 't6s_invented'}):
            with self.subTest(values=values):
                self.assertEqual(self.call('drive', 'edit', {'drive': drive, **values}).rule, 'input')
        self.assertEqual(asdict(self.store.get_drive(drive)), before)  # type: ignore[arg-type]

    def test_drive_edit_can_clear_one_identifying_fact_while_setting_another_in_the_same_edit(self) -> None:
        drive = self.drives[0]
        self.write('edit', {'drive': drive, 'serial': '', 'label': '', 'description': 'now only described'})
        found = self.store.get_drive(drive)
        assert found is not None
        self.assertEqual((found.serial, found.label, found.description), (None, None, 'now only described'))

    def test_drive_get_and_list_give_a_version_that_changes_with_the_facts_and_drive_edit_checks_it(self) -> None:
        drive = self.drives[0]
        first = self.read('get', {'drive': drive})['drive']['version']
        self.assertEqual({d['id']: d['version'] for d in self.read('list')['drives']}[drive], first)
        self.assertEqual(len({d['version'] for d in self.read('list')['drives']}), 3)
        stale = self.call('drive', 'edit', {'drive': drive, 'label': 'M01', 'expected_version': 'not-current'})
        self.assertIsInstance(stale, Refused)
        self.assertEqual(stale.rule, 'drive_changed')
        self.assertEqual(self.store.get_drive(drive).label, 'Synthetic 0')  # type: ignore[union-attr]
        self.write('edit', {'drive': drive, 'label': 'M01', 'expected_version': first})
        second = self.read('get', {'drive': drive})['drive']['version']
        self.assertNotEqual(second, first)
        self.write('edit', {'drive': drive, 'label': 'Synthetic 0'})
        self.assertEqual(self.read('get', {'drive': drive})['drive']['version'], first)  # the same facts give the same version
        self.write('assign', {'drive': drive, 'project': self.projects[0], 'label': 'A1', 'role': 'master'})
        self.assertEqual(self.read('get', {'drive': drive})['drive']['version'], first)  # an Assignment is not a fact

    def test_drive_edit_checks_the_expected_version_again_at_confirm(self) -> None:
        drive = self.drives[0]
        version = self.read('get', {'drive': drive})['drive']['version']
        values = {'drive': drive, 'label': 'M01', 'expected_version': version, 'idempotency_key': 'versioned'}
        preview = self.call('drive', 'edit', values)
        self.assertIsInstance(preview, Preview)
        self.store.set_facts(drive, {'brand': 'Changed elsewhere'})
        refused = self.call('drive', 'edit', {**values, 'confirm': preview.token})
        self.assertIsInstance(refused, Refused)
        self.assertEqual(refused.rule, 'drive_changed')
        self.assertEqual(self.store.get_drive(drive).label, 'Synthetic 0')  # type: ignore[union-attr]

    def test_drive_promote_serial_accepts_the_suspected_serial_once_it_is_unique(self) -> None:
        a, b, c = self.drives
        self.assertEqual(self.call('drive', 'promote-serial', {'drive': a}).rule, 'serial')
        self.assertEqual(self.call('drive', 'promote-serial', {'drive': 't6d_' + '0' * 32}).rule, 'drive')
        self.store.set_suspected_serial(b, 'SYNTH-2')
        held = self.call('drive', 'promote-serial', {'drive': b})
        self.assertIsInstance(held, Refused)
        self.assertEqual(held.rule, 'serial')
        self.assertIn(c, held.reason)
        self.store.set_suspected_serial(a, 'new-7')
        self.write('promote-serial', {'drive': a})
        promoted = self.store.get_drive(a)
        assert promoted is not None
        self.assertEqual((promoted.serial, promoted.suspected_serial), ('NEW-7', None))
        (entry,) = self.registry.audit(a)
        self.assertEqual((entry.action, (entry.before or {})['serial'], (entry.before or {})['suspected_serial'], entry.after['serial']), ('drive_promote_serial', 'SYNTH-0', 'NEW-7', 'NEW-7'))
        self.assertEqual([x.action for x in self.registry.activity(a)], ['drive_promote_serial'])

    def test_the_verbs_use_the_drive_store_interface_only(self) -> None:
        import ast
        import inspect
        import re
        import typing
        from t6 import drive_verbs
        tree = ast.parse(inspect.getsource(drive_verbs))
        imported = [alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) for alias in node.names]
        self.assertEqual([name for name in imported if name.startswith('_')], [])
        defined = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
        self.assertFalse(defined & {'_check_project', 'add_assignment', 'release_shipment', '_append'})
        sql = ' '.join(node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str) and re.match(r'\s*(SELECT|INSERT|UPDATE|DELETE)', node.value))
        self.assertIsNone(re.search(r'drives|drive_events|drive_assignments|delivery_drives|delivery_carriers|drive_previews', sql))
        self.assertFalse(hasattr(drive_verbs, 'VERBS'))
        self.assertIn('WRITE_VERBS', dir(drive_verbs))
        self.assertIsNot(typing.get_type_hints(drive_verbs.DriveVerbs._plan)['return'], typing.Any)

    def test_permission_and_input_refusals(self) -> None:
        values = {'drive': self.drives[0], 'project': self.projects[0]}
        refused = self.call('drive', 'receive', values, actor='reader')
        self.assertIsInstance(refused, Refused)
        self.assertIn('drives', refused.reason)
        self.assertEqual(self.store.list_events(self.drives[0]), ())
        self.assertEqual(self.call('drive', 'retire', {**values, 'reason': ' '}).rule, 'reason')
        self.assertEqual(self.call('drive', 'receive', {'drive': 't6d_' + '0' * 32, 'project': self.projects[0]}).rule, 'drive')
        self.assertEqual(self.call('drive', 'receive', {'drive': self.drives[0], 'project': 't6p_' + '0' * 24}).rule, 'project')
        self.assertEqual(self.call('drive', 'receive', {**values, 'on': 'soon'}).rule, 'input')
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            self.call('drive', 'assign', {**values, 'label': 'x', 'role': 'chef'})
        self.assertEqual(self.call('drive', 'assign', {**values, 'label': ' ', 'role': 'master'}).rule, 'input')
        self.assertEqual(self.call('drive', 'release', {'drives': [], 'project': self.projects[0], 'party': 'x', 'party_kind': 'studio'}).rule, 'input')

    def test_idempotent_replay_activity_audit_and_stale_preview(self) -> None:
        drive, project = self.drives[0], self.projects[0]
        values = {'drive': drive, 'project': project, 'on': '2026-11-01'}
        first = self.write('receive', values, 'same')
        replay = self.call('drive', 'receive', {**values, 'idempotency_key': 'same'})
        self.assertIsInstance(replay, Preview)
        again = self.call('drive', 'receive', {**values, 'idempotency_key': 'same', 'confirm': replay.token})
        self.assertEqual(again, first)
        self.assertEqual(len(self.store.list_events(drive)), 1)
        self.assertEqual(self.call('drive', 'receive', {**values, 'on': '2026-11-02', 'idempotency_key': 'same'}).rule, 'idempotency_key')
        self.assertEqual([a.action for a in self.registry.activity(project)][:1], ['drive_receive'])
        self.assertEqual(self.registry.activity(project)[0].actor, 'keeper')
        self.assertEqual(len(self.registry.audit(drive)), 1)
        stale = self.call('drive', 'retire', {'drive': drive, 'project': project, 'reason': 'lost', 'idempotency_key': 'late'})
        self.write('release', {'drives': [drive], 'project': project, 'party': 'Invented Studio', 'party_kind': 'studio'})
        changed = self.call('drive', 'retire', {'drive': drive, 'project': project, 'reason': 'lost', 'idempotency_key': 'late', 'confirm': stale.token})
        self.assertEqual(changed.rule, 'drive_changed')
        self.assertEqual(self.registry.activity(project)[0].action, 'drive_release')

    def test_catalogue_and_cli(self) -> None:
        from cli_api_fake import client_patch
        from t6.cli import main
        names = {(e['noun'], e['verb']) for e in catalogue()}
        self.assertTrue({('drive', verb) for verb in ('receive', 'release', 'return', 'retire', 'assign', 'unassign')} <= names)
        self.assertEqual({e['role'] for e in catalogue() if e['noun'] == 'drive' and not e['read_only']}, {'drives'})
        out = io.StringIO()
        with patch.dict(os.environ, {'T6_PERSON_ID': 'keeper', 'T6_PREVIEW_SIGNING_KEY': 'test'}), patch('t6.cli._open_registry', return_value=self.registry), client_patch(lambda _: None), redirect_stdout(out):
            status = main(['drive', 'retire', self.drives[0], self.projects[0], '--reason', 'dead'])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(out.getvalue())['outcome'], 'preview')
        out = io.StringIO()
        with patch.dict(os.environ, {'T6_PERSON_ID': 'reader', 'T6_PREVIEW_SIGNING_KEY': 'test'}), patch('t6.cli._open_registry', return_value=self.registry), client_patch(lambda _: None), redirect_stdout(out):
            status = main(['drive', 'get', '--drive', self.drives[0]])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(out.getvalue())['result']['drive']['serial'], 'SYNTH-0')


if __name__ == '__main__':
    unittest.main()
