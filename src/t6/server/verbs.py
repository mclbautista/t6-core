"""Authenticated HTTP boundary for the studio verb catalogue."""
from __future__ import annotations

import hmac
import json
import os
import tempfile
from pathlib import Path
from importlib.metadata import version
from typing import Any, Callable, Mapping, cast

from t6.domain.events import JsonValue
from urllib.parse import urlsplit

from pydantic import ValidationError

from t6.person import PERMISSIONS, SERVICES, People
from t6.server.app import Request, Response
from t6.verbs import VERBS, Done, Refused, catalogue, invoke


class VerbAPI:
    def __init__(self, people: People, queue: Any, secrets: Mapping[str, str],
                 open_notion: Callable[..., Any]) -> None:
        if any(not secret or not (identity.startswith('t6n_') or identity in SERVICES)
               for secret, identity in secrets.items()):
            raise ValueError('Each API secret must bind to a Person or named service')
        self.people, self.queue, self.secrets, self.open_notion = people, queue, dict(secrets), open_notion
        self.routes = {f'/v1/{v.noun}' + (f'/{v.verb}' if v.verb else ''): v for v in VERBS}

    def __call__(self, request: Request) -> Response:
        path = urlsplit(request.path).path
        if path == '/health':
            if request.method != 'GET':
                return Response(405)
            with self.people.registry._transaction() as db:
                schema = db.execute('SELECT MAX(version) FROM schema_version').fetchone()[0]
            with self.queue._transaction() as db:
                jobs = dict(db.execute('SELECT state, COUNT(*) FROM jobs GROUP BY state').fetchall())
            return Response(200, {'version': version('t6'), 'schema_version': schema, 'jobs': jobs})
        auth = request.header('authorization')
        supplied = auth[7:] if auth.lower().startswith('bearer ') else ''
        actor = next((identity for secret, identity in self.secrets.items()
                      if supplied and hmac.compare_digest(secret, supplied)), None)
        if actor is None:
            return Response(401, {'error': 'unauthorized'})
        behalf = request.header('x-telegram-user-id')
        if 'x-telegram-user-id' in request.headers_lower():
            if actor != 'gateway':
                return self.refused('identity', 'Only the gateway may act on behalf of a Telegram user')
            try:
                telegram_id = int(behalf)
            except ValueError:
                return self.refused('identity', 'Invalid Telegram user ID')
            with self.people.registry._transaction() as db:
                row = db.execute('SELECT id FROM persons WHERE telegram_id = ? AND active = 1', (telegram_id,)).fetchone()
            if not row:
                return self.refused('identity', 'No active Person for Telegram user ID')
            actor = row[0]
        if path == '/v1/commands':
            return Response(200, cast(Mapping[str, JsonValue], catalogue())) if request.method == 'GET' else Response(405)
        entry = self.routes.get(path)
        if entry is None:
            return Response(404)
        if request.method != 'POST':
            return Response(405)
        try:
            data = json.loads(request.body)
            if not isinstance(data, dict):
                raise ValueError('Expected a JSON object')
        except (ValueError, UnicodeDecodeError):
            return Response(422, {'errors': [{'loc': [], 'msg': 'Expected a JSON object'}]})
        if 'actor' in data:
            return self.refused('identity', 'Actor comes from authentication')
        header_key = request.header('idempotency-key')
        if header_key:
            if data.get('idempotency_key') not in (None, header_key):
                return self.refused('idempotency_key', 'Conflicting idempotency keys')
            data['idempotency_key'] = header_key
        if entry.local_only:
            return self.refused('verb', 'This verb currently runs locally')
        # Materialize only the explicitly supported uploaded text fields in an
        # isolated directory; never open a path supplied by an HTTP caller.
        with tempfile.TemporaryDirectory() as directory:
            for name in ('data_sources', 'file'):
                if name not in entry.input_model.model_fields:
                    continue
                contents = data.pop(name + '_contents', None)
                if not isinstance(contents, str):
                    return Response(422, {'errors': [{'loc': [name], 'msg': 'File contents required'}]})
                uploaded = Path(directory) / name
                uploaded.write_text(contents, encoding='utf-8')
                data[name] = str(uploaded)
            data.pop('registry_db', None)
            return self._dispatch(entry, data, actor)

    def _dispatch(self, entry: Any, data: dict[str, Any], actor: str) -> Response:
        try:
            inputs = entry.input_model.model_validate(data)
        except ValidationError as error:
            return Response(422, {'errors': cast(JsonValue, error.errors(include_url=False, include_context=False, include_input=False))})
        if entry.handler is None:
            refusal = self.people.authorize(actor, entry.role)
            outcome = refusal or self._legacy_read(entry.noun, entry.verb, inputs)
        else:
            outcome = invoke(entry, inputs, actor=actor, people=self.people, open_notion=self.open_notion)
        return Response(200, outcome.model_dump(mode='json'))

    def _legacy_read(self, noun: str, verb: str, inputs: Any) -> Any:
        # The legacy write commands do not have signed previews yet.
        if (noun, verb) in {('jo', 'raise'), ('camera', 'enrich')}:
            return Refused(rule='verb', reason='This legacy write does not support preview and confirm')
        try:
            from t6.cli import TOKEN_VARIABLE, _read_data_sources
            from t6.cli_jo import _format, _get
            from t6.cli_studio import _contacts_find, _projects_resolve
            from datetime import date
            from pathlib import Path
            if (noun, verb) == ('jo', 'format'):
                return Done(result=_format(inputs.file.read_text(encoding='utf-8'), date.fromisoformat(inputs.date) if inputs.date else None))
            token = os.environ.get(TOKEN_VARIABLE)
            if not token:
                return Refused(rule='credentials', reason=f'set {TOKEN_VARIABLE}')
            sources = _read_data_sources(inputs.data_sources)
            result: Any
            with self.open_notion(token) as client:
                if (noun, verb) == ('jo', 'get'):
                    result = _get(client, sources['job_orders'], inputs.ref)
                elif (noun, verb) == ('person', 'find'):
                    result = _contacts_find(client, sources['contacts'], inputs.mention)
                    for item in ([result['match']] if result['match'] else []) + result['candidates']:
                        item['t6_id'] = self.people.registry.bound_entity('notion', item['id'])
                elif (noun, verb) == ('project', 'resolve'):
                    result = _projects_resolve(client, sources['projects'], inputs.text,
                                               inputs.registry_db or Path(os.environ.get('T6_REGISTRY_DB', 't6-registry.sqlite')))
                elif (noun, verb) == ('camera', 'get'):
                    from t6.notion.cameras import get_cameras
                    result = get_cameras(client, sources['cameras'], page_id=inputs.page_id)
                else:
                    return Refused(rule='verb', reason='Verb has no registered handler')
            return Done(result=result)
        except (OSError, ValueError, KeyError, ImportError) as error:
            return Refused(rule='read', reason=str(error))

    @staticmethod
    def refused(rule: str, reason: str) -> Response:
        return Response(200, Refused(rule=rule, reason=reason).model_dump())


def secrets_from_env(environ: Mapping[str, str]) -> dict[str, str]:
    """Read JSON mapping bearer secrets to their identities."""
    value = json.loads(environ.get('T6_API_SECRETS', '{}'))
    if not isinstance(value, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in value.items()):
        raise ValueError('T6_API_SECRETS must be a JSON object mapping secrets to identities')
    return value


def service_permissions_from_env(environ: Mapping[str, str]) -> dict[str, frozenset[str]]:
    """Read service role grants; all named services already have reader access."""
    value = json.loads(environ.get('T6_API_SERVICE_PERMISSIONS', '{}'))
    if not isinstance(value, dict):
        raise ValueError('T6_API_SERVICE_PERMISSIONS must map service names to lists of roles')
    grants: dict[str, frozenset[str]] = {}
    for name, roles in value.items():
        if (name not in SERVICES or not isinstance(roles, list)
                or any(not isinstance(role, str) or role not in PERMISSIONS for role in roles)):
            raise ValueError('Invalid service or permission in T6_API_SERVICE_PERMISSIONS')
        grants[name] = frozenset(roles)
    return grants
