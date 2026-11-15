"""Swing assign, unassign, return and overdue on the t6 database, and the DriveStore methods behind them."""
import io
import json
import os
import unittest
from contextlib import redirect_stdout
from datetime import date
from typing import Any
from unittest.mock import patch

from t6.drives import Assignment, Drive, Ref, Role, UnknownDrive
from t6.drives.sqlite import SqliteDriveStore
from t6.drives.testing import InMemoryDriveStore
from t6.ids import EntityKind, T6Id
from t6.person import People
from t6.preview import PreviewTokens
from t6.registry import Registered
from t6.verbs import VERBS, Ambiguous, Done, Preview, Refused, catalogue, invoke
from test_person import MemoryRegistry

SHOW = str(T6Id.mint(EntityKind.PROJECT))
PERSON = str(T6Id.mint(EntityKind.PERSON))


class SwingStoreTests(unittest.TestCase):
    def test_overdue_and_drop_return_date_on_the_in_memory_store(self) -> None:
        store = InMemoryDriveStore()
        late, fine, none = (store.add_drive(Drive(serial=f'SYNTH-{n}')) for n in range(3))
        project = Ref(page_id=SHOW)
        start = date(2026, 11, 1)
        store.add_assignment(late.id, Assignment(label='L', project=project, role=Role.SWING, start=start, return_date=date(2026, 11, 10)))
        store.add_assignment(fine.id, Assignment(label='F', project=project, role=Role.SWING, start=start, return_date=date(2026, 11, 20)))
        store.add_assignment(none.id, Assignment(label='N', project=project, role=Role.SWING, start=start))
        self.assertEqual([(d.id, a.label) for d, a in store.overdue_returns(date(2026, 11, 16))], [(late.id, 'L')])
        self.assertEqual(store.overdue_returns(date(2026, 11, 10)), [])
        cleared = store.drop_return_date(late.id, 1)
        self.assertIsNone(cleared.return_date)
        self.assertEqual(store.overdue_returns(date(2026, 11, 16)), [])
        with self.assertRaises(UnknownDrive):
            store.drop_return_date('t6d_' + '0' * 32, 1)
        with self.assertRaises(ValueError):
            store.drop_return_date(late.id, 5)

    def test_return_date_before_start_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            Assignment(label='x', project=Ref(page_id=SHOW), role=Role.SWING, start=date(2026, 11, 5), return_date=date(2026, 11, 1))

    def test_an_ended_assignment_is_not_overdue(self) -> None:
        store = InMemoryDriveStore()
        drive = store.add_drive(Drive(serial='SYNTH-1'))
        store.add_assignment(drive.id, Assignment(label='L', project=Ref(page_id=SHOW), role=Role.SWING, start=date(2026, 11, 1), return_date=date(2026, 11, 5)))
        store.end_assignment(drive.id, 1, date(2026, 11, 6))
        self.assertEqual(store.overdue_returns(date(2026, 11, 16)), [])


class SwingVerbTests(unittest.TestCase):
    def setUp(self) -> None:
        env = patch.dict(os.environ, {'T6_PREVIEW_SIGNING_KEY': 'test'})
        env.start()
        self.addCleanup(env.stop)
        self.registry = MemoryRegistry()
        self.addCleanup(self.registry.db.close)
        self.people = People(self.registry, PreviewTokens('test'))
        self.registry._clock = lambda: 1_794_772_800.0  # 2026-11-15 20:00 UTC, already 2026-11-16 in Manila
        with self.registry._transaction() as db:
            for person in ('keeper', 'reader'):
                db.execute("INSERT INTO persons VALUES (?, 'staff', '[]', '[]', NULL, 0, 1)", (person,))
            db.execute("INSERT INTO persons VALUES (?, 'staff', '[]', '[]', NULL, 0, 1)", (PERSON,))
            db.execute("INSERT INTO person_permissions VALUES ('keeper', 'drives')")
        self.registry._new_id = lambda: str(T6Id.mint(EntityKind.PROJECT))
        self.projects = []
        for key in ('one', 'two'):
            project = self.registry.register(actor='setup', idempotency_key=key)
            assert isinstance(project, Registered)
            self.projects.append(project.project.id)
        self.store = SqliteDriveStore(self.registry)
        self.drives = [self.store.add_drive(Drive(serial=f'SYNTH-{n}', label=f'Synthetic {n}')).id for n in range(2)]

    def call(self, verb: str, values: dict[str, Any], actor: str = 'keeper') -> Any:
        entry = next(e for e in VERBS if (e.noun, e.verb) == ('swing', verb))
        return invoke(entry, entry.input_model.model_validate(values), actor=actor, people=self.people, open_notion=lambda _: None)

    def write(self, verb: str, values: dict[str, Any], key: str) -> Any:
        preview = self.call(verb, {**values, 'idempotency_key': key})
        self.assertIsInstance(preview, Preview)
        result = self.call(verb, {**values, 'idempotency_key': key, 'confirm': preview.token})
        self.assertIsInstance(result, Done)
        return result

    def test_assign_with_a_return_date_is_recorded_as_a_swing_assignment(self) -> None:
        drive, one = self.drives[0], self.projects[0]
        values = {'drive': drive, 'target': one, 'label': 'SW1', 'start': '2026-11-10', 'return_date': '2026-11-14'}
        preview = self.call('assign', {**values, 'idempotency_key': 'a'})
        self.assertIsInstance(preview, Preview)
        self.assertEqual(self.store.list_assignments(drive), ())  # nothing before confirm
        done = self.call('assign', {**values, 'idempotency_key': 'a', 'confirm': preview.token})
        self.assertEqual(done.result['return_date'], '2026-11-14')
        (held,) = self.store.list_assignments(drive)
        self.assertEqual((held.label, held.role, held.project, held.return_date, held.holder), ('SW1', Role.SWING, Ref(page_id=one), date(2026, 11, 14), None))
        again = self.call('assign', {**values, 'idempotency_key': 'a', 'confirm': preview.token})
        self.assertIsInstance(again, Refused)  # the token is single use
        self.assertEqual(len(self.store.list_assignments(drive)), 1)
        with self.registry._transaction() as db:
            self.assertEqual(db.execute("SELECT action, actor FROM audit WHERE action='swing_assign'").fetchall(), [('swing_assign', 'keeper')])
            self.assertEqual(db.execute("SELECT project_id, action FROM activity WHERE action='swing_assign'").fetchall(), [(one, 'swing_assign')])

    def test_assign_to_a_person_needs_the_project_and_records_the_holder(self) -> None:
        drive, one = self.drives[0], self.projects[0]
        refused = self.call('assign', {'drive': drive, 'target': PERSON})
        self.assertIsInstance(refused, Refused)
        self.assertEqual(refused.rule, 'project')
        self.write('assign', {'drive': drive, 'target': PERSON, 'project': one}, 'p')
        (held,) = self.store.list_assignments(drive)
        self.assertEqual((held.holder, held.project), (PERSON, Ref(page_id=one)))
        unknown = self.call('assign', {'drive': self.drives[1], 'target': str(T6Id.mint(EntityKind.PERSON)), 'project': one})
        self.assertIsInstance(unknown, Refused)

    def test_assign_refuses_bad_input(self) -> None:
        drive, one = self.drives[0], self.projects[0]
        for values in ({'drive': drive, 'target': one, 'return_date': 'soon'},
                       {'drive': drive, 'target': one, 'start': '2026-11-10', 'return_date': '2026-11-01'},
                       {'drive': 't6d_' + '0' * 32, 'target': one},
                       {'drive': drive, 'target': 'not-an-id'}):
            self.assertIsInstance(self.call('assign', values), Refused, values)
        self.write('assign', {'drive': drive, 'target': one, 'start': '2026-11-10'}, 'x')
        self.assertIsInstance(self.call('assign', {'drive': drive, 'target': one}), Refused)  # one open swing per Project

    def test_return_clears_the_return_date_and_ends_the_swing(self) -> None:
        drive, one = self.drives[0], self.projects[0]
        self.write('assign', {'drive': drive, 'target': one, 'start': '2026-11-10', 'return_date': '2026-11-14'}, 'a')
        done = self.write('return', {'drive': drive, 'end': '2026-11-15'}, 'r')
        self.assertIsNone(done.result.get('return_date'))
        (held,) = self.store.list_assignments(drive)
        self.assertEqual((held.end, held.return_date, held.is_open), (date(2026, 11, 15), None, False))
        self.assertEqual(self.call('overdue', {}).result['overdue'], [])
        self.assertIsInstance(self.call('return', {'drive': drive}), Refused)  # nothing open

    def test_unassign_ends_the_swing_and_keeps_the_return_date_in_history(self) -> None:
        drive, one = self.drives[0], self.projects[0]
        self.write('assign', {'drive': drive, 'target': one, 'start': '2026-11-10', 'return_date': '2026-11-14'}, 'a')
        self.write('unassign', {'drive': drive, 'end': '2026-11-12'}, 'u')
        (held,) = self.store.list_assignments(drive)
        self.assertEqual((held.end, held.return_date), (date(2026, 11, 12), date(2026, 11, 14)))
        self.assertEqual(self.call('overdue', {}).result['overdue'], [])

    def test_several_open_swings_are_ambiguous_until_named(self) -> None:
        drive, one, two = self.drives[0], *self.projects
        self.write('assign', {'drive': drive, 'target': one, 'label': 'A', 'start': '2026-11-10'}, 'a')
        self.write('assign', {'drive': drive, 'target': two, 'label': 'B', 'start': '2026-11-10'}, 'b')
        ambiguous = self.call('return', {'drive': drive})
        self.assertIsInstance(ambiguous, Ambiguous)
        self.write('return', {'drive': drive, 'project': two, 'end': '2026-11-11'}, 'r')
        self.assertEqual([a.label for a in self.store.list_assignments(drive) if a.is_open], ['A'])

    def test_overdue_returns_are_readable_by_a_reader_in_manila_time(self) -> None:
        one = self.projects[0]
        self.write('assign', {'drive': self.drives[0], 'target': one, 'label': 'LATE', 'start': '2026-11-01', 'return_date': '2026-11-10'}, 'a')
        self.write('assign', {'drive': self.drives[1], 'target': one, 'label': 'OK', 'start': '2026-11-01', 'return_date': '2026-11-16'}, 'b')
        result = self.call('overdue', {}, actor='reader')
        self.assertIsInstance(result, Done)
        self.assertEqual(result.result['today'], '2026-11-16')
        self.assertEqual([(o['drive_id'], o['label'], o['return_date'], o['days_overdue'], o['project_id'])
                          for o in result.result['overdue']], [(self.drives[0], 'LATE', '2026-11-10', 6, one)])

    def test_write_verbs_need_the_drives_permission_and_the_catalogue_lists_them(self) -> None:
        entries = {(e.verb): e for e in VERBS if e.noun == 'swing'}
        self.assertEqual(set(entries), {'assign', 'unassign', 'return', 'overdue'})
        for verb in ('assign', 'unassign', 'return'):
            self.assertEqual((entries[verb].role, entries[verb].read_only), ('drives', False))
        self.assertEqual((entries['overdue'].role, entries['overdue'].read_only), ('reader', True))
        listed = {(e['noun'], e['verb']) for e in catalogue()}
        self.assertTrue({('swing', 'assign'), ('swing', 'unassign'), ('swing', 'return'), ('swing', 'overdue')} <= listed)

    def test_cli_swing_assign_preview(self) -> None:
        from cli_api_fake import client_patch
        from t6.cli import main
        out = io.StringIO()
        with patch.dict(os.environ, {'T6_PERSON_ID': 'keeper', 'T6_PREVIEW_SIGNING_KEY': 'test'}), patch('t6.cli._open_registry', return_value=self.registry), client_patch(lambda _: None), redirect_stdout(out):
            status = main(['swing', 'assign', self.drives[0], self.projects[0], '--return-date', '2026-11-20'])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(out.getvalue())['outcome'], 'preview')


if __name__ == '__main__':
    unittest.main()
