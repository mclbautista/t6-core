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
from t6.privacy import Registry as PrivacyRegistry, Session
from t6.server.app import Request, Response, WebhookApp
from t6.registry.sqlite import _audit
from t6.verbs import VERBS, Done, Refused, catalogue, invoke


class VerbAPI:
    def __init__(self, people: People, queue: Any, secrets: Mapping[str, str],
                 open_notion: Callable[..., Any], tap_app: WebhookApp | None = None,
                 *, llm_service: Any = None, data_sources: Mapping[str, str] | None = None,
                 privacy_registry: PrivacyRegistry | None = None) -> None:
        if any(not secret or not (identity.startswith('t6n_') or identity in SERVICES)
               for secret, identity in secrets.items()):
            raise ValueError('Each API secret must bind to a Person or named service')
        self.people, self.queue, self.secrets, self.open_notion = people, queue, dict(secrets), open_notion
        self.tap_app = tap_app
        self.llm_service = llm_service
        self.data_sources = dict(data_sources or {})
        self.privacy_registry = privacy_registry
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
        if path == '/tap':
            return self._tap(request, actor)
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
            if entry.input_model.model_config.get('extra') != 'forbid':
                data.pop('registry_db', None)
            return self._dispatch(entry, data, actor)

    def _dispatch(self, entry: Any, data: dict[str, Any], actor: str) -> Response:
        try:
            inputs = entry.input_model.model_validate(data)
        except ValidationError as error:
            return Response(422, {'errors': cast(JsonValue, error.errors(include_url=False, include_context=False, include_input=False))})
        if (entry.noun, entry.verb) in {('project', 'list'), ('project', 'get'), ('project', 'resolve'), ('jo', 'list'), ('jo', 'find'), ('jo', 'get'), ('person', 'get'), ('person', 'find'), ('person', 'list'), ('camera', 'get'), ('departments', ''), ('standing-cc', ''), ('external-link', 'find'), ('studio', 'notices'), ('deliverable', 'list'), ('meeting-notes', 'list')}:
            refusal = self.people.authorize(actor, entry.role)
            outcome = refusal or self._typed_read(entry.noun, entry.verb, inputs)
            if isinstance(outcome, Done) and entry.noun in {'project', 'jo', 'person', 'camera', 'deliverable', 'meeting-notes', 'external-link', 'studio'} and self.people.authorize(actor, 'operator'):
                if self.privacy_registry is None:
                    outcome = Refused(rule='masking', reason='Read privacy registry is not configured')
                else:
                    session = Session(self.privacy_registry)
                    def mask(value: Any, key: str = '') -> Any:
                        if key == 'id' or key.endswith(('_id', '_ids')) or key == 'telegram_key':
                            if session.leaks(value):
                                raise ValueError('Client name occurs in an identifier')
                            return value
                        if isinstance(value, str):
                            return session.mask(value).text
                        if isinstance(value, list):
                            return [mask(item) for item in value]
                        if isinstance(value, dict):
                            return {name: mask(item, name) for name, item in value.items()}
                        return value
                    try:
                        outcome = Done(result=mask(outcome.model_dump(mode='json')['result']))
                    except ValueError:
                        outcome = Refused(rule='masking', reason='A client name occurs in an identifier')
        elif (entry.noun, entry.verb) == ('llm', 'run'):
            refusal = self.people.authorize(actor, entry.role)
            outcome = refusal or (self.llm_service.run(inputs, actor=actor, registry=self.people.registry) if self.llm_service else Refused(rule='config', reason='LLM service is not configured'))
        elif entry.handler is None:
            refusal = self.people.authorize(actor, entry.role)
            outcome = refusal or self._legacy_read(entry.noun, entry.verb, inputs)
        else:
            outcome = invoke(entry, inputs, actor=actor, people=self.people, open_notion=self.open_notion)
        return Response(200, outcome.model_dump(mode='json'))

    def _typed_read(self, noun: str, verb: str, inputs: Any) -> Any:
        from datetime import date, datetime
        from dataclasses import asdict, replace
        from t6.notion import NotionError
        from t6.notion.reads import ProjectReader, DeliverableReader, is_live
        from t6.notion.job_orders import JobOrders
        from t6.notion.meeting_notes import note_from_page
        from t6.domain.job_order_rules import status_group, is_active
        from t6.registry import ProjectRefs
        from t6.registry.core import normalize_page_id
        from t6.cli import TOKEN_VARIABLE
        if noun == 'standing-cc':
            from t6.domain.studio_people import standing_cc
            page = standing_cc()
            return Done(result={'standing_cc': (self.people.registry.bound_entity('notion', page) or page) if page else None})
        token = os.environ.get(TOKEN_VARIABLE)
        if not token:
            return Refused(rule='config', reason='Notion token is not configured')
        refs = ProjectRefs(self.people.registry)
        def identity(page: str) -> str:
            return self.people.registry.bound_entity('notion', page) or page
        def same_project(a: str | None, b: str) -> bool:
            if refs.same(a, b):
                return True
            return a is not None and refs.identity(a) is None and refs.identity(b) is None and normalize_page_id(a) == normalize_page_id(b)
        def project_result(project: Any) -> Any:
            project = refs.identified(project)
            def ids(pages: tuple[str, ...]) -> tuple[str, ...]:
                return tuple(identity(page) for page in pages)
            return replace(project, editor_ids=ids(project.editor_ids),
                           assistant_editor_ids=ids(project.assistant_editor_ids),
                           online_editor_ids=ids(project.online_editor_ids), colorist_ids=ids(project.colorist_ids),
                           sound_tech_ids=ids(project.sound_tech_ids), post_coordinator_ids=ids(project.post_coordinator_ids),
                           director_ids=ids(project.director_ids), cinematographer_ids=ids(project.cinematographer_ids))
        def order_result(order: Any) -> dict[str, Any]:
            item = asdict(order)
            item['t6_id'] = self.people.registry.bound_entity('notion', order.page_id)
            item['project_id'] = refs.identity(order.project_id) or order.project_id
            for field in ('assigned_ids', 'cc_ids', 'coordinator_ids', 'deliverable_ids'):
                item[field] = [identity(page) for page in item[field]]
            return item
        try:
            with self.open_notion(token) as client:
                if noun == 'person':
                    from t6.notion.reads import ContactReader
                    from t6.lookup import Index, Lookup
                    from t6.cli_studio import _resolution
                    from uuid import UUID
                    contact_reader = ContactReader(client, self.data_sources['contacts'])
                    def contact(c: Any) -> dict[str, Any]:
                        item = asdict(c)
                        item['t6_id'] = self.people.registry.bound_entity('notion', c.page_id)
                        return item
                    if verb == 'list':
                        return Done(result={'people': [contact(c) for c in contact_reader.list()]})
                    if verb == 'find':
                        return Done(result=_resolution(Lookup(Index(contacts=contact_reader.list())).contact(inputs.mention), contact))
                    person = self.people.get(identity(inputs.person_id))
                    page = next((b['page_id'] for b in person['bindings'] if b['active']), None) if person else inputs.person_id
                    if not page:
                        return Refused(rule='person', reason='Person has no active Contact binding')
                    if len(page) == 32:
                        page = str(UUID(page))
                    if (client.retrieve_page(page).get('parent') or {}).get('data_source_id') != self.data_sources['contacts']:
                        return Refused(rule='person', reason='Contact not found')
                    item = contact(contact_reader.get(page))
                    if person:
                        item.update({key: person[key] for key in ('kind', 'departments', 'roles', 'shift', 'permissions', 'master_brief', 'active')})
                    return Done(result={'person': item})
                if noun == 'external-link':
                    from t6.domain import ExternalRef
                    from t6.notion.external_links import ExternalLinks
                    link = ExternalLinks(client, self.data_sources['external_links']).find(
                        ExternalRef(system=inputs.system, kind=inputs.kind, external_id=inputs.external_id))
                    link_item = asdict(link) if link else None
                    if link_item is not None:
                        link_item['project_ids'] = [refs.identity(page) or page for page in link_item['project_ids']]
                    return Done(result={'link': link_item})
                if noun == 'studio':
                    source = client.retrieve_data_source(self.data_sources['projects'])
                    description = ''.join(part.get('plain_text', (part.get('text') or {}).get('content', ''))
                                          for part in source.get('description') or [])
                    return Done(result={'description': description})
                if noun == 'departments':
                    from t6.notion.departments import get_departments
                    return Done(result={'departments': [{'id': d.option_id, 'name': d.name} for d in get_departments(client, self.data_sources['job_orders'])]})
                if noun == 'camera':
                    from t6.notion.cameras import get_cameras
                    cameras = get_cameras(client, self.data_sources['cameras'], page_id=inputs.page_id)
                    return Done(result={'cameras': [{'page_id': c['id'], 'name': c['name'] or '', 'tags': c['tags']} for c in cameras]})
                if noun == 'project':
                    projects = [project_result(p) for p in ProjectReader(client, self.data_sources['projects']).list()]
                    if verb == 'get':
                        pages = refs.pages(inputs.project)
                        active = pages[0].replace('-', '').lower() if pages else ''
                        project = next((p for p in projects if p.page_id.replace('-', '').lower() == active), None)
                        return Done(result={'project': project}) if project else Refused(rule='project', reason='Project not found')
                    if verb == 'resolve':
                        from t6.lookup import Index, Lookup
                        from t6.cli_studio import _resolution
                        lookup = Lookup(Index(projects=tuple(projects)))
                        found = lookup.project(inputs.text)
                        if found.entity is None and not found.candidates:
                            found = lookup.project(None, name=inputs.text)
                        return Done(result=_resolution(found, asdict))
                    return Done(result={'projects': [p for p in projects if (inputs.rotation is None or p.rotation == inputs.rotation)
                                and (inputs.status is None or p.status == inputs.status)]})
                if noun == 'jo':
                    jo_reader = JobOrders(client, self.data_sources['job_orders'])
                    if verb == 'get':
                        from t6.job_order import JobOrderStore
                        from t6.preview import PreviewTokens
                        from uuid import UUID
                        record = JobOrderStore(self.people.registry, PreviewTokens('read-only')).get(inputs.ref)
                        if record:
                            page = next((b['page_id'] for b in record['bindings'] if b['active']), None)
                        else:
                            from t6.domain.job_order_rules import parse_telegram_key
                            if parse_telegram_key(inputs.ref) is not None:
                                matching_orders = jo_reader.find_all_by_telegram_key(inputs.ref)
                                if len(matching_orders) > 1:
                                    from t6.verbs import Ambiguous
                                    return Ambiguous(candidates=[identity(o.page_id) for o in matching_orders])
                                page = matching_orders[0].page_id if matching_orders else None
                            else:
                                page = inputs.ref
                        if not page:
                            return Refused(rule='job_order', reason='Job Order has no active Notion page')
                        if len(page) == 32:
                            page = str(UUID(page))
                        if (client.retrieve_page(page).get('parent') or {}).get('data_source_id') != self.data_sources['job_orders']:
                            return Refused(rule='job_order', reason='Job Order not found')
                        order = jo_reader.get(page)
                        return Done(result={'job_order': order_result(order)})
                    orders = jo_reader.find_all_by_telegram_key(inputs.telegram_key) if verb == 'find' else jo_reader.list()
                    if verb == 'list':
                        orders = tuple(o for o in orders if (not inputs.project or same_project(o.project_id, inputs.project))
                            and (not inputs.assignee or any(normalize_page_id(identity(p)) == normalize_page_id(identity(inputs.assignee)) for p in o.assigned_ids))
                            and (not inputs.status_group or (is_active(o.status) if inputs.status_group == 'active' else status_group(o.status) == inputs.status_group)))
                    items = []
                    for order in orders:
                        items.append(order_result(order))
                    return Done(result={'job_orders': items})
                if noun == 'deliverable':
                    pages = refs.pages(inputs.project)
                    items = [asdict(d) for page in pages for d in DeliverableReader(client, self.data_sources['deliverables']).for_project(page)]
                    for item in items:
                        item['t6_id'] = self.people.registry.bound_entity('notion', item['page_id'])
                        item['project_id'] = refs.identity(item['project_id']) or item['project_id']
                    return Done(result={'deliverables': list({item['page_id']: item for item in items}.values())})
                since = date.fromisoformat(inputs.since) if inputs.since else None
                notes = [note_from_page(p) for p in client.query_data_source(self.data_sources['meeting_notes']) if is_live(p)]
                before = date.fromisoformat(inputs.before) if inputs.before else None
                def matches_note(n: Any) -> bool:
                    day = n.meeting_date.date() if isinstance(n.meeting_date, datetime) else n.meeting_date
                    return bool((not inputs.project or any(same_project(p, inputs.project) for p in n.project_ids))
                        and (since is None or (day is not None and day >= since))
                        and (before is None or (day is not None and day < before))
                        and (inputs.title is None or n.name == inputs.title)
                        and (inputs.title_prefix is None or n.name.startswith(inputs.title_prefix))
                        and (inputs.ready_to_send is None or (n.ready_to_send and n.sent_at is None) == inputs.ready_to_send))
                notes = [n for n in notes if matches_note(n)]
                def blocks(parent: str) -> list[dict[str, Any]]:
                    result = []
                    for block in client.list_block_children(parent):
                        kind = block.get('type', '')
                        content = block.get(kind) or {}
                        parts = content.get('rich_text') or []
                        text = ''.join(part.get('plain_text', (part.get('text') or {}).get('content', '')) for part in parts)
                        spans = []
                        for part in parts:
                            annotations = part.get('annotations') or {}
                            spans.append({'text': part.get('plain_text', (part.get('text') or {}).get('content', '')),
                                          **{key: bool(annotations.get(key)) for key in ('bold', 'italic', 'underline', 'strikethrough', 'code')},
                                          'url': part.get('href') or ((part.get('text') or {}).get('link') or {}).get('url')})
                        result.append({'kind': kind, 'text': text, 'spans': spans,
                                       'checked': content.get('checked') if kind == 'to_do' else None,
                                       'children': blocks(block['id']) if block.get('has_children') else []})
                    return result
                items = []
                for note in notes:
                    item = asdict(note)
                    item['project_ids'] = [refs.identity(page) or page for page in note.project_ids]
                    item['body'] = blocks(note.page_id)
                    items.append(item)
                return Done(result={'meeting_notes': items})
        except (KeyError, ValueError, OSError, NotionError):
            return Refused(rule='read', reason='Read failed; check the input and server configuration')

    def _legacy_read(self, noun: str, verb: str, inputs: Any) -> Any:
        # The legacy write commands do not have signed previews yet.
        if (noun, verb) == ('camera', 'enrich'):
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

    def _tap(self, request: Request, actor: str) -> Response:
        if actor != 'gateway':
            return Response(403, {'error': 'only the gateway may push taps'})
        if request.method != 'POST':
            return Response(405)
        if self.tap_app is None:
            return Response(503, {'error': 'tap service not configured'})
        try:
            telegram_id = int(request.header('x-telegram-user-id'))
            body = json.loads(request.body)
            sender = body.get('from') if isinstance(body, dict) else None
            if not isinstance(sender, dict) or type(sender.get('id')) is not int or sender['id'] != telegram_id:
                raise ValueError('Telegram identity does not match payload')
        except (ValueError, UnicodeDecodeError):
            return Response(400, {'error': 'invalid Telegram identity or body'})
        with self.people.registry._transaction() as db:
            person = db.execute('SELECT id FROM persons WHERE telegram_id = ? AND active = 1', (telegram_id,)).fetchone()
        if person is None:
            if self.tap_app._operator is not None:
                self.tap_app._operator.store.reject('no_person', operator=telegram_id, token=None)
            with self.people.registry._transaction() as db:
                _audit(db, f'telegram:{telegram_id}', 'tap_rejected', 'gateway', self.people.registry._clock(), None,
                       {'reason': 'no_person'})
            return Response(200, {'outcome': 'rejected'})
        response = self.tap_app._tap(request, authenticated=True)
        outcome = response.body.get('outcome')
        if response.status == 200 and outcome not in {'rejected', 'ignored', 'already_handled', 'busy', 'expired'}:
            key = request.header('idempotency-key') or body.get('idempotency_key')
            with self.people.registry._transaction() as db:
                recorded = db.execute("SELECT 1 FROM audit WHERE action = 'tap_accepted' AND json_extract(after, '$.key') = ?", (key,)).fetchone()
                if not recorded:
                    _audit(db, person[0], 'tap_accepted', person[0], self.people.registry._clock(), None,
                           {'key': key, 'data': body.get('data'), 'outcome': outcome})
        return response


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


def read_config_from_env(environ: Mapping[str, str]) -> tuple[dict[str, str], PrivacyRegistry | None]:
    """Load API-owned Notion source IDs and client-name masking terms."""
    from t6.privacy import Person
    from t6.cli import _read_data_sources
    source_path = environ.get('T6_READ_DATA_SOURCES')
    privacy_path = environ.get('T6_READ_PRIVACY_REGISTRY')
    sources = _read_data_sources(Path(source_path)) if source_path else {}
    if not privacy_path:
        return sources, None
    with open(privacy_path, encoding='utf-8') as file:
        names = json.load(file)
    if not isinstance(names, list) or any(not isinstance(name, str) or not name.strip() for name in names):
        raise ValueError('T6_READ_PRIVACY_REGISTRY must contain a JSON array of client names')
    return sources, PrivacyRegistry(people=[Person(name=name) for name in names])
