import unittest

from test_person import MemoryRegistry
from t6.person import People
from t6.preview import PreviewTokens
from t6.verbs import Done, Outcome, Preview, Refused, VERBS, PersonPermissionInput, invoke


class PermissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = MemoryRegistry()
        self.addCleanup(self.registry.db.close)
        with self.registry._transaction() as db:
            for person, telegram in [('operator', 42), ('staff', 43)]:
                db.execute("INSERT INTO persons VALUES (?, 'staff', '[]', '[]', ?, 0, 1)", (person, telegram))
        self.people = People(self.registry, PreviewTokens('secret'))

    def invoke(self, verb: str, actor: str, permission: str, key: str | None = None, token: str | None = None) -> Outcome:
        entry = next(v for v in VERBS if (v.noun, v.verb) == ('person', verb))
        return invoke(entry, PersonPermissionInput(person_id='staff', permission=permission,
                      idempotency_key=key, confirm=token), actor=actor, people=self.people,
                      open_notion=lambda: None)

    def test_seed_and_grant_revoke(self) -> None:
        self.assertEqual(self.people.seed_operators([42, 99])['unmatched'], [99])
        self.assertFalse(self.people.seed_operators([43])['seeded'])
        self.assertIsInstance(self.invoke('grant', 'staff', 'coordinator'), Refused)
        for verb in ('grant', 'revoke'):
            preview = self.invoke(verb, 'operator', 'coordinator')
            self.assertIsInstance(preview, Preview)
            assert isinstance(preview, Preview)
            result = self.invoke(verb, 'operator', 'coordinator', preview.plan['idempotency_key'], preview.token)
            self.assertIsInstance(result, Done)
            person = self.people.get('staff')
            assert person is not None
            self.assertEqual('coordinator' in person['permissions'], verb == 'grant')
            self.assertEqual(self.people.authorize('staff', 'coordinator') is None, verb == 'grant')
        self.assertEqual([a.action for a in self.registry.activity('staff')], ['person_revoke', 'person_grant'])
        refusal = self.people.authorize('staff', 'coordinator')
        assert refusal is not None
        self.assertEqual(refusal.reason, 'needs coordinator')

    def test_dispatch_enforces_registered_role_and_active_staff(self) -> None:
        from t6.verbs import Verb, ProjectHoldInput
        from typing import Any
        called: list[Any] = []
        def handler(inputs: Any, client: Any) -> Outcome:
            called.append(inputs)
            return Done(result='ok')
        entry = Verb('example', 'write', '', (), ProjectHoldInput, ProjectHoldInput,
                     'coordinator', '', handler)
        inputs = ProjectHoldInput(project='show', reason='pause')
        denied = invoke(entry, inputs, actor='staff', people=self.people, open_notion=lambda: None)
        assert isinstance(denied, Refused)
        self.assertEqual(denied.reason, 'needs coordinator')
        self.assertEqual(called, [])
        self.people.seed_operators([42])
        preview = self.invoke('grant', 'operator', 'coordinator')
        assert isinstance(preview, Preview)
        self.invoke('grant', 'operator', 'coordinator', preview.plan['idempotency_key'], preview.token)
        self.assertIsInstance(invoke(entry, inputs, actor='staff', people=self.people, open_notion=lambda: None), Done)
        with self.registry._transaction() as db:
            db.execute("UPDATE persons SET active = 0 WHERE id = 'staff'")
        self.assertIsInstance(invoke(entry, inputs, actor='staff', people=self.people, open_notion=lambda: None), Refused)
        self.assertEqual(len(called), 1)

    def test_admin_may_manage_permissions(self) -> None:
        with self.registry._transaction() as db:
            db.execute("INSERT INTO person_permissions VALUES ('operator', 'admin')")
        self.assertIsInstance(self.invoke('grant', 'operator', 'drives'), Preview)

    def test_revoked_operator_cannot_confirm(self) -> None:
        self.people.seed_operators([42])
        preview = self.invoke('grant', 'operator', 'drives')
        self.assertIsInstance(preview, Preview)
        assert isinstance(preview, Preview)
        with self.registry._transaction() as db:
            db.execute('DELETE FROM person_permissions WHERE person_id = ?', ('operator',))
        self.assertIsInstance(self.invoke('grant', 'operator', 'drives', preview.plan['idempotency_key'], preview.token), Refused)

class SeedOnOpenTests(unittest.TestCase):
    def test_open_seeds_once_and_reports_unmatched(self) -> None:
        import contextlib
        import io
        import os
        import sqlite3
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from t6.registry import sqlite as migrations
        from t6.registry.sqlite import SqliteProjectRegistry

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'db'
            with sqlite3.connect(path) as db:
                with patch.object(migrations, '_MIGRATIONS', migrations._MIGRATIONS[:-1]):
                    migrations._migrate(db)
                db.execute("INSERT INTO persons VALUES ('alice', 'staff', '[]', '[]', 42, 0, 1)")
            err = io.StringIO()
            with patch.dict(os.environ, {'T6_OPERATOR_IDS': '42, 99'}, clear=True), contextlib.redirect_stderr(err):
                registry = SqliteProjectRegistry(path)
            people = People(registry, PreviewTokens('secret'))
            person = people.get('alice')
            assert person is not None
            self.assertEqual(person['permissions'], ['operator'])
            self.assertIn('99', err.getvalue())
            with patch.dict(os.environ, {'T6_OPERATOR_IDS': '42'}, clear=True), contextlib.redirect_stderr(io.StringIO()) as second:
                SqliteProjectRegistry(path)
            self.assertEqual(second.getvalue(), '')
            self.assertEqual(len(registry.audit('alice')), 1)
