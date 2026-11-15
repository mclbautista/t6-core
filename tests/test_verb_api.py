import json
import os
import tempfile
import threading
import unittest
from typing import Any
from urllib.request import Request as HttpRequest, urlopen
from unittest.mock import patch

from t6.person import People
from t6.preview import PreviewTokens
from t6.registry.sqlite import SqliteProjectRegistry
from t6.registry import Registered
from t6.server.http import make_server
from t6.server.queue import JobQueue
from t6.server.verbs import VerbAPI, service_permissions_from_env
from t6.server.app import Request, ServerConfig, WebhookApp
from t6.operator import ActionStore, OperatorService
from t6.verbs import catalogue


class VerbAPITests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.registry = SqliteProjectRegistry(self.tmp.name + '/registry')
        self.queue = JobQueue(self.tmp.name + '/queue')
        self.actor = 't6n_' + '1' * 32
        self.staff = 't6n_' + '2' * 32
        with self.registry._transaction() as db:
            for person, telegram in ((self.actor, 42), (self.staff, 43)):
                db.execute("INSERT INTO persons VALUES (?, 'staff', '[]', '[]', ?, 0, 1)", (person, telegram))
            db.execute("INSERT INTO person_permissions VALUES (?, 'operator')", (self.actor,))
        self.env = patch.dict(os.environ, {'T6_PREVIEW_SIGNING_KEY': 'signing-key'})
        self.env.start()
        self.addCleanup(self.env.stop)
        api = VerbAPI(People(self.registry, PreviewTokens('signing-key'),
                             service_permissions_from_env({'T6_API_SERVICE_PERMISSIONS': '{"worker": ["operator"]}'})), self.queue,
                      {'operator-secret': self.actor, 'staff-secret': self.staff, 'gateway-secret': 'gateway', 'reports-secret': 'reports', 'worker-secret': 'worker'}, lambda _: None)
        self.api = api
        self.server = make_server(('127.0.0.1', 0), api)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join)
        self.addCleanup(self.server.shutdown)

    def request(self, path: str, body: dict[str, Any] | None = None, secret: str | None = 'operator-secret',
                headers: dict[str, str] | None = None) -> tuple[int, Any]:
        from urllib.error import HTTPError
        headers = dict(headers or {})
        if secret:
            headers['Authorization'] = 'Bearer ' + secret
        request = HttpRequest('http://127.0.0.1:%s%s' % (self.server.server_port, path),
                              data=json.dumps(body).encode() if body is not None else None, headers=headers)
        try:
            response = urlopen(request)
        except HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response)

    def test_auth_validation_and_catalogue(self) -> None:
        self.assertEqual(self.request('/v1/commands', secret=None)[0], 401)
        self.assertEqual(self.request('/v1/commands', secret='unknown')[0], 401)
        self.assertEqual(self.request('/v1/commands')[1], catalogue())
        status, errors = self.request('/v1/project/hold', {'project': 'x'})
        self.assertEqual(status, 422)
        self.assertEqual(errors['errors'][0]['loc'], ['reason'])
        self.assertEqual(self.request('/v1/project/hold', {'project': 'x', 'reason': 'r'}, secret='staff-secret')[1]['outcome'], 'refused')
        self.assertEqual(self.request('/v1/commands', headers={'X-Telegram-User-Id': '42'})[1]['outcome'], 'refused')
        self.assertEqual(self.request('/v1/commands', secret='gateway-secret', headers={'X-Telegram-User-Id': '42'})[1], catalogue())
        self.assertIn('schema_version', self.request('/health', secret=None)[1])

    def test_media_migration_api_uses_authenticated_actor_and_fake_notion(self) -> None:
        from t6.notion.testing import FakeNotion, external_link_properties
        fake = FakeNotion()
        fake.add_data_source('links')
        project = self.registry.register(actor='setup', idempotency_key='media-migration-project', notion_page_id='project-page')
        assert isinstance(project, Registered)
        fake.add_page('links', external_link_properties(system='resolve', kind='project', external_id='db/show', projects=['project-page'], active=True))
        fake.add_page('links', external_link_properties(system='frameio', kind='project', external_id='f', projects=['project-page']))
        fake.add_page('links', external_link_properties(system='telegram', kind='chat', external_id='c', projects=['project-page']))
        self.api.open_notion = lambda _: fake.client()
        self.api.data_sources['external_links'] = 'links'
        with self.registry._transaction() as db:
            db.execute("INSERT INTO person_permissions VALUES (?, 'admin')", (self.actor,))
        with patch.dict(os.environ, {'T6_NOTION_TOKEN': 'fake'}):
            self.assertEqual(self.request('/v1/admin/media/migrate', {}, secret='staff-secret')[1]['outcome'], 'refused')
            preview = self.request('/v1/admin/media/migrate', {'idempotency_key': 'migrate'})[1]
            self.assertEqual(preview['outcome'], 'preview')
            self.assertEqual(len(preview['plan']['review']), 2)
            result = self.request('/v1/admin/media/migrate', {'idempotency_key': 'migrate', 'confirm': preview['token']})[1]
            self.assertEqual(result['outcome'], 'done')
            replay = self.request('/v1/admin/media/migrate', {'idempotency_key': 'migrate'})[1]
            self.assertEqual(self.request('/v1/admin/media/migrate', {'idempotency_key': 'migrate', 'confirm': replay['token']})[1], result)
        self.assertEqual(self.registry.activity(project.project.id)[0].actor, self.actor)
        import io
        from contextlib import redirect_stdout
        from t6.cli import main
        env = {'T6_API_URL': 'http://127.0.0.1:%s' % self.server.server_port, 'T6_API_SECRET': 'operator-secret', 'T6_NOTION_TOKEN': 'fake'}
        with patch.dict(os.environ, env), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(['admin', 'media', 'migrate', '--idempotency-key', 'cli-migrate']), 0)
        self.assertEqual(json.loads(out.getvalue())['outcome'], 'preview')

    def test_media_cli_api_and_catalogue(self) -> None:
        from t6.client import Client
        from t6.verbs import MediaLinkInput, MediaListInput, MediaUnlinkInput, Done, Preview
        project = self.registry.register(actor='setup', idempotency_key='media-project')
        assert isinstance(project, Registered)
        client = Client('http://127.0.0.1:%s' % self.server.server_port, 'operator-secret')
        values = MediaLinkInput(owner=project.project.id, kind='nas-path', ref='/media', role='master', idempotency_key='media-link')
        self.assertEqual(self.request('/v1/media/link', values.model_dump(), secret='worker-secret')[1]['rule'], 'person')
        preview = client.media_link(values)
        assert isinstance(preview, Preview)
        values.confirm = preview.token
        done = client.media_link(values)
        assert isinstance(done, Done)
        listed = client.media_list(MediaListInput(owner=project.project.id))
        assert isinstance(listed, Done)
        self.assertEqual(listed.result['locations'][0]['id'], done.result['media_id'])
        unlink = MediaUnlinkInput(media=done.result['media_id'], idempotency_key='media-unlink')
        preview = client.media_unlink(unlink)
        assert isinstance(preview, Preview)
        unlink.confirm = preview.token
        self.assertIsInstance(client.media_unlink(unlink), Done)
        self.assertEqual({v['verb'] for v in catalogue() if v['noun'] == 'media'}, {'link', 'unlink', 'list'})
        self.assertTrue(any(v['noun'] == 'admin media' and v['verb'] == 'migrate' for v in catalogue()))
        self.assertEqual(self.registry.activity(project.project.id)[0].actor, self.actor)
        import io
        from contextlib import redirect_stdout
        from t6.cli import main
        env = {'T6_API_URL': 'http://127.0.0.1:%s' % self.server.server_port, 'T6_API_SECRET': 'operator-secret'}
        with patch.dict(os.environ, env), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(['media', 'link', project.project.id, 'nas-path', '/other', '--role', 'backup']), 0)
        self.assertEqual(json.loads(out.getvalue())['outcome'], 'preview')

    def test_timeline_verbs_over_the_api_and_cli(self) -> None:
        import io
        from contextlib import redirect_stdout
        from t6.cli import main
        from t6.client import Client
        from t6.verbs import Done, MediaLinkInput, Preview, TimelineLinkInput, TimelineListInput
        project = self.registry.register(actor='setup', idempotency_key='timeline-project')
        assert isinstance(project, Registered)
        client = Client('http://127.0.0.1:%s' % self.server.server_port, 'operator-secret')
        resolve = MediaLinkInput(owner=project.project.id, kind='resolve-project', ref='db/show', role='working', idempotency_key='timeline-resolve')
        preview = client.media_link(resolve)
        assert isinstance(preview, Preview)
        resolve.confirm = preview.token
        self.assertIsInstance(client.media_link(resolve), Done)
        stamp = 't6t_' + 'c' * 32
        values = TimelineLinkInput(project=project.project.id, timeline='Ep1', stamp=stamp, idempotency_key='timeline-link')
        self.assertEqual(self.request('/v1/timeline/link', values.model_dump(), secret='worker-secret')[1]['rule'], 'permission')
        preview = client.timeline_link(values)
        assert isinstance(preview, Preview)
        values.confirm = preview.token
        done = client.timeline_link(values)
        assert isinstance(done, Done)
        self.assertEqual(done.result['timeline_id'], stamp)
        listed = client.timeline_list(TimelineListInput(project=project.project.id))
        assert isinstance(listed, Done)
        self.assertEqual(listed.result['timelines'][0]['canonical']['resolve_project'], 'db/show')
        env = {'T6_API_URL': 'http://127.0.0.1:%s' % self.server.server_port, 'T6_API_SECRET': 'operator-secret'}
        with patch.dict(os.environ, env), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(['timeline', 'link', project.project.id, 'Ep2', '--stamp', 't6t_' + 'd' * 32]), 0)
        self.assertEqual(json.loads(out.getvalue())['outcome'], 'preview')
        with patch.dict(os.environ, env), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(['timeline', 'list', project.project.id]), 0)
        self.assertEqual(json.loads(out.getvalue())['result']['timelines'][0]['id'], stamp)

    def test_drive_verbs_over_the_api_need_the_drives_permission(self) -> None:
        from t6.client import Client
        from t6.drives import Drive, DriveStatus
        from t6.drives.sqlite import SqliteDriveStore
        from t6.verbs import DriveReceiveInput, DriveRetireInput, Done, Preview, Refused
        project = self.registry.register(actor='setup', idempotency_key='drive-project')
        assert isinstance(project, Registered)
        drive = SqliteDriveStore(self.registry).add_drive(Drive(serial='SYNTH-API')).id
        client = Client('http://127.0.0.1:%s' % self.server.server_port, 'operator-secret')
        values = DriveReceiveInput(drive=drive, project=project.project.id, idempotency_key='drive-receive')
        refused = client.drive_receive(values)
        assert isinstance(refused, Refused)
        self.assertIn('drives', refused.reason)
        with self.registry._transaction() as db:
            db.execute("INSERT INTO person_permissions VALUES (?, 'drives')", (self.actor,))
        preview = client.drive_receive(values)
        assert isinstance(preview, Preview)
        values.confirm = preview.token
        self.assertIsInstance(client.drive_receive(values), Done)
        retire = DriveRetireInput(drive=drive, project=project.project.id, reason='dead', idempotency_key='drive-retire')
        preview = client.drive_retire(retire)
        assert isinstance(preview, Preview)
        retire.confirm = preview.token
        self.assertIsInstance(client.drive_retire(retire), Done)
        self.assertIs(SqliteDriveStore(self.registry).status(drive), DriveStatus.RETIRED)
        self.assertEqual(self.registry.activity(project.project.id)[0].actor, self.actor)

    def test_delivery_api_uses_configured_spec_and_authenticated_actor(self) -> None:
        from t6.notion.testing import FakeNotion, package_spec_properties
        fake = FakeNotion()
        fake.add_data_source('specs')
        fake.add_page('specs', package_spec_properties(package_kind='delivery', version='1', advisory_items=('Reference',)))
        self.api.open_notion = lambda _: fake.client()
        self.api.data_sources['package_specs'] = 'specs'
        project = self.registry.register(actor='setup', idempotency_key='delivery-setup')
        assert isinstance(project, Registered)
        with self.registry._transaction() as db:
            db.execute('INSERT INTO deliverables VALUES (?, ?, ?, NULL, NULL, NULL)', ('delivery', project.project.id, 'FTR'))
        body = {'deliverable': 'delivery', 'carrier': 'frameio-share', 'ref': 'share-1', 'idempotency_key': 'deliver'}
        with patch.dict(os.environ, {'T6_NOTION_TOKEN': 'fake'}):
            self.assertEqual(self.request('/v1/deliverable/deliver', body, secret='staff-secret')[1]['outcome'], 'refused')
            preview = self.request('/v1/deliverable/deliver', body)[1]
            self.assertEqual(preview['outcome'], 'preview')
            done = self.request('/v1/deliverable/deliver', {**body, 'confirm': preview['token']})[1]
            self.assertEqual(done['outcome'], 'done')
        from t6.package import PackageStore
        package = PackageStore(self.registry, PreviewTokens('signing-key')).get(done['result']['package_id'])
        assert package is not None
        self.assertEqual((package['from_person'], package['recipient'], package['ref']), (self.actor, 'client', 'share-1'))

    def test_permission_writes_require_configured_signing_key(self) -> None:
        with patch.dict(os.environ, {'T6_PREVIEW_SIGNING_KEY': ''}):
            result = self.request('/v1/person/grant', {'person_id': self.staff, 'permission': 'admin'})[1]
        self.assertEqual((result['outcome'], result['rule']), ('refused', 'config'))

    def test_brief_reply_uses_gateway_replier_identity(self) -> None:
        from t6.concern import ConcernStore
        from t6.verbs import Preview, Done
        project = self.registry.register(actor='setup', idempotency_key='project')
        assert isinstance(project, Registered)
        store = ConcernStore(self.registry, PreviewTokens('signing-key'))
        values = {'project': project.project.id, 'owner': self.actor, 'waiting_on': 'client', 'anchor': 'Waiting', 'as_of': '2026-11-01'}
        preview = store.write('raise', values, actor=self.actor, idempotency_key='raise')
        assert isinstance(preview, Preview)
        raised = store.write('raise', values, actor=self.actor, idempotency_key='raise', confirm=preview.token)
        assert isinstance(raised, Done)
        concern = raised.result['concern_id']
        mapping = {'concern': concern, 'chat': '-100', 'message_id': '12', 'idempotency_key': 'map'}
        preview_body = self.request('/v1/concern/record-message', mapping)[1]
        self.assertEqual(preview_body['outcome'], 'preview')
        self.assertEqual(self.request('/v1/concern/record-message', {**mapping, 'confirm': preview_body['token']})[1]['outcome'], 'done')
        self.assertEqual(self.request('/v1/concern/for-message', {'chat': '-100', 'message_id': '12'}, secret='gateway-secret')[1]['result']['concern_ids'], [concern])
        reply = {'concern': concern, 'text': 'Update', 'from_reply': '-100:12', 'message_id': '13', 'idempotency_key': 'reply'}
        self.assertEqual(self.request('/v1/concern/note', reply, secret='gateway-secret')[1]['rule'], 'person')
        headers = {'X-Telegram-User-Id': '43'}
        preview_body = self.request('/v1/concern/note', reply, secret='gateway-secret', headers=headers)[1]
        self.assertEqual(preview_body['outcome'], 'preview')
        self.assertEqual(self.request('/v1/concern/note', {**reply, 'confirm': preview_body['token']}, secret='gateway-secret', headers=headers)[1]['outcome'], 'done')
        record = store.get(concern)
        assert record is not None
        self.assertEqual((record['notes'][0]['actor'], record['notes'][0]['message_id']), (self.staff, '13'))

    def test_offline_write_generates_retry_key(self) -> None:
        from t6.client import Client, Unavailable
        from t6.verbs import ProjectHoldInput
        result = Client(base_url='http://127.0.0.1:1', secret='x', timeout=0.1).project_hold(
            ProjectHoldInput(project='show', reason='pause'))
        assert isinstance(result, Unavailable)
        self.assertTrue(result.idempotency_key)

    def test_project_hold_preview_confirm(self) -> None:
        project = self.registry.register(code='ABC', actor='setup', idempotency_key='setup')
        assert isinstance(project, Registered)
        project_id = project.project.id
        data = {'project': project_id, 'reason': 'pause'}
        preview = self.request('/v1/project/hold', data)[1]
        self.assertEqual(preview['outcome'], 'preview')
        result = self.request('/v1/project/hold', {**data, 'confirm': preview['token'], 'idempotency_key': preview['plan']['idempotency_key']})[1]
        self.assertEqual(result['outcome'], 'done')
        self.assertEqual(result['result']['project_id'], project_id)

    def test_service_permissions_and_gateway_delegation(self) -> None:
        project = self.registry.register(code='SERVICE', actor='setup', idempotency_key='service-setup')
        assert isinstance(project, Registered)
        project_id = project.project.id
        data = {'project': project_id, 'reason': 'pause'}
        read = self.request('/v1/project/activity', {'project': project_id}, secret='reports-secret')[1]
        self.assertEqual(read['outcome'], 'done')
        denied = self.request('/v1/project/hold', data, secret='reports-secret')[1]
        self.assertEqual((denied['outcome'], denied['rule']), ('refused', 'permission'))
        preview = self.request('/v1/project/hold', data, secret='worker-secret')[1]
        self.assertEqual(preview['outcome'], 'preview')
        confirmed = self.request('/v1/project/hold', {**data, 'confirm': preview['token'],
                                 'idempotency_key': preview['plan']['idempotency_key']}, secret='worker-secret')[1]
        self.assertEqual(confirmed['outcome'], 'done')
        self.assertEqual(self.registry.activity(project_id)[0].actor, 'worker')
        headers = {'X-Telegram-User-Id': '42'}
        resume = self.request('/v1/project/resume', {'project': project_id}, secret='gateway-secret', headers=headers)[1]
        self.assertEqual(resume['outcome'], 'preview')
        result = self.request('/v1/project/resume', {'project': project_id, 'confirm': resume['token'],
                              'idempotency_key': resume['plan']['idempotency_key']},
                              secret='gateway-secret', headers=headers)[1]
        self.assertEqual(result['outcome'], 'done')
        self.assertEqual(self.registry.activity(project_id)[0].actor, self.actor)
        denied = self.request('/v1/project/hold', data, secret='gateway-secret',
                              headers={'X-Telegram-User-Id': '43'})[1]
        self.assertEqual((denied['outcome'], denied['rule']), ('refused', 'permission'))

    def test_job_order_raise_preview_confirm_and_permissions(self) -> None:
        from contextlib import nullcontext

        project = self.registry.register(actor='setup', idempotency_key='jo-setup')
        assert isinstance(project, Registered)
        with self.registry._transaction() as db:
            db.execute("INSERT INTO person_permissions VALUES (?, 'coordinator')", (self.actor,))
        self.api.open_notion = lambda _: nullcontext(None)
        data = {'file_contents': '{}', 'data_sources_contents': '{}'}
        for verb, inputs in (
            ('raise', data), ('assign', {'jo': 'x', 'person': 'x'}),
            ('reassign', {'jo': 'x', 'person': 'x', 'reason': 'r'}),
            ('note', {'jo': 'x', 'text': 'note'}),
        ):
            denied = self.request('/v1/jo/' + verb, inputs, secret='staff-secret')[1]
            self.assertEqual((denied['outcome'], denied['rule']), ('refused', 'permission'))
        with patch.dict(os.environ, {'T6_NOTION_TOKEN': 'secret'}), \
             patch('t6.cli._read_data_sources', return_value={'job_orders': 'jo'}), \
             patch('t6.cli_jo.prepare_fields', return_value={'project_id': project.project.id}):
            preview = self.request('/v1/jo/raise', data)[1]
            self.assertEqual(preview['outcome'], 'preview')
            self.assertRegex(preview['plan']['idempotency_key'], r'^[0-9a-f]+$')
            result = self.request('/v1/jo/raise', {**data, 'confirm': preview['token'],
                                  'idempotency_key': preview['plan']['idempotency_key']})[1]
        self.assertEqual(result['outcome'], 'done')
        self.assertEqual(self.registry.audit(result['result']['jo_id'])[0].actor, self.actor)

    def test_service_grant_configuration_validation(self) -> None:
        self.assertEqual(service_permissions_from_env({'T6_API_SERVICE_PERMISSIONS': '{"worker": ["operator"]}'}),
                         {'worker': frozenset({'operator'})})
        for value in ('{"unknown": ["operator"]}', '{"worker": ["bogus"]}', '{"worker": "operator"}'):
            with self.assertRaises(ValueError):
                service_permissions_from_env({'T6_API_SERVICE_PERMISSIONS': value})

    def test_gateway_taps_identity_rules_and_audit(self) -> None:
        people = self.api.people
        store = ActionStore(self.tmp.name + '/actions')
        operator = OperatorService(self.registry, store, [42], people=people)
        app = WebhookApp(self.queue, ServerConfig(operator_ids=(42,)), registry=self.registry, operator=operator)
        api = VerbAPI(people, self.queue, {'gateway-secret': 'gateway', 'other-secret': 'worker'}, lambda _: None, app)
        action, _ = operator.offer_unbound_page('page', 'Show')
        operator.delivered(action.token, 1, 123)

        def tap(who: int, secret: str = 'gateway-secret', key: str = 'tap-1', header: str | None = None) -> Any:
            body = {'data': f'op:{action.token}:skip', 'from': {'id': who}, 'chat': 42, 'message_id': 123}
            headers = {'Authorization': 'Bearer ' + secret, 'X-Telegram-User-Id': header or str(who), 'Idempotency-Key': key}
            return api(Request('POST', '/tap', headers, json.dumps(body).encode()))

        self.assertEqual(tap(42, 'other-secret').status, 403)
        self.assertEqual(tap(42, header='43').status, 400)
        self.assertEqual(tap(99).body['outcome'], 'rejected')
        self.assertEqual(store.rejections()[-1][0], 'no_person')
        self.assertEqual(tap(43).body['outcome'], 'rejected')
        self.assertEqual(tap(42).body['outcome'], 'skipped')
        self.assertEqual(tap(42).body['outcome'], 'already_handled')
        with self.registry._transaction() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit WHERE action = 'tap_accepted' AND actor = ?", (self.actor,)).fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM activity WHERE action = 'tap_accepted' AND actor = ?", (self.actor,)).fetchone()[0], 1)

        headers = {'Authorization': 'Bearer gateway-secret', 'X-Telegram-User-Id': '42', 'Idempotency-Key': 'generic'}
        generic = Request('POST', '/tap', headers, json.dumps({'data': 'approve:page', 'from': {'id': 42}}).encode())
        self.assertTrue(api(generic).body['created'])
        self.assertFalse(api(generic).body['created'])
        with self.registry._transaction() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit WHERE action='tap_accepted' AND json_extract(after, '$.key')='generic'").fetchone()[0], 0)
        denied = Request('POST', '/tap', {**headers, 'X-Telegram-User-Id': '43', 'Idempotency-Key': 'denied'}, json.dumps({'data': 'approve:page', 'from': {'id': 43}}).encode())
        self.assertEqual(api(denied).body['outcome'], 'rejected')
        headers['Idempotency-Key'] = 'command'
        command = Request('POST', '/tap', headers, json.dumps({'kind': 'command', 'command': 'project',
                          'from': {'id': 42}, 'chat': 42}).encode())
        self.assertEqual(api(command).body['outcome'], 'opened')
        self.assertEqual(api(command).body['outcome'], 'already_handled')

class ClientTests(VerbAPITests):
    def test_client_registry_and_retry(self) -> None:
        from t6.client import Client, Unavailable
        from t6.verbs import ProjectHoldInput, ActivityInput, Preview, Done, VERBS
        client = Client('http://127.0.0.1:%s' % self.server.server_port, 'operator-secret')
        for entry in VERBS:
            self.assertTrue(callable(getattr(client, (entry.noun + ('_' + entry.verb if entry.verb else '')).replace('-', '_').replace(' ', '_'))))
        project = self.registry.register(code='RETRY', actor='setup', idempotency_key='setup-retry')
        assert isinstance(project, Registered)
        inputs = ProjectHoldInput(project=project.project.id, reason='pause', idempotency_key='retry-key')
        offline = Client('http://127.0.0.1:1', 'operator-secret', timeout=1)
        self.assertIsInstance(offline.project_activity(ActivityInput(project=project.project.id)), Unavailable)
        failed = offline.project_hold(inputs)
        assert isinstance(failed, Unavailable)
        self.assertEqual(failed.idempotency_key, 'retry-key')
        preview = client.project_hold(inputs)
        assert isinstance(preview, Preview)
        inputs.confirm = preview.token
        self.assertIsInstance(client.project_hold(inputs), Done)
        inputs.confirm = None
        replay = client.project_hold(inputs)
        assert isinstance(replay, Preview)
        inputs.confirm = replay.token
        self.assertIsInstance(client.project_hold(inputs), Done)
        self.assertEqual(len([a for a in self.registry.activity(project.project.id) if a.action == 'project_hold']), 1)

    def test_client_job_order_raise_preview_confirm(self) -> None:
        from contextlib import nullcontext
        from pathlib import Path
        from t6.client import Client
        from t6.verbs import Done, JoRaiseInput, Preview

        project = self.registry.register(actor='setup', idempotency_key='client-jo')
        assert isinstance(project, Registered)
        with self.registry._transaction() as db:
            db.execute("INSERT INTO person_permissions VALUES (?, 'coordinator')", (self.actor,))
        self.api.open_notion = lambda _: nullcontext(None)
        draft = Path(self.tmp.name) / 'jo.json'
        sources = Path(self.tmp.name) / 'sources.json'
        draft.write_text('{}')
        sources.write_text('{}')
        inputs = JoRaiseInput(file=draft, data_sources=sources)
        client = Client('http://127.0.0.1:%s' % self.server.server_port, 'operator-secret')
        with patch.dict(os.environ, {'T6_NOTION_TOKEN': 'secret'}), \
             patch('t6.cli._read_data_sources', return_value={'job_orders': 'jo'}), \
             patch('t6.cli_jo.prepare_fields', return_value={'project_id': project.project.id}):
            preview = client.jo_raise(inputs)
            assert isinstance(preview, Preview)
            inputs.idempotency_key = preview.plan['idempotency_key']
            inputs.confirm = preview.token
            self.assertIsInstance(client.jo_raise(inputs), Done)

    def test_client_tap(self) -> None:
        from t6.client import Client
        self.api.tap_app = WebhookApp(self.queue, ServerConfig(), registry=self.registry)
        client = Client('http://127.0.0.1:%s' % self.server.server_port,
                        'gateway-secret', telegram_user_id=42)
        payload = {'data': 'approve:page', 'from': {'id': 42}}
        first = client.tap(payload, idempotency_key='client-tap')
        second = client.tap(payload, idempotency_key='client-tap')
        assert isinstance(first, dict) and isinstance(second, dict)
        self.assertTrue(first['created'])
        self.assertFalse(second['created'])

    def test_file_contents_and_missing_configuration(self) -> None:
        from pathlib import Path
        from t6.client import Client, Unavailable
        from t6.verbs import ActivityInput, JoFormatInput, Done
        with patch.dict(os.environ, {}, clear=True):
            result = Client().project_activity(ActivityInput(project='x'))
            assert isinstance(result, Unavailable)
            self.assertIn('T6_API_URL', result.reason)
            self.assertIn('T6_API_SECRET', result.reason)
        path = Path(self.tmp.name) / 'draft.txt'
        path.write_text('SHOW - TOC - JO #1\nTitle: Test', encoding='utf-8')
        client = Client('http://127.0.0.1:%s' % self.server.server_port, 'operator-secret')
        result = client.jo_format(JoFormatInput(file=path))
        self.assertIsInstance(result, Done)
        status, _ = self.request('/v1/jo/format', {'file': str(path)})
        self.assertEqual(status, 422)

    def test_cli_and_health_use_http(self) -> None:
        import io
        from contextlib import redirect_stdout
        from t6.cli import main
        project = self.registry.register(code='CLI', actor='setup', idempotency_key='setup-cli')
        assert isinstance(project, Registered)
        env = {'T6_API_URL': 'http://127.0.0.1:%s' % self.server.server_port, 'T6_API_SECRET': 'operator-secret'}
        with patch.dict(os.environ, env), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(['project', 'hold', project.project.id, '--reason', 'pause']), 0)
        preview = json.loads(out.getvalue())
        self.assertEqual(preview['outcome'], 'preview')
        with patch.dict(os.environ, env), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(['project', 'hold', project.project.id, '--reason', 'pause',
                                   '--idempotency-key', preview['plan']['idempotency_key'],
                                   '--confirm', preview['token']]), 0)
        self.assertEqual(json.loads(out.getvalue())['outcome'], 'done')
        with patch.dict(os.environ, env), redirect_stdout(io.StringIO()) as out:
            main(['health'])
        self.assertIsInstance(json.loads(out.getvalue())['api']['schema_version'], int)

    def test_frameio_project_verbs_are_staff_write_verbs_and_refuse_clients_and_vendors(self) -> None:
        from t6.client import Client
        from t6.verbs import Done, FrameioProjectInput, Preview, Refused
        entries = {v['verb']: v for v in catalogue() if v['noun'] == 'frameio'}
        self.assertEqual(set(entries), {'project link', 'project unlink'})
        for entry in entries.values():
            self.assertEqual((entry['role'], entry['read_only']), ('staff', False))
        client_person, vendor = 't6n_' + '3' * 32, 't6n_' + '4' * 32
        with self.registry._transaction() as db:
            for person, kind, telegram in ((client_person, 'client', 44), (vendor, 'vendor', 45)):
                db.execute("INSERT INTO persons VALUES (?, ?, '[]', '[]', ?, 0, 1)", (person, kind, telegram))
        self.api.secrets.update({'client-secret': client_person, 'vendor-secret': vendor})
        project = self.registry.register(actor='setup', idempotency_key='fio-project')
        assert isinstance(project, Registered)
        body = {'project': project.project.id, 'frameio_project': 'fp1', 'idempotency_key': 'fio-link'}
        self.assertEqual(self.request('/v1/frameio/project/link', body, secret='worker-secret')[1]['rule'], 'permission')
        for secret in ('client-secret', 'vendor-secret'):
            self.assertEqual(self.request('/v1/frameio/project/link', body, secret=secret)[1]['outcome'], 'refused')
        staff = Client('http://127.0.0.1:%s' % self.server.server_port, 'staff-secret')
        inputs = FrameioProjectInput(project=project.project.id, frameio_project='fp1', idempotency_key='fio-link')
        preview = staff.frameio_project_link(inputs)
        assert isinstance(preview, Preview)
        inputs.confirm = preview.token
        self.assertIsInstance(staff.frameio_project_link(inputs), Done)
        other = FrameioProjectInput(project=project.project.id, frameio_project='fp1', idempotency_key='fio-unlink')
        preview = staff.frameio_project_unlink(other)
        assert isinstance(preview, Preview)
        other.confirm = preview.token
        self.assertIsInstance(staff.frameio_project_unlink(other), Done)
