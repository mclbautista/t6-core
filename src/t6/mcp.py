"""Read-only MCP server over the authenticated t6 API (stdio transport)."""
from __future__ import annotations

import json
import math
import os
import sys
from importlib.metadata import version
from typing import Any

from t6.client import APIError, Client, Result, Unavailable
from t6.verbs import VERBS, Refused


class MCPServer:
    def __init__(self, client: Client | None) -> None:
        self.client = client
        if client is not None:
            client.mcp = True
        self.entries = {self._name(v.noun, v.verb): v for v in VERBS if v.read_only}

    @staticmethod
    def _name(noun: str, verb: str) -> str:
        return '__'.join((noun, verb)).strip('_').replace(' ', '__')

    def tools(self) -> list[dict[str, Any]]:
        return [{'name': name, 'description': v.description,
                 'inputSchema': v.input_model.model_json_schema()} for name, v in self.entries.items()]

    def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        method = request.get('method')
        if 'id' not in request:
            return None
        response: dict[str, Any] = {'jsonrpc': '2.0', 'id': request['id']}
        try:
            if method == 'initialize':
                result: Any = {'protocolVersion': '2024-11-05', 'capabilities': {'tools': {}},
                               'serverInfo': {'name': 't6', 'version': version('t6')}}
            elif method == 'tools/list':
                result = {'tools': self.tools()}
            elif method == 'tools/call':
                params = request.get('params', {})
                entry = self.entries[params['name']]
                arguments = params.get('arguments', {})
                if not isinstance(arguments, dict):
                    raise ValueError('Arguments must be an object')
                outcome: Result
                if any(k in arguments for k in ('file', 'data_sources', 'registry_db')):
                    outcome = Refused(rule='no local file reads over MCP', reason='Local file paths are not accepted')
                else:
                    inputs = entry.input_model.model_validate(arguments)
                    if self.client is None:
                        raise ValueError('API client is not configured')
                    outcome = self.client.call(entry.noun, entry.verb, inputs)
                value = {'outcome': 'Unavailable', 'reason': outcome.reason} if isinstance(outcome, Unavailable) else outcome.model_dump(mode='json')
                result = {'content': [{'type': 'text', 'text': json.dumps(value)}]}
            else:
                response['error'] = {'code': -32601, 'message': 'Method not found'}
                return response
            response['result'] = result
        except (KeyError, ValueError, TypeError, APIError) as error:
            response['error'] = {'code': -32602, 'message': str(error)}
        return response


def _timeout() -> float:
    """Seconds the API client waits, from T6_MCP_TIMEOUT_SECONDS (default 60); anything but a positive number stops the start."""
    text = os.environ.get('T6_MCP_TIMEOUT_SECONDS', '60')
    try:
        seconds = float(text)
    except ValueError:
        seconds = 0.0
    if not (math.isfinite(seconds) and seconds > 0):
        sys.exit(f'T6_MCP_TIMEOUT_SECONDS must be a positive number of seconds, not {text!r}')
    return seconds


def main() -> None:
    server = MCPServer(Client(mcp=True, timeout=_timeout()))
    for line in sys.stdin:
        try:
            request = json.loads(line)
            response = server.handle(request)
        except (ValueError, TypeError):
            response = {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'Parse error'}}
        if response is not None:
            print(json.dumps(response), flush=True)


if __name__ == '__main__':
    main()
