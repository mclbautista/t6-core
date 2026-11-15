"""Drive custody and assignment verbs on the t6 database."""
import io
import json
import os
import unittest
from contextlib import redirect_stdout
from typing import Any
from unittest.mock import patch

from t6.drives import Drive, DriveStatus, Role
from t6.drives.sqlite import SqliteDriveStore
from t6.ids import EntityKind, T6Id
from t6.person import People
from t6.preview import PreviewTokens
from t6.registry import Registered
from t6.verbs import VERBS, Ambiguous, Done, Preview, Refused, catalogue, invoke
from test_person import MemoryRegistry

CONTACT = '12345678123412341234123456789abc'


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

    def test_custody_sequence_derives_status_and_retire_is_not_terminal(self) -> None:
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
        self.write('receive', {'drive': drive, 'project': project, 'on': '2026-11-05'})
        self.assertIs(self.store.status(drive), DriveStatus.IN_CUSTODY)
        events = self.store.list_events(drive)
        self.assertEqual([e.kind.value for e in events], ['received', 'released', 'returned', 'retired', 'received'])
        self.assertEqual(events[1].party.ref.text, 'Invented Studio')  # type: ignore[union-attr]
        self.assertEqual(events[0].party.ref.page_id, CONTACT)  # type: ignore[union-attr]
        self.assertEqual(events[3].reason, 'dead')

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
        self.assertIsInstance(self.call('drive', 'unassign', {'drive': drive, 'project': one}), Ambiguous)
        self.write('unassign', {'drive': drive, 'project': one, 'label': 'B', 'end': '2026-11-02'})
        self.assertEqual([a.label for _, a in self.store.open_assignments(self.ref(one))], ['A'])
        early = self.call('drive', 'unassign', {'drive': drive, 'project': one, 'label': 'A', 'end': '2026-10-01'})
        self.assertIsInstance(early, Refused)

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
        stale = self.call('drive', 'return', {'drive': drive, 'project': project, 'idempotency_key': 'late'})
        self.write('retire', {'drive': drive, 'project': project, 'reason': 'lost'})
        changed = self.call('drive', 'return', {'drive': drive, 'project': project, 'idempotency_key': 'late', 'confirm': stale.token})
        self.assertEqual(changed.rule, 'drive_changed')
        self.assertEqual(self.registry.activity(project)[0].action, 'drive_retire')

    def test_catalogue_and_cli(self) -> None:
        from cli_api_fake import client_patch
        from t6.cli import main
        names = {(e['noun'], e['verb']) for e in catalogue()}
        self.assertTrue({('drive', verb) for verb in ('receive', 'release', 'return', 'retire', 'assign', 'unassign')} <= names)
        self.assertEqual({e['role'] for e in catalogue() if e['noun'] == 'drive'}, {'drives'})
        out = io.StringIO()
        with patch.dict(os.environ, {'T6_PERSON_ID': 'keeper', 'T6_PREVIEW_SIGNING_KEY': 'test'}), patch('t6.cli._open_registry', return_value=self.registry), client_patch(lambda _: None), redirect_stdout(out):
            status = main(['drive', 'retire', self.drives[0], self.projects[0], '--reason', 'dead'])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(out.getvalue())['outcome'], 'preview')


if __name__ == '__main__':
    unittest.main()
