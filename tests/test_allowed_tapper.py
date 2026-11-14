"""Action rules are stored with cards and checked against current Person records."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from t6.domain.events import JsonValue
from t6.operator import Action, ActionStore, OperatorService, TapResult
from t6.person import People
from t6.preview import PreviewTokens
from t6.registry import SqliteProjectRegistry


class AllowedTapperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.registry = SqliteProjectRegistry(root / 'registry.sqlite')
        self.store = ActionStore(str(root / 'actions.sqlite'))
        self.people = People(self.registry, PreviewTokens('test'))
        self.service = OperatorService(self.registry, self.store, [1], people=self.people)
        self.freelancer = 't6n_' + 'a' * 32
        self.coordinator = 't6n_' + 'b' * 32
        with self.registry._transaction() as db:
            for person, kind, telegram in [(self.freelancer, 'freelancer', 2), (self.coordinator, 'staff', 3), ('t6n_' + 'c' * 32, 'staff', 1), ('t6n_' + 'd' * 32, 'staff', 4)]:
                db.execute('INSERT INTO persons VALUES (?, ?, ?, ?, ?, 0, 1)', (person, kind, '[]', '[]', telegram))
            db.executemany('INSERT INTO person_permissions VALUES (?, ?)', [(self.coordinator, 'coordinator'), ('t6n_' + 'c' * 32, 'operator'), ('t6n_' + 'd' * 32, 'coordinator')])

    def action(self, rule: JsonValue) -> Action:
        action, _ = self.store.create('unbound_page', {'page_id': 'unused', 'label': 'test'}, chat=99, offer_key=str(rule), ttl=60, allowed_tapper=rule)
        self.store.delivered(action.token, 1, 42)
        return action

    def tap(self, action: Action, who: int, key: str) -> TapResult:
        return self.service.tap(f'op:{action.token}:skip', operator=who, chat=99, message_id=42, tap_key=key)

    def test_named_person_refuses_operator_then_accepts_once(self) -> None:
        rule = f'person:{self.freelancer}'
        action = self.action(rule)
        self.assertEqual(self.tap(action, 1, 'one').outcome, 'rejected')
        with sqlite3.connect(self.store._path) as db:
            self.assertEqual(json.loads(db.execute('SELECT rule FROM rejections').fetchone()[0]), rule)
        self.assertEqual(self.tap(action, 2, 'two').outcome, 'skipped')
        self.assertEqual(self.tap(action, 2, 'three').outcome, 'already_handled')

    def test_coordinators_and_any_of(self) -> None:
        for who in (3, 4):
            action, _ = self.store.create('unbound_page', {}, chat=99, offer_key=str(who), ttl=60, allowed_tapper='permission:coordinator')
            self.store.delivered(action.token, 1, 42)
            self.assertEqual(self.tap(action, who, str(who)).outcome, 'skipped')
        action = self.action({'any_of': [f'person:{self.freelancer}', 'permission:coordinator']})
        self.assertEqual(self.tap(action, 2, 'any').outcome, 'skipped')

    def test_inactive_and_changed_permissions_are_enforced(self) -> None:
        action = self.action('permission:coordinator')
        with self.registry._transaction() as db:
            db.execute('DELETE FROM person_permissions WHERE person_id = ?', (self.coordinator,))
            db.execute('UPDATE persons SET active = 0 WHERE id = ?', (self.freelancer,))
        self.assertEqual(self.tap(action, 3, 'revoked').outcome, 'rejected')
        action = self.action(f'person:{self.freelancer}')
        self.assertEqual(self.tap(action, 2, 'inactive').outcome, 'rejected')

    def test_existing_database_defaults_old_cards_to_operator(self) -> None:
        action = self.action('permission:operator')
        with sqlite3.connect(self.store._path) as db:
            db.execute('ALTER TABLE actions DROP COLUMN allowed_tapper')
            db.execute('ALTER TABLE rejections DROP COLUMN rule')
        reopened = ActionStore(self.store._path)
        first = reopened.get(action.token)
        second = ActionStore(self.store._path).get(action.token)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        assert first is not None and second is not None
        self.assertEqual(first.allowed_tapper, 'permission:operator')
        self.assertEqual(second.allowed_tapper, 'permission:operator')
