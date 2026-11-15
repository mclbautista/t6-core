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
        'booking__get', 'booking__list', 'camera__get', 'concern__for-message', 'concern__get', 'deliverable__bangko',
        'deliverable__get', 'deliverable__hand-to-mouth', 'deliverable__list', 'deliverable__sabit', 'departments',
        'external-link__find', 'jo__find', 'jo__get', 'jo__list', 'media__list', 'meeting-notes__list', 'package__get',
        'package__list', 'person__find', 'person__get', 'person__list', 'project__activity', 'project__flags',
        'project__get', 'project__list', 'project__resolve', 'standing-cc', 'studio__notices', 'swing__overdue', 'timeline__list', 'transcript__get',
        'version__find', 'version__get']

    def test_tool_list_is_the_written_list_of_read_tools_and_has_no_writes(self) -> None:
        tools = MCPServer(None).tools()
        self.assertEqual(sorted(t['name'] for t in tools), self.TOOL_NAMES)
        for tool in tools:
            self.assertNotIn('confirm', tool['inputSchema'].get('properties', {}), tool['name'])
            self.assertNotIn('idempotency_key', tool['inputSchema'].get('properties', {}), tool['name'])
        names = {t['name'] for t in tools}
        for write in ('swing__assign', 'swing__unassign', 'swing__return', 'transcript__create', 'transcript__speakers', 'transcript__translate', 'jo__format'):
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
