"""Read-only MCP tools follow the command catalogue."""
import unittest

from t6.mcp import MCPServer


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
        expected = {f"{v['noun']} {v['verb']}".strip() for v in commands if v['role'] == 'reader'}
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
        from t6.verbs import Done
        from unittest.mock import patch

        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        registry = SqliteProjectRegistry(tmp.name + '/registry')
        operator = 't6n_' + '1' * 32
        with registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES (?, 'staff', '[]', '[]', NULL, 0, 1)", (operator,))
            db.execute("INSERT INTO person_permissions VALUES (?, 'operator')", (operator,))
        api = VerbAPI(People(registry, PreviewTokens('test')), None,
                      {'reader-secret': 'mcp', 'operator-secret': operator}, lambda _: None,
                      privacy_registry=Registry(people=[Person(name='Client Corp')]))
        with patch('t6.server.verbs.invoke', return_value=Done(result={'locations': [{'label': 'Client Corp'}]})):
            def call(secret: str) -> str:
                response = api(Request('POST', '/v1/media/list', {'Authorization': 'Bearer ' + secret, 'X-T6-MCP': '1'}, json.dumps({'owner': 't6p_test'}).encode()))
                return json.dumps(response.body)
            self.assertNotIn('Client Corp', call('reader-secret'))
            self.assertIn('Client Corp', call('operator-secret'))

    def test_jo_format_file_path_returns_typed_refusal(self) -> None:
        import json

        request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                   'params': {'name': 'jo__format', 'arguments': {'file': '/example/input.txt'}}}
        reply = MCPServer(None).handle(request)
        assert reply is not None
        outcome = json.loads(reply['result']['content'][0]['text'])
        self.assertEqual(outcome['outcome'], 'refused')
        self.assertEqual(outcome['rule'], 'no local file reads over MCP')

    def test_protocol_calls_only_listed_tools(self) -> None:
        request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                   'params': {'name': 'person__grant', 'arguments': {}}}
        reply = MCPServer(None).handle(request)
        assert reply is not None
        self.assertEqual(reply['error']['code'], -32602)

    def test_missing_privacy_registry_refuses_mcp_read(self) -> None:
        import json
        import tempfile
        from unittest.mock import patch
        from t6.person import People
        from t6.preview import PreviewTokens
        from t6.registry.sqlite import SqliteProjectRegistry
        from t6.server.app import Request
        from t6.server.verbs import VerbAPI
        from t6.verbs import Done

        with tempfile.TemporaryDirectory() as directory:
            api = VerbAPI(People(SqliteProjectRegistry(directory + '/db'), PreviewTokens('test')),
                          None, {'secret': 'mcp'}, lambda _: None)
            with patch('t6.server.verbs.invoke', return_value=Done(result={'locations': []})):
                response = api(Request('POST', '/v1/media/list', {'Authorization': 'Bearer secret', 'X-T6-MCP': '1'}, json.dumps({'owner': 't6p_test'}).encode()))
            self.assertEqual(response.body['rule'], 'masking')
