"""`Sixto (auto)` is a named service that may run Concern verbs and nothing else."""
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from t6.concern import ConcernStore
from t6.ids import EntityKind, T6Id
from t6.person import AUTO_ACTOR, People
from t6.preview import PreviewTokens
from t6.registry import Registered
from t6.server import Request, ServerConfig, WebhookApp
from t6.server.queue import JobQueue
from t6.server.verbs import VerbAPI
from t6.verbs import Preview
from test_person import MemoryRegistry


class AutoActorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = MemoryRegistry()
        self.addCleanup(self.registry.db.close)
        self.registry._new_id = lambda: str(T6Id.mint(EntityKind.PROJECT))
        project = self.registry.register(actor='setup', idempotency_key='setup', notion_page_id='project')
        assert isinstance(project, Registered)
        self.project = project.project.id
        with self.registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES ('coordinator', 'staff', '[]', '[]', NULL, 0, 1)")
        self.people = People(self.registry, PreviewTokens('secret'))

    def api(self) -> VerbAPI:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        queue = JobQueue(tmp.name + '/queue')
        return VerbAPI(self.people, queue, {'auto-secret': AUTO_ACTOR}, lambda _: None, WebhookApp(queue, ServerConfig(), registry=self.registry))

    def call(self, api: VerbAPI, path: str, body: dict[str, Any]) -> Any:
        with patch.dict('os.environ', {'T6_PREVIEW_SIGNING_KEY': 'secret'}):
            return api(Request('POST', path, {'Authorization': 'Bearer auto-secret'}, json.dumps(body).encode()))

    def test_it_is_a_named_service_that_a_secret_can_bind_to(self) -> None:
        self.api()
        self.assertIsNone(self.people.authorize(AUTO_ACTOR, 'reader'))
        self.assertIsNone(self.people.authorize(AUTO_ACTOR, 'coordinator'))
        for role in ('operator', 'admin', 'bookings', 'drives', 'llm'):
            refused = self.people.authorize(AUTO_ACTOR, role)
            self.assertIsNotNone(refused)

    def test_it_cannot_be_granted_more_than_the_concern_verbs_by_configuration(self) -> None:
        people = People(self.registry, PreviewTokens('secret'), {AUTO_ACTOR: frozenset({'operator', 'admin'})})
        self.assertIsNotNone(people.authorize(AUTO_ACTOR, 'admin'))

    def test_a_non_concern_verb_is_refused_naming_the_rule(self) -> None:
        api = self.api()
        for path, body in (('/v1/person/grant', {'person': 'coordinator', 'permission': 'admin'}),
                           ('/v1/person/assign', {'person': 'coordinator', 'project': self.project, 'role': 'coordinator'}),
                           ('/v1/person/list', {})):
            response = self.call(api, path, body)
            self.assertEqual(response.body['outcome'], 'refused', path)
            self.assertEqual(response.body['rule'], 'service', path)

    def test_a_concern_verb_passes_the_check(self) -> None:
        api = self.api()
        raised = {'project': self.project, 'owner': 'coordinator', 'waiting_on': 'client', 'anchor': 'Waiting on client', 'as_of': '2026-11-01', 'idempotency_key': 'raise'}
        response = self.call(api, '/v1/concern/raise', raised)
        self.assertNotEqual(response.body.get('rule'), 'service')
        self.assertNotEqual(response.body.get('rule'), 'permission')

    def test_the_old_concern_snooze_route_is_gone(self) -> None:
        response = self.call(self.api(), '/v1/concern/snooze', {'concern': 't6c_x', 'idempotency_key': 'snooze'})
        self.assertEqual(response.status, 404)

    def test_the_actor_cannot_be_written_for_anything_but_a_concern_change(self) -> None:
        with self.registry._transaction() as db:
            ConcernStore._log(db, self.project, 'c', AUTO_ACTOR, 'concern_auto_resolve', 1.0, None, {}, 'ok')
            with self.assertRaises(PermissionError):
                ConcernStore._log(db, self.project, 'c', AUTO_ACTOR, 'jo_close', 1.0, None, {}, 'no')

    def test_no_code_writes_the_actor_text_outside_the_permission_model(self) -> None:
        root = Path(__file__).resolve().parents[1] / 'src' / 't6'
        found = [str(p.relative_to(root)) for p in root.rglob('*.py') if AUTO_ACTOR in p.read_text(encoding='utf-8') and p.name != 'person.py']
        self.assertEqual(found, [])
