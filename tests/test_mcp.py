"""Read-only MCP tools follow the command catalogue."""
import unittest

from media_seed import link_media
from t6.mcp import MCPServer
from t6.registry import Registered
from t6.registry.sqlite import SqliteProjectRegistry


def seeded_project(registry: SqliteProjectRegistry) -> str:
    """A Project whose NAS Media location (linked through `media link`) and one Activity entry name a client."""
    project = registry.register(actor='setup', idempotency_key='project')
    assert isinstance(project, Registered)
    link_media(registry, project.project.id, 'Client Corp')
    with registry._transaction() as db:
        audit = db.execute("INSERT INTO audit (project_id, action, actor, at, before, after) VALUES (?, 'note', 'setup', 5, NULL, '{}')", (project.project.id,))
        db.execute("INSERT INTO activity (project_id, subject_id, at, actor, action, summary, audit_id) VALUES (?, ?, 5, 'setup', 'note', 'Client Corp visit', ?)", (project.project.id, project.project.id, audit.lastrowid))
    return str(project.project.id)


class MCPTests(unittest.TestCase):
    def test_tools_match_catalogue_reads_and_exclude_writes(self) -> None:
        server = MCPServer(None)
        tools = server.tools()
        import io
        import json
        from contextlib import redirect_stdout
        from t6.cli import main

        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(['commands', '--json']), 0)
        commands = json.loads(output.getvalue())
        expected = {f"{v['noun']} {v['verb']}".strip() for v in commands if v['read_only']}
        self.assertEqual({t['name'].replace('__', ' ') for t in tools}, expected)
        self.assertNotIn('person__grant', {t['name'] for t in tools})

    def test_mcp_api_masks_all_read_outcomes_except_for_operator(self) -> None:
        import json
        from t6.person import People
        from t6.preview import PreviewTokens
        from t6.privacy import Person, Registry
        from t6.registry.sqlite import SqliteProjectRegistry
        from t6.server.app import Request
        from t6.server.verbs import VerbAPI

        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        registry = SqliteProjectRegistry(tmp.name + '/registry')
        project = seeded_project(registry)
        operator = 't6n_' + '1' * 32
        with registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES (?, 'staff', '[]', '[]', NULL, 0, 1)", (operator,))
            db.execute("INSERT INTO person_permissions VALUES (?, 'operator')", (operator,))
        api = VerbAPI(People(registry, PreviewTokens('test')), None,
                      {'reader-secret': 'mcp', 'operator-secret': operator}, lambda _: None,
                      privacy_registry=Registry(people=[Person(name='Client Corp')]))
        def call(secret: str) -> str:
            response = api(Request('POST', '/v1/media/list', {'Authorization': 'Bearer ' + secret, 'X-T6-MCP': '1'}, json.dumps({'owner': project}).encode()))
            return json.dumps(response.body)
        self.assertNotIn('Client Corp', call('reader-secret'))
        self.assertIn('Client Corp', call('operator-secret'))

    TOOL_NAMES = [
        'booking__get', 'booking__list', 'camera__get', 'concern__changes', 'concern__counts', 'concern__for-message', 'concern__get', 'concern__list', 'deliverable__bangko',
        'deliverable__candidates', 'deliverable__get', 'deliverable__hand-to-mouth', 'deliverable__list', 'deliverable__sabit', 'departments',
        'drive__exceptions', 'drive__find', 'drive__get', 'drive__list', 'drive__shipment', 'drive__shipments', 'external-link__find', 'jo__find', 'jo__get', 'jo__list', 'machine__list', 'media__list', 'meeting-notes__list', 'package__get',
        'package__list', 'person__find', 'person__get', 'person__list', 'project__activity', 'project__flags',
        'project__get', 'project__list', 'project__notes', 'project__resolve', 'queue__failed', 'standing-cc', 'studio__notices', 'swing__overdue', 'text__withhold', 'timeline__list', 'transcript__get',
        'version__find', 'version__get']

    def test_tool_list_is_the_written_list_of_read_tools_and_has_no_writes(self) -> None:
        tools = MCPServer(None).tools()
        self.assertEqual(sorted(t['name'] for t in tools), self.TOOL_NAMES)
        for tool in tools:
            self.assertNotIn('confirm', tool['inputSchema'].get('properties', {}), tool['name'])
            self.assertNotIn('idempotency_key', tool['inputSchema'].get('properties', {}), tool['name'])
        names = {t['name'] for t in tools}
        for write in ('swing__assign', 'swing__unassign', 'swing__return', 'drive__add', 'drive__edit', 'drive__promote-serial', 'transcript__create', 'transcript__speakers', 'transcript__translate', 'jo__format'):
            self.assertNotIn(write, names)

    def test_project_activity_read_is_masked_for_the_mcp_service_and_a_non_operator_person(self) -> None:
        import json
        import tempfile
        from t6.person import People
        from t6.preview import PreviewTokens
        from t6.privacy import Person, Registry
        from t6.server.app import Request
        from t6.server.verbs import VerbAPI
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        registry = SqliteProjectRegistry(tmp.name + '/registry')
        project = seeded_project(registry)
        operator, staff = ('t6n_' + c * 32 for c in '15')
        with registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES (?, 'staff', '[]', '[]', NULL, 0, 1)", (operator,))
            db.execute("INSERT INTO person_permissions VALUES (?, 'operator')", (operator,))
            db.execute("INSERT INTO persons VALUES (?, 'staff', '[]', '[]', NULL, 0, 1)", (staff,))
        api = VerbAPI(People(registry, PreviewTokens('test')), None,
                      {'mcp-secret': 'mcp', 'operator-secret': operator, 'staff-secret': staff}, lambda _: None,
                      privacy_registry=Registry(people=[Person(name='Client Corp')]))
        for path, body in (('/v1/project/activity', {'project': project}),):
            with self.subTest(path):
                def call(secret: str, header: bool, path: str = path, body: dict[str, str] = body) -> str:
                    headers = {'Authorization': 'Bearer ' + secret, **({'X-T6-MCP': '1'} if header else {})}
                    return json.dumps(api(Request('POST', path, headers, json.dumps(body).encode())).body)
                self.assertNotIn('Client Corp', call('mcp-secret', True))
                self.assertNotIn('Client Corp', call('mcp-secret', False))
                self.assertNotIn('Client Corp', call('staff-secret', True))
                self.assertIn('Client Corp', call('staff-secret', False))
                self.assertIn('Client Corp', call('operator-secret', True))
                with registry._transaction() as db:
                    db.execute("INSERT OR IGNORE INTO person_permissions VALUES (?, 'unmasked')", (staff,))
                self.assertIn('Client Corp', call('staff-secret', True))
                with registry._transaction() as db:
                    db.execute("DELETE FROM person_permissions WHERE person_id = ?", (staff,))

    def test_every_reader_verb_is_read_only_except_the_written_exception(self) -> None:
        from t6.verbs import VERBS
        written_exception = {('jo', 'format')}  # formats a draft and writes nothing, but is a local helper with no API read
        unmarked = {(v.noun, v.verb) for v in VERBS if v.role == 'reader' and not v.read_only}
        self.assertEqual(unmarked, written_exception)

    def test_a_reader_role_alone_does_not_make_a_tool(self) -> None:
        from dataclasses import replace
        from unittest.mock import patch
        from t6 import mcp
        from t6.verbs import VERBS
        unmarked = tuple(replace(v, read_only=False) if (v.noun, v.verb) == ('media', 'list') else v for v in VERBS)
        with patch.object(mcp, 'VERBS', unmarked):
            self.assertNotIn('media__list', {t['name'] for t in MCPServer(None).tools()})

    def test_transcript_write_verbs_use_a_staff_role_not_reader(self) -> None:
        from t6.verbs import VERBS
        for verb in ('create', 'speakers', 'translate'):
            entry = next(v for v in VERBS if (v.noun, v.verb) == ('transcript', verb))
            self.assertEqual(entry.role, 'staff')
            self.assertFalse(entry.read_only)

    def test_staff_role_needs_an_active_staff_person(self) -> None:
        import tempfile
        from t6.person import People
        from t6.preview import PreviewTokens
        from t6.registry.sqlite import SqliteProjectRegistry
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        registry = SqliteProjectRegistry(tmp.name + '/registry')
        staff, client, gone = ('t6n_' + c * 32 for c in '234')
        with registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES (?, 'staff', '[]', '[]', NULL, 0, 1)", (staff,))
            db.execute("INSERT INTO persons VALUES (?, 'client', '[]', '[]', NULL, 0, 1)", (client,))
            db.execute("INSERT INTO persons VALUES (?, 'staff', '[]', '[]', NULL, 0, 0)", (gone,))
        people = People(registry, PreviewTokens('test'))
        self.assertIsNone(people.authorize(staff, 'staff'))
        for actor in (client, gone, 'mcp', 'worker'):
            self.assertIsNotNone(people.authorize(actor, 'staff'), actor)

    def test_mcp_service_is_masked_without_the_mcp_header(self) -> None:
        import json
        import tempfile
        from t6.person import People
        from t6.preview import PreviewTokens
        from t6.privacy import Person, Registry
        from t6.server.app import Request
        from t6.server.verbs import VerbAPI
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        registry = SqliteProjectRegistry(tmp.name + '/registry')
        project = seeded_project(registry)
        operator = 't6n_' + '1' * 32
        with registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES (?, 'staff', '[]', '[]', NULL, 0, 1)", (operator,))
            db.execute("INSERT INTO person_permissions VALUES (?, 'operator')", (operator,))
        api = VerbAPI(People(registry, PreviewTokens('test'), service_permissions={'mcp': frozenset({'operator'})}), None,
                      {'mcp-secret': 'mcp', 'operator-secret': operator}, lambda _: None,
                      privacy_registry=Registry(people=[Person(name='Client Corp')]))
        def call(secret: str, headers: dict[str, str]) -> str:
            response = api(Request('POST', '/v1/media/list', {'Authorization': 'Bearer ' + secret, **headers}, json.dumps({'owner': project}).encode()))
            return json.dumps(response.body)
        self.assertNotIn('Client Corp', call('mcp-secret', {}))
        self.assertNotIn('Client Corp', call('mcp-secret', {'X-T6-MCP': '1'}))
        self.assertIn('Client Corp', call('operator-secret', {}))
        self.assertIn('Client Corp', call('operator-secret', {'X-T6-MCP': '1'}))

    def test_server_reports_the_package_version(self) -> None:
        from unittest.mock import patch
        with patch('t6.mcp.version', return_value='9.9.9'):
            reply = MCPServer(None).handle({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'})
        assert reply is not None
        self.assertEqual(reply['result']['serverInfo']['version'], '9.9.9')

    def test_protocol_calls_only_listed_tools(self) -> None:
        request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                   'params': {'name': 'person__grant', 'arguments': {}}}
        reply = MCPServer(None).handle(request)
        assert reply is not None
        self.assertEqual(reply['error']['code'], -32602)

    def test_missing_privacy_registry_refuses_mcp_read(self) -> None:
        import json
        import tempfile
        from t6.person import People
        from t6.preview import PreviewTokens
        from t6.server.app import Request
        from t6.server.verbs import VerbAPI

        with tempfile.TemporaryDirectory() as directory:
            registry = SqliteProjectRegistry(directory + '/db')
            project = seeded_project(registry)
            api = VerbAPI(People(registry, PreviewTokens('test')), None, {'secret': 'mcp'}, lambda _: None)
            response = api(Request('POST', '/v1/media/list', {'Authorization': 'Bearer secret', 'X-T6-MCP': '1'}, json.dumps({'owner': project}).encode()))
            self.assertEqual(response.body['rule'], 'masking')


class MCPStartTests(unittest.TestCase):
    """`t6 mcp` and `python -m t6.mcp` are one server, and its API timeout is a setting."""
    REQUESTS = ('{"jsonrpc": "2.0", "id": 1, "method": "initialize"}\n'
                '{"jsonrpc": "2.0", "id": 2, "method": "tools/list"}\n')

    def serve(self, delay: float) -> str:
        """A fake t6 API that answers every POST with a Refused outcome after `delay` seconds."""
        import json
        import threading
        import time
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                self.rfile.read(int(self.headers.get('Content-Length', 0)))
                time.sleep(delay)
                body = json.dumps({'outcome': 'Refused', 'rule': 'fake', 'reason': 'fake'}).encode()
                try:
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except BrokenPipeError:  # the client gave up first
                    pass

            def log_message(self, *args: object) -> None:
                pass

        server = HTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f'http://127.0.0.1:{server.server_port}'

    def run_server(self, entry: str, env: dict[str, str]) -> str:
        import io
        import os
        from contextlib import redirect_stdout
        from unittest.mock import patch
        from t6 import mcp
        from t6.cli import main
        output = io.StringIO()
        with patch.dict(os.environ, env), patch('sys.stdin', io.StringIO(self.REQUESTS)), redirect_stdout(output):
            if entry == 'cli':
                self.assertEqual(main(['mcp']), 0)
            else:
                mcp.main()
        return output.getvalue()

    def test_t6_mcp_and_the_module_answer_initialize_and_tools_list_the_same_way(self) -> None:
        env = {'T6_API_URL': self.serve(0), 'T6_API_SECRET': 'made-up'}
        by_cli, by_module = self.run_server('cli', env), self.run_server('module', env)
        self.assertEqual(by_cli, by_module)
        replies = [__import__('json').loads(line) for line in by_cli.splitlines()]
        self.assertEqual([r['id'] for r in replies], [1, 2])
        self.assertEqual(replies[0]['result']['serverInfo']['name'], 't6')
        self.assertTrue(replies[1]['result']['tools'])

    def test_mcp_is_in_the_help_and_not_in_the_command_catalogue(self) -> None:
        import io
        import json
        from contextlib import redirect_stdout
        from t6.cli import main
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit):
            main(['--help'])
        self.assertRegex(output.getvalue(), r'\n\s+mcp\s')
        output = io.StringIO()
        with redirect_stdout(output):
            main(['commands', '--json'])
        self.assertNotIn('mcp', {v['noun'] for v in json.loads(output.getvalue())})

    def started_timeout(self, env: dict[str, str]) -> float:
        import io
        import os
        from unittest.mock import patch
        from t6 import mcp
        with patch.dict(os.environ, env), patch('sys.stdin', io.StringIO('')), patch('t6.mcp.Client') as client:
            mcp.main()
        return float(client.call_args.kwargs['timeout'])

    def test_timeout_is_60_unset_and_5_when_set(self) -> None:
        import os
        from unittest.mock import patch
        with patch.dict(os.environ):
            os.environ.pop('T6_MCP_TIMEOUT_SECONDS', None)
            self.assertEqual(self.started_timeout({}), 60)
        self.assertEqual(self.started_timeout({'T6_MCP_TIMEOUT_SECONDS': '5'}), 5)

    def test_a_timeout_that_is_not_a_positive_number_stops_the_start_naming_the_setting(self) -> None:
        for value in ('abc', '0', '-3', 'nan', ''):
            with self.subTest(value):
                with self.assertRaises(SystemExit) as stop:
                    self.started_timeout({'T6_MCP_TIMEOUT_SECONDS': value})
                self.assertIn('T6_MCP_TIMEOUT_SECONDS', str(stop.exception))

    def test_other_callers_of_client_keep_10_seconds(self) -> None:
        from t6.client import Client
        self.assertEqual(Client().timeout, 10)

    def test_a_timed_out_call_is_unavailable_and_names_the_setting(self) -> None:
        from t6.client import Client
        client = Client(self.serve(1), 'made-up', timeout=0.2)
        reply = MCPServer(client).handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                          'params': {'name': 'project__list', 'arguments': {}}})
        assert reply is not None
        value = __import__('json').loads(reply['result']['content'][0]['text'])
        self.assertEqual(value['outcome'], 'Unavailable')
        self.assertIn('T6_MCP_TIMEOUT_SECONDS', value['reason'])
