"""Authenticated HTTP boundary for the studio verb catalogue."""
from __future__ import annotations

import hmac
import json
import os
import sys
import re
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from importlib.metadata import version
from typing import Any, Callable, Mapping, cast

from t6.domain.events import JobKind, JsonValue
from urllib.parse import urlsplit

from pydantic import ValidationError

from t6.frameio.project_folders import LocalFrameioQueue, QueueFrameioWriter
from t6.person import AUTO_ACTOR, PERMISSIONS, SERVICES, People
from t6.privacy import MaskOptions, Registry as PrivacyRegistry, Session, mask_reply
from t6.server.app import Request, Response, WebhookApp
from t6.server.queue import MAIN_QUEUE
from t6.registry.sqlite import _audit
from t6.verbs import VERBS, Done, Preview, Refused, catalogue, invoke
from t6.vocabulary import PERSON_KINDS


class VerbAPI:
    def __init__(self, people: People, queue: Any, secrets: Mapping[str, str],
                 open_notion: Callable[..., Any], tap_app: WebhookApp | None = None,
                 *, llm_service: Any = None, data_sources: Mapping[str, str] | None = None,
                 privacy_registry: PrivacyRegistry | None = None, transcribe_rate: float = 0.4,
                 translate_enabled: bool = False, frameio_timeout: float = 60.0,
                 frameio_queue: Any = None, concern_observe_closes: bool = False,
                 request_log: Callable[[str], None] | None = None, privacy_list: PrivacyList | None = None) -> None:
        import math
        if not math.isfinite(transcribe_rate) or transcribe_rate < 0:
            raise ValueError('Transcription rate must be finite and non-negative')
        if any(not secret or not (identity.startswith('t6n_') or identity in SERVICES)
               for secret, identity in secrets.items()):
            raise ValueError('Each API secret must bind to a Person or named service')
        self.people, self.queue, self.secrets, self.open_notion = people, queue, dict(secrets), open_notion
        self.tap_app = tap_app
        self.llm_service = llm_service
        self.data_sources = dict(data_sources or {})
        self._privacy_registry, self.privacy_list = privacy_registry, privacy_list
        self.transcribe_rate = transcribe_rate
        self.translate_enabled = translate_enabled
        self.concern_observe_closes = concern_observe_closes
        self.request_log = request_log
        self.frameio = QueueFrameioWriter(frameio_queue or LocalFrameioQueue(queue), timeout=frameio_timeout)  # asks the Frame.io worker; holds no token
        self.routes = {f'/v1/{v.noun}' + ('/' + v.verb.replace(' ', '/') if v.verb else ''): v for v in VERBS}
        for verb in VERBS:
            if verb.noun == 'approval':
                for alias in verb.aliases:
                    self.routes['/v1/' + alias.replace(' ', '/')] = verb

    def _data_gaps(self) -> dict[str, JsonValue]:
        """Projects whose Notion row has no name: the page ids, never a name, for the operator to fix in Notion."""
        token = os.environ.get('T6_NOTION_TOKEN')
        if not token or not self.data_sources.get('projects'):
            return {}
        from t6.notion import NotionError
        from t6.notion.reads import ProjectReader
        try:
            with self.open_notion(token) as client:
                blank = sorted(p.page_id for p in ProjectReader(client, self.data_sources['projects']).list() if not p.name.strip())
        except (NotionError, OSError) as error:
            return {'data_gaps': {'unavailable': str(error)}}
        return {'data_gaps': {'projects_without_name': {'count': len(blank), 'page_ids': list(blank)}}}

    @property
    def privacy_registry(self) -> PrivacyRegistry | None:
        return self.privacy_list.current() if self.privacy_list else self._privacy_registry

    @privacy_registry.setter
    def privacy_registry(self, value: PrivacyRegistry | None) -> None:
        self._privacy_registry, self.privacy_list = value, None

    def __call__(self, request: Request) -> Response:
        """Answer a request and write its one request log line (see `request_line`); `/health` is not logged."""
        path = urlsplit(request.path).path
        if self.request_log is None or path == '/health':
            return self._handle(request, {})
        seen: dict[str, str] = {}
        started = time.monotonic()
        try:
            response = self._handle(request, seen)
        except Exception:
            self.request_log(request_line(path, seen.get('identity'), Response(500), time.monotonic() - started, self.routes))
            raise
        self.request_log(request_line(path, seen.get('identity'), response, time.monotonic() - started, self.routes))
        return response

    def _handle(self, request: Request, seen: dict[str, str]) -> Response:
        path = urlsplit(request.path).path
        if path == '/health':
            if request.method != 'GET':
                return Response(405)
            with self.people.registry._transaction() as db:
                schema = db.execute('SELECT MAX(version) FROM schema_version').fetchone()[0]
            with self.queue._transaction() as db:
                jobs = dict(db.execute('SELECT state, COUNT(*) FROM jobs GROUP BY state').fetchall())
            report: dict[str, JsonValue] = {'version': version('t6'), 'schema_version': schema, 'jobs': jobs,
                                            'privacy_registry': self.privacy_list.status() if self.privacy_list else None}
            auth = request.header('authorization')
            supplied = auth[7:] if auth.lower().startswith('bearer ') else ''
            if supplied and any(hmac.compare_digest(secret, supplied) for secret in self.secrets):
                report.update(self._data_gaps())  # page ids are for a caller that holds a secret
            return Response(200, report)
        auth = request.header('authorization')
        supplied = auth[7:] if auth.lower().startswith('bearer ') else ''
        actor = next((identity for secret, identity in self.secrets.items()
                      if supplied and hmac.compare_digest(secret, supplied)), None)
        if actor is None:
            return Response(401, {'error': 'unauthorized'})
        seen['identity'] = actor
        if path.startswith('/v1/transcribe/'):
            return self._transcribe_route(path, request, actor)
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
            seen['identity'] = actor
        if request.header('x-t6-require-unmasked') == '1' and not self._reads_unmasked(actor):
            # A masked answer does not say it is masked: a caller that passes names on asks to be stopped instead (#488).
            return self.refused('unmasked', 'The caller asks for real names and has neither operator nor the unmasked grant')
        if path == '/v1/commands':
            return Response(200, cast(Mapping[str, JsonValue], catalogue())) if request.method == 'GET' else Response(405)
        if path == '/v1/admin/media/migrate':
            if request.method != 'POST':
                return Response(405)
            refusal = self.people.authorize(actor, 'admin')
            person = self.people.get(actor)
            if not person or not person['active']:
                return self.refused('person', 'An active Person is required')
            if refusal:
                return Response(200, refusal.model_dump(mode='json'))
            from t6.verbs import MediaMigrateInput
            from t6.media import MediaStore
            from t6.notion import NotionError
            from t6.media_migration import read_external_links_once_for_migration
            try:
                data = json.loads(request.body)
                if not isinstance(data, dict) or set(data) - {'idempotency_key', 'confirm'}:
                    raise ValueError('Invalid inputs')
                inputs = MediaMigrateInput.model_validate(data)
                key = request.header('idempotency-key')
                if key and inputs.idempotency_key not in (None, key):
                    return self.refused('idempotency_key', 'Conflicting idempotency keys')
                token = os.environ.get('T6_NOTION_TOKEN')
                signing = os.environ.get('T6_PREVIEW_SIGNING_KEY')
                if not self.data_sources.get('external_links'):
                    return self.refused('config', 'There is no External Links source to migrate from')
                if not token or not signing:
                    return self.refused('config', 'Notion and preview configuration are required')
                with self.open_notion(token) as client:
                    rows = read_external_links_once_for_migration(client, self.data_sources['external_links'])
                result = MediaStore(self.people.registry, self.people.tokens).migrate_write(
                    rows, actor=actor, idempotency_key=key or inputs.idempotency_key, confirm=inputs.confirm)
                if self.tap_app:  # the route is outside the verb registry; what follows a verb follows it too
                    self.tap_app.after_verb('admin media', 'migrate', result)
                return Response(200, result.model_dump(mode='json'))
            except (ValueError, KeyError, OSError, NotionError, ValidationError) as error:
                return self.refused('input', str(error))
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
        if entry.noun == 'approval' and entry.verb == 'request' and path.startswith('/v1/approval/request-'):
            gate = path.removeprefix('/v1/approval/request-')
            if 'gate' in data and data['gate'] != gate:
                return self.refused('gate', 'Gate conflicts with the alias')
            data['gate'] = gate
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
                    if not entry.input_model.model_fields[name].is_required():
                        data.pop(name, None)  # a path sent by the caller is never opened
                        continue
                    return Response(422, {'errors': [{'loc': [name], 'msg': 'File contents required'}]})
                uploaded = Path(directory) / name
                uploaded.write_text(contents, encoding='utf-8')
                data[name] = str(uploaded)
            if entry.input_model.model_config.get('extra') != 'forbid':
                data.pop('registry_db', None)
            response = self._dispatch(entry, data, actor)
            if entry.read_only and self._masked_for(actor, request.header('x-t6-mcp') == '1'):
                return self._mask_mcp(response)
            return response

    def _masked_for(self, actor: str, mcp_header: bool) -> bool:
        """Mask unless the secret is bound to a Person who reads unmasked; the `mcp` service is always masked."""
        if actor in SERVICES:
            return actor == 'mcp' or mcp_header
        return mcp_header and not self._reads_unmasked(actor)

    def _reads_unmasked(self, actor: str) -> bool:
        """`operator` and the `unmasked` grant read real names; `unmasked` opens no write verb."""
        return any(self.people.authorize(actor, role) is None for role in ('operator', 'unmasked'))

    def _mask_mcp(self, response: Response) -> Response:
        if self.privacy_registry is None:
            return self.refused('masking', 'Read privacy registry is not configured')
        session = Session(self.privacy_registry, options=MaskOptions())  # reads hide every registered name, as before
        try:
            return Response(response.status, mask_reply(session, response.body))
        except ValueError:
            return self.refused('masking', 'A client name occurs in an identifier')

    def _dispatch(self, entry: Any, data: dict[str, Any], actor: str) -> Response:
        if actor == AUTO_ACTOR:
            refusal = People.auto_refusal(entry.noun)
            if refusal:
                return self.refused(refusal.rule, refusal.reason)
        outcome: Any
        try:
            inputs = entry.input_model.model_validate(data)
        except ValidationError as error:
            return Response(422, {'errors': cast(JsonValue, error.errors(include_url=False, include_context=False, include_input=False))})
        if (entry.noun, entry.verb) == ('transcript', 'get') and inputs.transcript.isdecimal():
            person = self.people.get(actor)
            if not person or not person['active']:
                return self.refused('person', 'An active Person is required')
            from t6.transcript import Forbidden, NotFound
            try:
                value = self._transcripts().result(int(inputs.transcript), actor)
            except (NotFound, Forbidden, KeyError, TypeError):
                return self.refused('job', 'Job not found')
            if value is None:
                return Response(200, Done(result={'job_id': int(inputs.transcript), 'state': 'pending'}).model_dump(mode='json'))
            return Response(200, cast(Mapping[str, JsonValue], value))
        if (entry.noun, entry.verb) in {('transcript', 'create'), ('transcript', 'speakers'), ('transcript', 'translate')}:
            refusal = self.people.authorize(actor, entry.role)
            outcome = refusal or self._transcripts().request(entry.verb, inputs, actor, translate_enabled=self.translate_enabled)
        elif (entry.noun, entry.verb) in {('project', 'list'), ('project', 'get'), ('project', 'notes'), ('project', 'flags'), ('project', 'resolve'), ('concern', 'list'), ('concern', 'get'), ('jo', 'list'), ('jo', 'find'), ('jo', 'get'), ('person', 'get'), ('person', 'find'), ('person', 'list'), ('camera', 'get'), ('departments', ''), ('standing-cc', ''), ('external-link', 'find'), ('studio', 'notices'), ('deliverable', 'list'), ('deliverable', 'candidates'), ('deliverable', 'get'), ('meeting-notes', 'list'), ('version', 'find'), ('version', 'get')}:
            refusal = self.people.authorize(actor, entry.role)
            outcome = refusal or self._typed_read(entry.noun, entry.verb, inputs)
            if isinstance(outcome, Done) and entry.noun in {'project', 'concern', 'jo', 'person', 'camera', 'deliverable', 'meeting-notes', 'external-link', 'studio'} and not self._reads_unmasked(actor):
                outcome = self._masked_outcome(outcome)
        elif entry.noun == 'approval':
            if self.tap_app is None or self.tap_app._operator is None:
                outcome = Refused(rule='config', reason='Operator cards are not configured')
            else:
                self.tap_app._redeliver_cards()
                outcome = invoke(entry, inputs, actor=actor, people=self.people, open_notion=self.open_notion)
                if isinstance(outcome, Done):
                    from t6.approval import Approvals
                    approval = Approvals(self.people.registry, self.people.tokens).get(outcome.result['approval_id'])
                    assert approval is not None
                    if entry.verb == 'request' and approval['state'] == 'requested':
                        action, _ = self.tap_app._operator.offer_approval(approval)
                        self.tap_app._deliver(action.token, action.version)
                    elif entry.verb != 'request':
                        existing_action = self.tap_app._operator.approval_action(approval['id'])
                        if existing_action is not None and existing_action.state != 'done':
                            action = self.tap_app._operator.store.finish(existing_action.token, {'outcome': approval['state']})
                            self.tap_app._deliver(action.token, action.version)
        elif (entry.noun, entry.verb) == ('deliverable', 'deliver'):
            refusal = self.people.authorize(actor, entry.role)
            if refusal:
                outcome = refusal
            else:
                from t6.package import PackageStore
                from t6.notion.package_specs import PackageSpecReader
                from t6.notion import NotionError
                from t6.cli import TOKEN_VARIABLE
                store = PackageStore(self.people.registry, self.people.tokens)
                try:
                    token = os.environ.get(TOKEN_VARIABLE)
                    source = self.data_sources.get('package_specs')
                    if token and source:
                        with self.open_notion(token) as client:
                            store.specs = PackageSpecReader(client, source)
                            outcome = store.deliver(inputs.model_dump(exclude={'idempotency_key', 'confirm'}), actor=actor, idempotency_key=inputs.idempotency_key, confirm=inputs.confirm)
                    else:
                        outcome = store.deliver(inputs.model_dump(exclude={'idempotency_key', 'confirm'}), actor=actor, idempotency_key=inputs.idempotency_key, confirm=inputs.confirm)
                except (OSError, ValueError, KeyError, NotionError) as error:
                    outcome = Refused(rule='spec', reason=str(error))
        elif (entry.noun, entry.verb) == ('concern', 'scan'):
            refusal = self.people.authorize(actor, entry.role)
            if refusal:
                outcome = refusal
            elif self.llm_service is None:
                outcome = Refused(rule='config', reason='LLM service is not configured')
            else:
                from t6.verbs import ProjectNotesInput
                from t6.concern import ConcernStore
                from t6.concern_task import ClassifyRefused, classify
                notes = self._typed_read('project', 'notes', ProjectNotesInput(project=inputs.project)) if inputs.owner in self._coordinators(inputs.project) else Refused(rule='owner', reason='Owner must be a Project coordinator')
                if isinstance(notes, Done):
                    try:
                        ConcernStore(self.people.registry, self.people.tokens).process_notes(inputs.project, inputs.owner, notes.result['lines'], lambda payload: classify(self.llm_service, payload, actor=actor, registry=self.people.registry))
                        outcome = Done(result={'project': inputs.project})
                    except ValueError:
                        outcome = Refused(rule='project', reason='Project and active coordinator are required')
                    except ClassifyRefused as refused:
                        outcome = Refused(rule=refused.rule, reason='llm run refused the classification')
                else:
                    outcome = notes
        elif (entry.noun, entry.verb) == ('llm', 'run'):
            refusal = self.people.authorize(actor, entry.role)
            outcome = refusal or (self.llm_service.run(inputs, actor=actor, registry=self.people.registry) if self.llm_service else Refused(rule='config', reason='LLM service is not configured'))
        elif (entry.noun, entry.verb) == ('llm', 'decide'):
            refusal = self.people.authorize(actor, entry.role)
            outcome = refusal or (self.llm_service.decide(inputs, actor=actor, registry=self.people.registry) if self.llm_service else Refused(rule='config', reason='LLM service is not configured'))
        elif (entry.noun, entry.verb) == ('llm', 'filter'):
            refusal = self.people.authorize(actor, entry.role)
            outcome = refusal or (self.llm_service.filter(inputs, actor=actor, registry=self.people.registry) if self.llm_service else Refused(rule='config', reason='LLM service is not configured'))
        elif entry.noun == 'meeting-notes':
            refusal = self.people.authorize(actor, entry.role)
            outcome = refusal or self._meeting_notes(entry.verb, inputs, actor)
            # The reply holds the note's title and attendee names, so it is masked like `meeting-notes list`.
            if isinstance(outcome, Done) and not self._reads_unmasked(actor):
                outcome = self._masked_outcome(outcome)
        elif (entry.noun, entry.verb) == ('machine', 'edit'):
            refusal = self.people.authorize(actor, entry.role)
            outcome = refusal or self._machine_edit(inputs, actor)
        elif entry.noun == 'machine':
            refusal = self.people.authorize(actor, entry.role)
            outcome = refusal or self._machine(entry.verb, inputs, actor)
        elif (entry.noun, entry.verb) == ('camera', 'enrich'):
            refusal = self.people.authorize(actor, entry.role)
            outcome = refusal or self._camera_enrich(inputs, actor)
        elif (entry.noun, entry.verb) == ('person', 'create'):
            refusal = self.people.authorize(actor, entry.role)
            outcome = refusal or self._person_create(inputs, actor)
        elif (entry.noun, entry.verb) == ('jo', 'raise'):
            # The server knows its own Job Orders data source; a caller never uploads one.
            from dataclasses import replace
            from t6.verbs import jo_write
            source = self.data_sources.get('job_orders')
            outcome = invoke(replace(entry, handler=lambda i, o: jo_write(i, o, source)), inputs, actor=actor, people=self.people, open_notion=self.open_notion)
        elif (entry.noun, entry.verb) == ('person', 'set-telegram') and (page := self._contact_page(inputs.person_id)):
            refusal = self.people.authorize(actor, entry.role)
            outcome = refusal or self._person_set_telegram(inputs, actor, page)
        elif (entry.noun, entry.verb) == ('concern', 'observe'):
            refusal = self.people.authorize(actor, entry.role)
            outcome = refusal or self._concern_observe(inputs, actor)
        elif (entry.noun, entry.verb) == ('concern', 'raise') and inputs.owner is not None and inputs.owner not in self._coordinators(inputs.project):
            outcome = self.people.authorize(actor, entry.role) or Refused(rule='owner', reason='Owner must be a Project coordinator')
        elif entry.handler is None:
            refusal = self.people.authorize(actor, entry.role)
            outcome = refusal or self._legacy_read(entry.noun, entry.verb, inputs)
        else:
            outcome = invoke(entry, inputs, actor=actor, people=self.people, open_notion=self.open_notion, frameio=self.frameio)
        if entry.noun == 'project' and entry.verb in ('link-chat', 'unlink-chat') and isinstance(outcome, Preview):
            if self.tap_app and self.tap_app._operator and outcome.plan.get('approval_id'):
                from t6.approval import Approvals
                approval = Approvals(self.people.registry, self.people.tokens).get(outcome.plan['approval_id'])
                if approval:
                    action, _ = self.tap_app._operator.offer_approval(approval)
                    self.tap_app._deliver(action.token, action.version)
        if self.tap_app:
            self.tap_app.after_verb(entry.noun, entry.verb, outcome)
        return Response(200, outcome.model_dump(mode='json'))

    def _masked_outcome(self, outcome: Done) -> Any:
        if self.privacy_registry is None:
            return Refused(rule='masking', reason='Read privacy registry is not configured')
        try:
            return Done(result=mask_reply(Session(self.privacy_registry, options=MaskOptions()), outcome.model_dump(mode='json')['result']))
        except ValueError:
            return Refused(rule='masking', reason='A client name occurs in an identifier')

    def _concern_observe(self, inputs: Any, actor: str) -> Any:
        """Hand the findings of one run to the Concern list; the reply names Concern text, so a caller without real-name access gets it masked."""
        from t6.concern import ConcernStore
        from t6.concern_observe import observe
        outcome = observe(ConcernStore(self.people.registry, self.people.tokens), inputs.model_dump(), actor=actor, coordinators=self._coordinators, closes=self.concern_observe_closes)
        if isinstance(outcome, Done):
            if not inputs.dry_run:  # counts only: no text, name or ID
                print('t6.server: concern observe run_date=' + inputs.run_date + ' ' + ' '.join(f'{name}={count}' for name, count in outcome.result['counts'].items()), file=sys.stderr, flush=True)
            if not self._reads_unmasked(actor):
                return self._masked_outcome(outcome)
        return outcome

    def _transcripts(self) -> Any:
        from t6.transcript import TranscriptStore
        return TranscriptStore(self.people.registry, self.people.tokens, rate=self.transcribe_rate)

    def _transcribe_route(self, path: str, request: Request, actor: str) -> Response:
        from t6.transcript import ClaimLost, Forbidden, Invalid, NotFound
        if request.method != 'POST':
            return Response(405)
        transcripts = self._transcripts()
        try:
            body = json.loads(request.body)
            if not isinstance(body, dict):
                raise ValueError('Expected an object')
            action = path.removeprefix('/v1/transcribe/')
            if action == 'claim':
                if actor != 'transcribe':
                    return Response(403)
                claim = transcripts.claim()
                job = None if claim is None else {'job_id': claim.job_id, 'kind': claim.kind, 'payload': dict(claim.payload), 'dedupe_key': claim.dedupe_key, 'token': claim.token, 'attempt': claim.attempt}
                return Response(200, {'job': cast(JsonValue, job)})
            if action == 'llm':
                if actor != 'transcribe' or not self.translate_enabled or not self.llm_service:
                    return Response(403)
                if not transcripts.holds_translate_lease(body.get('job_id'), body.get('token')) or not isinstance(body.get('text'), str):
                    return Response(403)
                from t6.transcribe.translate import task_input
                outcome = self.llm_service.run(task_input(body['text']), actor=actor, registry=self.people.registry)
                return Response(200, outcome.model_dump(mode='json'))
            job_id = body.get('job_id')
            if not isinstance(job_id, int):
                return Response(400, {'error': 'job_id required'})
            try:
                if action == 'result':
                    return Response(200, {'result': cast(JsonValue, transcripts.result(job_id, actor))})
                if action == 'extend':
                    transcripts.extend(job_id, body.get('token'), actor)
                elif action == 'fail':
                    transcripts.fail_job(job_id, body.get('token'), actor)
                else:
                    transcripts.report(job_id, body.get('token'), body.get('kind') if action == 'report' else None, body.get('data'), actor)
                return Response(200, {'accepted': True})
            except NotFound:
                return Response(404)
            except Forbidden:
                return Response(403)
            except ClaimLost:
                return Response(409, {'error': 'claim lost'})
            except Invalid as error:
                return Response(400, {'error': error.error}) if error.error else Response(400)
        except (ValueError, KeyError, TypeError):
            return Response(400, {'error': 'invalid request'})

    def _contact_page(self, person_id: str) -> str | None:
        """The Person's active Contacts page, or None for a registry-only Person."""
        person = self.people.get(person_id)
        return next((b['page_id'] for b in person['bindings'] if b['active']), None) if person else None

    def _person_set_telegram(self, inputs: Any, actor: str, page: str) -> Any:
        """Set a Person's Telegram ID on the Contact first, then in the registry.

        Setting the same value twice is harmless, so Notion goes first and the
        registry change (with its audit entry and key) is the commit point.
        Until then a retry with the same key writes the Contact again and
        then commits once. A confirm answers `Ambiguous` when the Contact
        could not be confirmed, and nothing is committed.
        """
        from t6.notion import NotionError
        from t6.notion.contacts import Contacts
        from t6.notion.writes import Rejected, Ambiguous as WriteAmbiguous
        from t6.verbs import Ambiguous
        from uuid import UUID
        values = {'person_id': inputs.person_id, 'telegram_id': inputs.telegram_id}
        key = inputs.idempotency_key

        def write(confirm: str | None) -> Any:
            return self.people.write('set-telegram', values, actor=actor, idempotency_key=key, confirm=confirm)

        if not inputs.confirm or not os.environ.get('T6_PREVIEW_SIGNING_KEY'):
            return write(inputs.confirm)  # the preview, and the refusals, are the registry verb's
        nonce = self.people.tokens.verify(inputs.confirm, 'person set-telegram', actor, {**values, 'idempotency_key': key})
        if isinstance(nonce, Refused):
            return nonce
        registry = self.people.registry
        with registry._transaction() as db:
            committed = db.execute('SELECT 1 FROM verb_requests WHERE idempotency_key = ?', (key,)).fetchone() or db.execute('SELECT 1 FROM used_preview_tokens WHERE nonce = ?', (nonce,)).fetchone()
            holder = None if committed else db.execute('SELECT id FROM persons WHERE active = 1 AND telegram_id = ? AND id != ?', (inputs.telegram_id, inputs.person_id)).fetchone()
        if committed:
            return write(inputs.confirm)  # already committed: replay or refuse as the registry verb does
        if holder:
            return Refused(rule='telegram_id', reason=f'Telegram ID is held by {holder[0]}')
        try:
            with self.open_notion(os.environ['T6_NOTION_TOKEN']) as client:
                saved = Contacts(client, self.data_sources['contacts']).set_telegram_user_id(str(UUID(page)) if len(page) == 32 else page, inputs.telegram_id)
        except (ValueError, KeyError, OSError, NotionError) as error:
            return Refused(rule='input', reason=str(error))
        if isinstance(saved, Rejected):
            return Refused(rule='notion', reason=str(saved))
        if isinstance(saved, WriteAmbiguous):
            return Ambiguous(candidates=[{'pending': True, 'reason': str(saved), 'page_id': saved.page_id}])
        outcome = write(inputs.confirm)
        if isinstance(outcome, Refused):  # the ID was taken between the check and the commit
            return Ambiguous(candidates=[{'pending': True, 'reason': f'The Contact was updated but the registry refused: {outcome.reason}', 'page_id': page}])
        return outcome

    def _person_create(self, inputs: Any, actor: str) -> Any:
        """Create a Contacts page and register the Person bound to it.

        The page is made once per idempotency key. The key is marked pending
        before Notion is called, so a retry after an unclear failure is
        `Ambiguous` and never a second page.
        """
        import secrets
        from dataclasses import asdict
        from t6.ids import EntityKind, T6Id
        from t6.notion import NotionError
        from t6.notion.contacts import Contacts
        from t6.notion.writes import Rejected, Ambiguous as WriteAmbiguous
        from t6.registry.core import normalize_page_id
        from t6.verbs import Ambiguous
        key = inputs.idempotency_key or secrets.token_hex(16)
        if not os.environ.get('T6_PREVIEW_SIGNING_KEY'):
            return Refused(rule='config', reason='Set T6_PREVIEW_SIGNING_KEY')
        if inputs.confirm and not inputs.idempotency_key:
            return Refused(rule='idempotency_key', reason='Supply the key from the preview')
        name, handle, note = inputs.name.strip(), (inputs.telegram_handle or '').strip().lstrip('@'), (inputs.note or '').strip()
        if not name or inputs.kind not in PERSON_KINDS:
            return Refused(rule='input', reason='A name and valid kind are required')
        values = {'name': name, 'kind': inputs.kind, 'telegram_handle': handle or None, 'telegram_id': inputs.telegram_id, 'note': note or None, 'idempotency_key': key}
        fingerprint = json.dumps({'verb': 'person create', 'actor': actor, **values}, sort_keys=True, separators=(',', ':'))
        nonce = self.people.tokens.verify(inputs.confirm, 'person create', actor, values) if inputs.confirm else None
        if isinstance(nonce, Refused):
            return nonce
        registry = self.people.registry

        def held(db: Any) -> Refused | None:
            holder = db.execute('SELECT id FROM persons WHERE active = 1 AND telegram_id = ?', (inputs.telegram_id,)).fetchone() if inputs.telegram_id is not None else None
            return Refused(rule='telegram_id', reason=f'Telegram ID is held by {holder[0]}') if holder else None

        try:
            source = self.data_sources['contacts']
            token = os.environ['T6_NOTION_TOKEN']
            with registry._transaction() as db:
                previous = db.execute('SELECT fingerprint, result FROM verb_requests WHERE idempotency_key=?', (key,)).fetchone()
                if previous and previous[0] != fingerprint:
                    return Refused(rule='idempotency_key', reason='Idempotency key was used for a different request')
                if nonce and db.execute('SELECT 1 FROM used_preview_tokens WHERE nonce=?', (nonce,)).fetchone():
                    return Refused(rule='token_used', reason='Preview token has already been used')
                if previous and nonce:
                    db.execute('INSERT INTO used_preview_tokens VALUES (?)', (nonce,))
                    result = json.loads(previous[1])
                    return Ambiguous(candidates=[result]) if result.get('pending') else Done(result=result)
                if not previous:
                    refusal = held(db)
                    if refusal:
                        return refusal
                if nonce is None:
                    return self.people.tokens.create('person create', actor, values, {'action': 'create', **values})
                db.execute('INSERT INTO used_preview_tokens VALUES (?)', (nonce,))
                db.execute('INSERT INTO verb_requests VALUES (?, ?, ?)', (key, fingerprint, json.dumps({'pending': True, 'reason': 'The Contacts page may have been created'})))
            with self.open_notion(token) as client:
                saved = Contacts(client, source).create(name, telegram_user_id=inputs.telegram_id, telegram_handle=handle, note=note)
            if isinstance(saved, Rejected):
                # Notion made no page, so the key is free for another try.
                with registry._transaction() as db:
                    db.execute('DELETE FROM verb_requests WHERE idempotency_key=?', (key,))
                return Refused(rule='notion', reason=str(saved))
            if isinstance(saved, WriteAmbiguous):
                pending = {'pending': True, 'reason': str(saved), 'page_id': saved.page_id}
                with registry._transaction() as db:
                    db.execute('UPDATE verb_requests SET result=? WHERE idempotency_key=?', (json.dumps(pending), key))
                return Ambiguous(candidates=[pending])
            with registry._transaction() as db:
                if held(db):
                    pending = {'pending': True, 'reason': 'The Contacts page was created but the Telegram ID was taken meanwhile; register it with person add', 'page_id': saved.page_id}
                    db.execute('UPDATE verb_requests SET result=? WHERE idempotency_key=?', (json.dumps(pending), key))
                    return Ambiguous(candidates=[pending])
                person_id, now = str(T6Id.mint(EntityKind.PERSON)), registry._clock()
                db.execute('INSERT INTO persons VALUES (?, ?, ?, ?, ?, ?, ?)', (person_id, inputs.kind, '[]', '[]', inputs.telegram_id, 0, 1))
                db.execute("INSERT INTO bindings (system, page_id, project_id, bound_at, active) VALUES ('notion', ?, ?, ?, 1)", (normalize_page_id(saved.page_id), person_id, now))
                _audit(db, person_id, 'person_create', actor, now, None, {'page_id': saved.page_id, 'kind': inputs.kind, 'telegram_id': inputs.telegram_id})
            person = self.people.get(person_id)
            assert person is not None
            item = {**asdict(saved), 't6_id': person_id, **{field: person[field] for field in ('kind', 'departments', 'roles', 'project_roles', 'shift', 'permissions', 'master_brief', 'active')}}
            result = {'person': json.loads(json.dumps(item, default=str))}
            with registry._transaction() as db:
                db.execute('UPDATE verb_requests SET result=? WHERE idempotency_key=?', (json.dumps(result), key))
            return Done(result=result)
        except (ValueError, KeyError, OSError, NotionError) as error:
            return Refused(rule='input', reason=str(error))

    def _meeting_notes(self, verb: str, inputs: Any, actor: str) -> Any:
        """The Meeting Notes write verbs. Each write adds an audit row (the page ID, never the title or text)."""
        from dataclasses import asdict
        from datetime import date, datetime, timedelta, timezone
        from t6.notion import NotionError
        from t6.notion.meeting_notes import MeetingNotes, note_from_page
        from t6.notion.writes import Ambiguous as WriteAmbiguous, Rejected
        from t6.registry import ProjectRefs
        from t6.registry.core import normalize_page_id
        from t6.verbs import Ambiguous
        kinds = ('paragraph', 'heading_1', 'heading_2', 'heading_3', 'bulleted_list_item', 'numbered_list_item', 'to_do', 'toggle')
        registry = self.people.registry
        refs = ProjectRefs(registry)

        def notion_blocks(blocks: list[Any]) -> list[dict[str, Any]]:
            result = []
            for block in blocks:
                if block.kind not in kinds:
                    raise ValueError(f'Unknown block kind {block.kind}; use one of {", ".join(kinds)}')
                parts = [{'type': 'text', 'text': {'content': span.text, **({'link': {'url': span.url}} if span.url else {})},
                          'annotations': {key: getattr(span, key) for key in ('bold', 'italic', 'underline', 'strikethrough', 'code')}} for span in block.spans]
                content: dict[str, Any] = {'rich_text': parts or [{'type': 'text', 'text': {'content': block.text}}]}
                if block.kind == 'to_do':
                    content['checked'] = bool(block.checked)
                if block.children:
                    content['children'] = notion_blocks(block.children)
                result.append({'type': block.kind, block.kind: content})
            return result

        def audit(action: str, page_id: str) -> None:
            with registry._transaction() as db:
                _audit(db, page_id, action, actor, registry._clock(), None, {})

        def done(note: Any) -> Any:
            item = asdict(note)
            item['project_ids'] = [refs.identity(page) or page for page in note.project_ids]
            return Done(result={'meeting_note': item})

        try:
            source = self.data_sources['meeting_notes']
            token = os.environ['T6_NOTION_TOKEN']
            with self.open_notion(token) as client:
                notes = MeetingNotes(client, source)
                if verb == 'create':
                    day = date.fromisoformat(inputs.meeting_date)
                    pages = []
                    for ref in inputs.project_ids:
                        found = refs.pages(ref)
                        if not found:
                            return Refused(rule='project', reason=f'Project {ref} not found')
                        pages.append(found[0])
                    saved = notes.create(title=inputs.title, meeting_date=day, project_ids=pages, attendee_ids=inputs.attendee_ids, blocks=notion_blocks(inputs.blocks))
                    if isinstance(saved, Rejected):
                        return Refused(rule='notion', reason=str(saved))
                    if isinstance(saved, WriteAmbiguous):
                        if saved.page_id:
                            audit('meeting_note_create', saved.page_id)
                        return Ambiguous(candidates=[{'pending': True, 'reason': str(saved), 'page_id': saved.page_id}])
                    audit('meeting_note_create', saved.page_id)
                    return done(saved)
                page = client.retrieve_page(inputs.note)
                if normalize_page_id((page.get('parent') or {}).get('data_source_id') or '') != normalize_page_id(source):
                    return Refused(rule='input', reason='Not a Meeting Notes page')
                note = note_from_page(page)
                if verb == 'mark-sent':
                    if note.sent_at is not None:
                        return done(note)
                    when = inputs.at or datetime.now(timezone(timedelta(hours=8)))
                    saved = notes.mark_sent(note.page_id, when)
                    if isinstance(saved, Rejected):
                        return Refused(rule='notion', reason=str(saved))
                    if isinstance(saved, WriteAmbiguous):
                        return Ambiguous(candidates=[{'pending': True, 'reason': str(saved), 'page_id': note.page_id}])
                    audit('meeting_note_sent', note.page_id)
                    return done(saved)
                if note.sent_at is not None:
                    return Refused(rule='sent', reason='The note is already sent')
                if verb == 'trash':
                    notes.trash(note.page_id)
                    audit('meeting_note_trash', note.page_id)
                else:
                    if page.get('in_trash') or page.get('archived'):
                        return Refused(rule='sent', reason='The note is in the trash')
                    try:
                        notes.append(note.page_id, notion_blocks(inputs.blocks))
                    finally:
                        audit('meeting_note_append', note.page_id)  # a refused later chunk leaves the earlier ones written
                return done(note_from_page(client.retrieve_page(note.page_id)))
        except (ValueError, KeyError, OSError, NotionError) as error:
            return Refused(rule='input', reason=str(error))

    def _machine(self, verb: str, inputs: Any, actor: str) -> Any:
        """`machine list` reads the Bays rows; `machine sync` previews, then on confirm writes the fleet facts (`t6.fleet`)."""
        from dataclasses import asdict
        from t6.fleet import Fleet, FleetSafetyError, MachineFacts
        from t6.notion import NotionError
        from t6.notion.writes import Ambiguous as WriteAmbiguous, Rejected
        from t6.verbs import MachineFactChange, MachineFactSkipped, MachineSyncResult
        if not os.environ.get('T6_PREVIEW_SIGNING_KEY') and verb == 'sync':
            return Refused(rule='config', reason='Set T6_PREVIEW_SIGNING_KEY')
        try:
            source = self.data_sources['bays']
            token = os.environ['T6_NOTION_TOKEN']
            if verb == 'sync':
                values = inputs.model_dump(mode='json', exclude={'confirm'})
                nonce = self.people.tokens.verify(inputs.confirm, 'machine sync', actor, values) if inputs.confirm else None
                if isinstance(nonce, Refused):
                    return nonce
                facts = [MachineFacts(**{**machine.model_dump(exclude={'names'}), 'names': tuple(machine.names)}) for machine in inputs.machines]
                with self.people.registry._transaction() as db:
                    if nonce and db.execute('SELECT 1 FROM used_preview_tokens WHERE nonce=?', (nonce,)).fetchone():
                        return Refused(rule='token_used', reason='Preview token has already been used')
                    if nonce:
                        db.execute('INSERT INTO used_preview_tokens VALUES (?)', (nonce,))
            with self.open_notion(token) as client:
                fleet = Fleet(client, source)
                if verb == 'list':
                    return Done(result={'machines': [asdict(machine) for machine in fleet.machines()]})
                results = []
                for facts_item, found in zip(facts, fleet.sync(facts, apply=nonce is not None), strict=True):
                    written = found.written
                    results.append(MachineSyncResult(
                        name=found.name, status=found.status, page_id=found.page_id,
                        changes=[MachineFactChange(fact=change.fact, old=change.old, new=change.new) for change in found.changes],
                        skipped=[MachineFactSkipped(fact=fact, reason='unknown' if getattr(facts_item, fact) in (None, '') else 'stale') for fact in found.skipped],
                        written=None if written is None else 'rejected' if isinstance(written, Rejected) else 'ambiguous' if isinstance(written, WriteAmbiguous) else 'saved',
                        detail=str(written) if isinstance(written, (Rejected, WriteAmbiguous)) else None))
                plan = {'machines': [result.model_dump(mode='json') for result in results]}
                return self.people.tokens.create('machine sync', actor, values, plan) if nonce is None else Done(result=plan)
        except (ValueError, KeyError, OSError, NotionError, FleetSafetyError) as error:
            return Refused(rule='input', reason=str(error))

    def _machine_edit(self, inputs: Any, actor: str) -> Any:
        """`machine edit` previews each row's old and new values, then on confirm writes them and audits each applied row (`t6.fleet`)."""
        from t6.fleet import Fleet, FleetSafetyError, MachineEdit
        from t6.notion import NotionError
        from t6.notion.writes import Ambiguous as WriteAmbiguous, Rejected
        from t6.verbs import MachineEditResult, MachineFactChange
        if not os.environ.get('T6_PREVIEW_SIGNING_KEY'):
            return Refused(rule='config', reason='Set T6_PREVIEW_SIGNING_KEY')
        try:
            source = self.data_sources['bays']
            token = os.environ['T6_NOTION_TOKEN']
            edits = [MachineEdit(names=tuple(machine.names), values=machine.values) for machine in inputs.machines]  # refuses an unlisted property or a wrong type before any read
            values = inputs.model_dump(mode='json', exclude={'confirm'})
            nonce = self.people.tokens.verify(inputs.confirm, 'machine edit', actor, values) if inputs.confirm else None
            if isinstance(nonce, Refused):
                return nonce
            with self.people.registry._transaction() as db:
                if nonce and db.execute('SELECT 1 FROM used_preview_tokens WHERE nonce=?', (nonce,)).fetchone():
                    return Refused(rule='token_used', reason='Preview token has already been used')
                if nonce:
                    db.execute('INSERT INTO used_preview_tokens VALUES (?)', (nonce,))
            results = []
            with self.open_notion(token) as client:
                for found in Fleet(client, source).edit(edits, apply=nonce is not None):
                    written = found.written
                    results.append(MachineEditResult(
                        name=found.name, status=found.status, page_id=found.page_id,
                        changes=[MachineFactChange(fact=change.fact, old=change.old, new=change.new) for change in found.changes],
                        written=None if written is None else 'rejected' if isinstance(written, Rejected) else 'ambiguous' if isinstance(written, WriteAmbiguous) else 'saved',
                        detail=str(written) if isinstance(written, (Rejected, WriteAmbiguous)) else None))
            if nonce is not None:
                with self.people.registry._transaction() as db:
                    for result in results:
                        if result.written == 'saved':
                            _audit(db, result.page_id, 'machine_edit', actor, self.people.registry._clock(),
                                   {change.fact: change.old for change in result.changes}, {change.fact: change.new for change in result.changes})
            plan = {'machines': [result.model_dump(mode='json') for result in results]}
            return self.people.tokens.create('machine edit', actor, values, plan) if nonce is None else Done(result=plan)
        except (ValueError, KeyError, OSError, NotionError, FleetSafetyError) as error:
            return Refused(rule='input', reason=str(error))

    def _camera_enrich(self, inputs: Any, actor: str) -> Any:
        import secrets
        from t6.notion.cameras import get_cameras, validate_update, camera_to_json
        from t6.notion import NotionError
        from t6.notion.generated import CamerasRow
        from t6.notion.writes import update_row, Rejected, Ambiguous as WriteAmbiguous
        from t6.verbs import Ambiguous
        key = inputs.idempotency_key or secrets.token_hex(16)
        if not os.environ.get('T6_PREVIEW_SIGNING_KEY'):
            return Refused(rule='config', reason='Set T6_PREVIEW_SIGNING_KEY')
        if inputs.confirm and not inputs.idempotency_key:
            return Refused(rule='idempotency_key', reason='Supply the key from the preview')
        try:
            fields = validate_update(inputs.fields)
            source = self.data_sources['cameras']
            token = os.environ['T6_NOTION_TOKEN']
            values = {'page_id': inputs.page_id, 'fields': fields, 'idempotency_key': key}
            fingerprint = json.dumps({'verb': 'camera enrich', 'actor': actor, **values}, sort_keys=True, separators=(',', ':'))
            nonce = self.people.tokens.verify(inputs.confirm, 'camera enrich', actor, values) if inputs.confirm else None
            if isinstance(nonce, Refused):
                return nonce
            with self.people.registry._transaction() as db:
                previous = db.execute('SELECT fingerprint, result FROM verb_requests WHERE idempotency_key=?', (key,)).fetchone()
                if previous and previous[0] != fingerprint:
                    return Refused(rule='idempotency_key', reason='Idempotency key was used for a different request')
                if nonce and db.execute('SELECT 1 FROM used_preview_tokens WHERE nonce=?', (nonce,)).fetchone():
                    return Refused(rule='token_used', reason='Preview token has already been used')
                if previous and nonce:
                    db.execute('INSERT INTO used_preview_tokens VALUES (?)', (nonce,))
                    result = json.loads(previous[1])
                    return Ambiguous(candidates=[result]) if result.get('pending') else Done(result=result)
            with self.open_notion(token) as client:
                before = get_cameras(client, source, page_id=inputs.page_id)[0]
                if nonce is None:
                    return self.people.tokens.create('camera enrich', actor, values, {**values, 'before': before})
                with self.people.registry._transaction() as db:
                    db.execute('INSERT INTO used_preview_tokens VALUES (?)', (nonce,))
                    db.execute('INSERT INTO verb_requests VALUES (?, ?, ?)', (key, fingerprint, json.dumps({'pending': True, 'reason': 'Notion write may have happened'})))
                saved = update_row(client, CamerasRow, inputs.page_id, fields)
                if isinstance(saved, (Rejected, WriteAmbiguous)):
                    return Ambiguous(candidates=[{'reason': str(saved)}])
                result = camera_to_json(saved)
                with self.people.registry._transaction() as db:
                    db.execute('UPDATE verb_requests SET result=? WHERE idempotency_key=?', (json.dumps(result), key))
                    _audit(db, inputs.page_id, 'camera_enrich', actor, self.people.registry._clock(), before, fields)
                return Done(result=result)
        except (ValueError, KeyError, OSError, NotionError) as error:
            return Refused(rule='input', reason=str(error))

    def _vendor_person(self, mention: str) -> str | None:
        """The Person an `/abang` mention names: a t6 ID, or an @handle or name found in Contacts."""
        if mention.startswith('t6n_'):
            return mention
        from t6.cli import TOKEN_VARIABLE
        from t6.lookup import Index, Lookup
        from t6.notion import NotionError
        from t6.notion.reads import ContactReader
        from t6.registry.core import normalize_page_id
        token, source = os.environ.get(TOKEN_VARIABLE), self.data_sources.get('contacts')
        if not token or not source:
            return None
        try:
            with self.open_notion(token) as client:
                found = Lookup(Index(contacts=ContactReader(client, source).list())).contact(mention)
        except (NotionError, OSError, ValueError, KeyError):
            return None
        if found.entity is None or found.needs_review:
            return None
        person = self.people.registry.bound_entity('notion', normalize_page_id(found.entity.page_id))
        return person if isinstance(person, str) else None

    def scan_concerns(self) -> int:
        """Scan the PM Notes of every Project in rotation for new Concern evidence; returns how many were scanned."""
        from t6.concern import ConcernStore
        from t6.concern_task import ClassifyRefused, classify
        from t6.registry import Rotation
        from t6.verbs import ProjectNotesInput
        if self.llm_service is None:
            return 0
        scanned, changes = 0, {'raised': 0, 'continued': 0, 'announced': 0}
        for project in self.people.registry.list_projects(Rotation.IN):
            owners = sorted(self._coordinators(project.id))
            notes = self._typed_read('project', 'notes', ProjectNotesInput(project=project.id)) if owners else None
            if not isinstance(notes, Done):
                continue
            owner = owners[0]
            try:
                done = ConcernStore(self.people.registry, self.people.tokens).process_notes(project.id, owner, notes.result['lines'], lambda payload: classify(self.llm_service, payload, actor=owner, registry=self.people.registry))
                scanned += 1
                changes = {name: count + done[name] for name, count in changes.items()}
            except ValueError:
                continue
            except ClassifyRefused as refused:
                print(f't6.server: concern scan stopped: llm run refused rule={refused.rule} projects={scanned}', file=sys.stderr, flush=True)  # rule only: no text
                return scanned
        print(f't6.server: concern scan projects={scanned} ' + ' '.join(f'{name}={count}' for name, count in changes.items()), file=sys.stderr, flush=True)  # counts only: no text
        return scanned

    def _coordinators(self, project_id: str) -> set[str]:
        """The Persons who coordinate a Project: its Notion post coordinators and its t6 coordinator roles."""
        from t6.verbs import ProjectGetInput
        read = self._typed_read('project', 'get', ProjectGetInput(project=project_id))
        if not isinstance(read, Done):
            return set()
        project = read.result['project']
        return set(project.post_coordinator_ids) | {r['person_id'] for r in project.project_roles if r['role'].casefold() in ('coordinator', 'post coordinator', 'post-coordinator')}

    def _typed_read(self, noun: str, verb: str, inputs: Any) -> Any:
        from datetime import date, datetime
        from dataclasses import asdict, replace
        from t6.notion import NotionError
        from t6.notion.reads import ProjectReader, DeliverableReader, is_live
        from t6.notion.job_orders import JobOrders
        from t6.notion.meeting_notes import note_from_page
        from t6.domain.job_order_rules import status_group, is_active, is_done, health_flags
        from t6.domain import project_rules
        from t6.registry import ProjectRefs
        from t6.registry.core import normalize_page_id
        from t6.cli import TOKEN_VARIABLE
        if (noun, verb) == ('concern', 'list'):
            from t6.concern import ConcernStore
            if inputs.raise_at_psr == inputs.open:
                return Refused(rule='input', reason='Pass exactly one of raise_at_psr and open')
            concerns = ConcernStore(self.people.registry, self.people.tokens)
            return Done(result={'concerns': concerns.open_concerns(inputs.project) if inputs.open else concerns.raise_at_psr(inputs.project)})
        if (noun, verb) == ('concern', 'get'):
            from t6.concern import ConcernStore
            record = ConcernStore(self.people.registry, self.people.tokens).get(inputs.concern)
            return Done(result={'concern': record}) if record else Refused(rule='concern', reason='Concern not found')
        if noun == 'standing-cc':
            from t6.domain.studio_people import standing_cc
            page = standing_cc()
            return Done(result={'standing_cc': (self.people.registry.bound_entity('notion', page) or page) if page else None})
        if noun == 'version':
            from t6.version import VersionStore
            versions = VersionStore(self.people.registry, self.people.tokens)
            if verb == 'get':
                record = versions.get(inputs.version)
                return Done(result={'version': record}) if record else Refused(rule='version', reason='Version not found')
            return Done(result={'version': versions.for_file(inputs.frameio_file)})
        if noun == 'deliverable' and verb != 'candidates':
            from t6.deliverable import DeliverableStore
            store = DeliverableStore(self.people.registry, self.people.tokens)
            if verb == 'get':
                record = store.get(inputs.ref)
                return Done(result={'deliverable': record}) if record else Refused(rule='deliverable', reason='Deliverable not found')
            return Done(result={'deliverables': store.list(inputs.project)})
        if noun == 'external-link':
            from t6.media import external_link_result
            return Done(result={'link': external_link_result(self.people.registry, inputs.system, inputs.kind, inputs.external_id)})
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
            with self.people.registry._transaction() as db:
                roles = tuple(dict(person_id=r[0], role=r[1]) for r in db.execute('SELECT person_id, role FROM project_person_roles WHERE project_id = ? ORDER BY person_id, role', (project.t6_id,)))
            return replace(project, project_roles=roles, editor_ids=ids(project.editor_ids),
                           assistant_editor_ids=ids(project.assistant_editor_ids),
                           online_editor_ids=ids(project.online_editor_ids), colorist_ids=ids(project.colorist_ids),
                           sound_tech_ids=ids(project.sound_tech_ids), post_coordinator_ids=ids(project.post_coordinator_ids),
                           director_ids=ids(project.director_ids), cinematographer_ids=ids(project.cinematographer_ids),
                           prod_coor_supe_ids=ids(project.prod_coor_supe_ids))
        def read_project(project: Any) -> Any:
            from dataclasses import fields
            from t6.verbs import ReadProject
            group = project_rules.status_gate(project.status)
            return ReadProject(**{f.name: getattr(project, f.name) for f in fields(project)},
                               status_key=project_rules.status_key(project.status), status_group=group.value if group else None,
                               snapshotted=project_rules.is_snapshotted(project.status), missing_roles=project_rules.missing_roles(project),
                               relevant_dates=project_rules.relevant_dates(project.status), health_missing=project_rules.health_missing(project),
                               health_rank=project_rules.health_rank(project.project_health))
        def order_result(order: Any) -> dict[str, Any]:
            item = asdict(order)
            group = status_group(order.status)
            item.update(status_group=group.value if group else None, active=is_active(order.status), done=is_done(order.status), health_flags=list(health_flags(order.health)))
            item['t6_id'] = self.people.registry.bound_entity('notion', order.page_id)
            item['project_id'] = refs.identity(order.project_id) or order.project_id
            item['project_ids'] = [refs.identity(page) or page for page in order.project_ids]
            for field in ('assigned_ids', 'cc_ids', 'coordinator_ids', 'deliverable_ids'):
                item[field] = [identity(page) for page in item[field]]
            return item
        try:
            with self.open_notion(token) as client:
                if noun == 'person':
                    from t6.notion.reads import ContactReader
                    from t6.cli_studio import _find_contacts
                    from uuid import UUID
                    contact_reader = ContactReader(client, self.data_sources['contacts'])
                    def contact(c: Any) -> dict[str, Any]:
                        item = asdict(c)
                        item['t6_id'] = self.people.registry.bound_entity('notion', c.page_id)
                        return item
                    if verb == 'list':
                        people = [contact(c) for c in contact_reader.list()]
                        kinds: dict[str, str] = {}
                        if inputs.kind:
                            with self.people.registry._transaction() as db:
                                kinds = dict(db.execute('SELECT id, kind FROM persons').fetchall())
                        return Done(result={'people': [c for c in people if (inputs.registered is None or (c['t6_id'] is not None) == inputs.registered)
                                                       and (inputs.kind is None or kinds.get(c['t6_id']) == inputs.kind)]})
                    if verb == 'find':
                        try:
                            return Done(result=_find_contacts(contact_reader.list(), contact, inputs.mention, inputs.mentions, inputs.suggest, inputs.prefer_telegram))
                        except ValueError as error:
                            return Refused(rule='input', reason=str(error))
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
                        item.update({key: person[key] for key in ('kind', 'departments', 'roles', 'project_roles', 'shift', 'permissions', 'master_brief', 'active')})
                    return Done(result={'person': item})
                if noun == 'studio':
                    source = client.retrieve_data_source(self.data_sources['projects'])
                    runs = [{'text': part.get('plain_text', (part.get('text') or {}).get('content', '')),
                             'bold': bool((part.get('annotations') or {}).get('bold'))}
                            for part in source.get('description') or []]
                    return Done(result={'description': ''.join(run['text'] for run in runs), 'runs': runs})
                if noun == 'departments':
                    from t6.notion.departments import get_departments
                    return Done(result={'departments': [{'id': d.option_id, 'name': d.name} for d in get_departments(client, self.data_sources['job_orders'], include_misc=inputs.include_misc)]})
                if noun == 'camera':
                    from t6.notion.cameras import get_cameras
                    cameras = get_cameras(client, self.data_sources['cameras'], page_id=inputs.page_id)
                    return Done(result={'cameras': [{'page_id': c['id'], 'name': c['name'] or '', 'tags': c['tags']} for c in cameras]})
                if noun == 'project':
                    projects = [project_result(p) for p in ProjectReader(client, self.data_sources['projects']).list()]
                    def flags_of(project: Any, stated_dates: Mapping[str, Any]) -> dict[str, Any]:
                        from dataclasses import replace
                        from t6.domain.project_rules import date_flags, date_sanity, health_missing, missing_roles
                        from t6.domain.studio_time import MANILA
                        today = inputs.as_of or datetime.fromtimestamp(self.people.registry._clock(), MANILA).date()
                        stated = {name: day for name, day in stated_dates.items() if getattr(project, name) is None}
                        judged = replace(project, **{name: day for name, day in stated.items() if day is not None})
                        flags = [{'kind': 'data_gap' if f.kind == 'missing' else f.kind, 'field': f.field, 'detail': f.kind, **({'days': f.days} if f.days is not None else {})}
                                 for f in date_flags(judged, today, inputs.lead_warning_days) if not (f.kind == 'missing' and f.field in stated)]
                        if health_missing(project):
                            flags.append({'kind': 'data_gap', 'field': 'health', 'detail': 'missing health update'})
                        sanity = [{'kind': kind, 'first': first, 'second': second, 'first_date': first_date, 'second_date': second_date,
                                   'from_notes': stated.get(first) is not None or stated.get(second) is not None}
                                  for kind, first, second, first_date, second_date in date_sanity({name: getattr(judged, name) for name in ('playdate', 'submission_deadline', 'archival_deadline')})]
                        return {'flags': flags, 'missing_roles': list(missing_roles(project)), 'sanity': sanity}
                    if verb == 'flags' and inputs.projects:
                        by_page = {p.page_id.replace('-', '').lower(): p for p in projects}
                        several = []
                        for item in inputs.projects:
                            pages = refs.pages(item.project)
                            found = by_page.get((pages[0] if pages else item.project).replace('-', '').lower())
                            several.append({'project': item.project, 'found': True, **flags_of(found, item.stated)} if found else {'project': item.project, 'found': False})
                        return Done(result={'projects': several})
                    if verb in ('get', 'notes', 'flags'):
                        pages = refs.pages(inputs.project)
                        active = (pages[0] if pages else inputs.project).replace('-', '').lower()
                        project = next((p for p in projects if p.page_id.replace('-', '').lower() == active), None)
                        if verb == 'notes' and project:
                            from t6.pm_notes import dated_lines
                            from t6.domain.studio_time import MANILA
                            return Done(result={'lines': dated_lines(project.pm_notes, datetime.fromtimestamp(self.people.registry._clock(), MANILA).date())})
                        if verb == 'flags' and project:
                            return Done(result=flags_of(project, inputs.stated))
                        return Done(result={'project': read_project(project)}) if project else Refused(rule='project', reason='Project not found')
                    if verb == 'resolve':
                        from t6.lookup import Index, Lookup
                        from t6.cli_studio import _resolution
                        lookup = Lookup(Index(projects=tuple(projects)))
                        if inputs.in_text:
                            found = lookup.project_in_text(inputs.text)
                        else:
                            found = lookup.project(inputs.text)
                            if found.entity is None and not found.candidates:
                                found = lookup.project(None, name=inputs.text)
                        return Done(result=_resolution(found, asdict))
                    return Done(result={'projects': [read_project(p) for p in projects if (inputs.rotation is None or p.rotation == inputs.rotation)
                                and (inputs.status is None or p.status == inputs.status)
                                and (inputs.status_group is None or getattr(project_rules.status_gate(p.status), 'value', None) == inputs.status_group)]})
                if noun == 'jo':
                    jo_reader = JobOrders(client, self.data_sources['job_orders'])
                    if verb == 'get':
                        from t6.job_order import JobOrderStore
                        from t6.preview import PreviewTokens
                        from uuid import UUID
                        record = JobOrderStore(self.people.registry, PreviewTokens('read-only')).get(inputs.ref)
                        if record:
                            page = next((b['page_id'] for b in record['bindings'] if b['active']), None)
                            if page is None:
                                # No Notion page yet (the poll after `jo raise`): the rule results come from the stored status.
                                status = record['state']['status']
                                group = status_group(status)
                                return Done(result={'job_order': {
                                    **record['fields'], **record, 't6_id': record['id'],
                                    'status_group': group.value if group else None, 'active': is_active(status), 'done': is_done(status),
                                    'health_flags': list(health_flags(record['fields'].get('health'))),
                                    'project_ids': [record['project_id']] if record['project_id'] else []}})
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
                        result = order_result(order)
                        if record:
                            result['notion_notes'] = result.get('notes')
                            result.update(id=record['id'], t6_id=record['id'], page_id=page,
                                          assignee_id=record['assignee_id'], notes=record['notes'],
                                          state=record['state'], history=record['history'])
                        return Done(result={'job_order': result})
                    if verb == 'find':
                        from t6.job_order import find_forms
                        located = find_forms(jo_reader, self.people.registry, inputs)
                        if isinstance(located, Refused):
                            return located
                        orders = located
                    else:
                        orders = jo_reader.list(oldest_first=bool(inputs.no_project))
                    if verb == 'list':
                        if inputs.no_project and inputs.project:
                            return Refused(rule='input', reason='Give project or no_project, not both')
                        orders = tuple(o for o in orders if (not inputs.project or same_project(o.project_id, inputs.project))
                            and (not inputs.no_project or not o.project_id)
                            and (not inputs.assignee or any(normalize_page_id(identity(p)) == normalize_page_id(identity(inputs.assignee)) for p in o.assigned_ids))
                            and (not inputs.status_group or (is_active(o.status) if inputs.status_group == 'active' else status_group(o.status) == inputs.status_group)))
                    items = []
                    for order in orders:
                        items.append(order_result(order))
                    return Done(result={'job_orders': items})
                if noun == 'deliverable':
                    pages = refs.pages(inputs.project)
                    known = {d.page_id: d for page in pages for d in DeliverableReader(client, self.data_sources['deliverables']).for_project(page)}
                    return Done(result={'candidates': [{'page_id': d.page_id, 'name': d.name, 'status': d.status, 'deliverable_class': d.deliverable_class} for d in known.values()]})
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
                if inputs.latest:
                    def meeting_day(n: Any) -> Any:
                        return n.meeting_date.date() if isinstance(n.meeting_date, datetime) else n.meeting_date
                    dated = [n for n in notes if meeting_day(n) is not None]
                    notes = [max(dated, key=lambda n: (meeting_day(n), n.last_edited.timestamp() if n.last_edited else 0))] if dated else []
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
                    item['body'] = blocks(note.page_id) if inputs.include_body else []
                    items.append(item)
                return Done(result={'meeting_notes': items})
        except (KeyError, ValueError, OSError, NotionError):
            return Refused(rule='read', reason='Read failed; check the input and server configuration')

    def _legacy_read(self, noun: str, verb: str, inputs: Any) -> Any:
        # The legacy write commands do not have signed previews yet.
        if (noun, verb) in (('camera', 'enrich'), ('person', 'create')):
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
                    result = _contacts_find(client, sources['contacts'], inputs.mention, inputs.mentions, inputs.suggest, inputs.prefer_telegram)
                    for found in result.get('results', [result]):
                        for item in ([found['match']] if found['match'] else []) + found['candidates']:
                            item['t6_id'] = self.people.registry.bound_entity('notion', item['id'])
                elif (noun, verb) == ('project', 'resolve'):
                    result = _projects_resolve(client, sources['projects'], inputs.text,
                                               inputs.registry_db or Path(os.environ.get('T6_REGISTRY_DB', 't6-registry.sqlite')), inputs.in_text)
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

    def _refused_command(self, chat: object, topic: object, key: object, text: str) -> Response:
        """Answer a refused command in its chat and topic with one job per pushed command; a push that a real gateway cannot send gets no reply."""
        if self.tap_app is not None and type(chat) is int and (topic is None or type(topic) is int) and isinstance(key, str) and key:
            self.tap_app._inside.submit(MAIN_QUEUE, JobKind.CHAT_REPLY.value, f'chat.reply:{key}', {'chat': chat, 'topic': topic, 'text': text, 'key': key})
        return Response(200, {'outcome': 'rejected'})

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
        if body.get('kind') == 'command' and body.get('command') == 'abang':
            # Typed in the source chat, where the freelancer is. The coordinator then taps a destination.
            args, chat, topic = body.get('args'), body.get('chat'), body.get('topic')
            key = request.header('idempotency-key') or body.get('idempotency_key')
            if self.people.authorize(person[0], 'coordinator'):
                return self._refused_command(chat, topic, key, 'Only a coordinator can use /abang.')
            if not isinstance(args, str) or not args.strip() or type(chat) is not int or (topic is not None and type(topic) is not int) or not isinstance(key, str) or not key:
                return self._refused_command(chat, topic, key, 'Usage: /abang @person note')
            mention, _, note = args.strip().partition(' ')
            vendor = self._vendor_person(mention)
            if vendor is None:
                return self._refused_command(chat, topic, key, 'No Person matches that mention.')
            from t6.abang import start_draft
            return Response(200, {'outcome': 'accepted' if isinstance(start_draft(self.people.registry, person[0], vendor, chat, topic, note.strip() or None, key), Done) else 'rejected'})
        if body.get('kind') == 'command' and body.get('command') in ('link', 'unlink'):
            args = body.get('args')
            chat, topic = body.get('chat'), body.get('topic')
            key = request.header('idempotency-key') or body.get('idempotency_key')
            if self.people.authorize(person[0], 'coordinator'):
                return self._refused_command(chat, topic, key, f"Only a coordinator can use /{body['command']}.")
            if isinstance(args, str) and not args.strip() and body['command'] == 'link' and type(chat) is int and type(topic) is int:
                import re
                with self.people.registry._transaction() as db:
                    row = db.execute('SELECT name FROM linked_chat_topics WHERE chat_id = ? AND topic_id = ?', (chat, topic)).fetchone()
                match = re.match(r'^\s*([A-Za-z0-9]+)\s+-\s+', row[0]) if row else None
                if match:
                    args = match.group(1)
            if not isinstance(args, str) or len(args.split()) != 1 or type(chat) is not int or (topic is not None and type(topic) is not int) or not isinstance(key, str) or not key:
                return self._refused_command(chat, topic, key, f"Usage: /{body['command']} CODE")
            entry = self.routes['/v1/project/' + ('link-chat' if body['command'] == 'link' else 'unlink-chat')]
            response = self._dispatch(entry, {'project': args.strip(), 'chat': chat, 'topic': topic, 'idempotency_key': key}, person[0])
            if response.body.get('outcome') in ('preview', 'done'):
                return Response(200, {'outcome': 'accepted'})
            return self._refused_command(chat, topic, key, f"/{body['command']} {args.strip()}: {response.body.get('reason') or 'refused'}")
        data = body.get('data')
        if isinstance(data, str) and data.startswith('abang-dest:'):
            parts = data.split(':')
            key = request.header('idempotency-key') or body.get('idempotency_key')
            with self.people.registry._transaction() as db:
                draft = db.execute("SELECT d.id, d.actor, d.person_id, d.source_chat, d.source_topic, d.note, o.chat, o.topic FROM abang_drafts d JOIN abang_draft_options o ON o.draft_id = d.id WHERE d.id = ? AND o.n = ? AND d.state = 'open'", (parts[1], parts[2] if len(parts) == 3 and parts[2].isdigit() else -1)).fetchone() if len(parts) == 3 else None
                if draft is None or draft[1] != person[0] or not isinstance(key, str) or not key:
                    _audit(db, person[0], 'tap_rejected', person[0], self.people.registry._clock(), None, {'reason': 'not_allowed', 'data': data})
                    return Response(200, {'outcome': 'rejected'})
            values: dict[str, JsonValue] = {'person': draft[2], 'source': str(draft[3]) + (f'/{draft[4]}' if draft[4] is not None else ''), 'destination': str(draft[6]) + (f'/{draft[7]}' if draft[7] is not None else ''), 'note': draft[5], 'idempotency_key': key}
            entry = self.routes['/v1/abang/request']
            response = self._dispatch(entry, values, person[0])
            if response.body.get('outcome') == 'preview':
                values['confirm'] = response.body['token']
                response = self._dispatch(entry, values, person[0])
            done = response.body.get('outcome') == 'done'
            if done:
                with self.people.registry._transaction() as db:
                    db.execute("UPDATE abang_drafts SET state = 'used' WHERE id = ?", (draft[0],))
                    _audit(db, person[0], 'tap_accepted', person[0], self.people.registry._clock(), None, {'key': key, 'data': data, 'outcome': 'accepted'})
            return Response(200, {'outcome': 'accepted' if done else 'rejected'})
        if isinstance(data, str) and data.startswith('abang:'):
            parts = data.split(':')
            candidate = parts[1] if len(parts) == 3 else ''
            decision = parts[2] if len(parts) == 3 else ''
            with self.people.registry._transaction() as db:
                row = db.execute("SELECT r.person_id FROM abang_candidates c JOIN abang_requests r ON r.id = c.request_id WHERE c.id = ?", (candidate,)).fetchone()
                allowed = bool(row and self.people._allows(db, person[0], 'person:' + row[0]))
                if not allowed:
                    _audit(db, person[0], 'tap_rejected', person[0], self.people.registry._clock(), None, {'reason': 'not_allowed', 'data': data})
            if not allowed:
                return Response(200, {'outcome': 'rejected'})
            key = request.header('idempotency-key') or body.get('idempotency_key')
            if not isinstance(key, str) or not key or decision not in ('yes', 'no'):
                return Response(200, {'outcome': 'rejected'})
            values = {'candidate': candidate, 'decision': decision, 'idempotency_key': key}
            entry = self.routes['/v1/abang/accept']
            response = self._dispatch(entry, values, person[0])
            if response.body.get('outcome') == 'preview':
                values['confirm'] = response.body['token']
                response = self._dispatch(entry, values, person[0])
            decided = 'accepted' if response.body.get('outcome') == 'done' else 'rejected'
            if decided == 'accepted':
                with self.people.registry._transaction() as db:
                    _audit(db, person[0], 'tap_accepted', person[0], self.people.registry._clock(), None, {'key': key, 'data': data, 'outcome': decided})
            return Response(200, {'outcome': decided})
        if body.get('kind') != 'command' and (not isinstance(data, str) or not data.startswith('op:')):
            action, separator, target = data.partition(':') if isinstance(data, str) else ('', '', '')
            if action not in {'approve', 'retry'} or not separator or not target or not self.people.allows_telegram(telegram_id, 'permission:operator'):
                with self.people.registry._transaction() as db:
                    _audit(db, person[0], 'tap_rejected', person[0], self.people.registry._clock(), None, {'reason': 'not_allowed', 'data': data})
                return Response(200, {'outcome': 'rejected'})
        card_action = None
        if isinstance(data, str) and data.startswith('op:') and self.tap_app._operator:
            card_action = self.tap_app._operator.store.get(data[3:].partition(':')[0])
        response = self.tap_app._tap(request, authenticated=True)
        outcome = response.body.get('outcome')
        if response.status == 200 and outcome == 'granted' and card_action and card_action.kind == 'approval':
            from t6.chat_link import ChatLinks
            with self.people.registry._transaction() as db:
                rows = db.execute("SELECT p.request FROM chat_link_previews p JOIN approvals a ON a.id = p.approval_id WHERE a.state = 'granted' AND a.id = ? AND p.nonce NOT IN (SELECT nonce FROM used_preview_tokens)", (card_action.params['approval_id'],)).fetchall()
            for row in rows:
                pending = json.loads(row[0])
                if not self.people.authorize(pending['actor'], 'coordinator'):
                    ChatLinks(self.people.registry, self.people.tokens).write(**pending)
        if response.status == 200 and outcome is not None and outcome not in {'rejected', 'ignored', 'already_handled', 'busy', 'expired'}:
            key = request.header('idempotency-key') or body.get('idempotency_key')
            with self.people.registry._transaction() as db:
                recorded = db.execute("SELECT 1 FROM audit WHERE action = 'tap_accepted' AND json_extract(after, '$.key') = ?", (key,)).fetchone()
                if not recorded:
                    _audit(db, person[0], 'tap_accepted', person[0], self.people.registry._clock(), None,
                           {'key': key, 'data': body.get('data'), 'outcome': outcome})
        return response


_FIXED_PATHS = {'/tap': 'tap', '/v1/commands': 'commands', '/v1/admin/media/migrate': 'admin.media.migrate'}


def request_line(path: str, identity: str | None, response: Response, seconds: float, routes: Mapping[str, Any]) -> str:
    """The request log line: `t6.server: request time=<UTC> identity=<service or T6 Person ID> verb=<noun.verb> outcome=<kind> ms=<n>`.

    `outcome` is `done`, `preview`, `refused:<rule>` or `error:<HTTP status>`. The line holds no input,
    result, secret, name or path: the verb comes from the route table, never from the request text.
    """
    entry = routes.get(path)
    verb = '.'.join(part for part in (entry.noun, entry.verb.replace(' ', '.')) if part) if entry else \
        'transcribe' if path.startswith('/v1/transcribe/') else _FIXED_PATHS.get(path, 'unknown')
    body = response.body if isinstance(response.body, Mapping) else {}
    kind = body.get('outcome')
    if response.status == 401:
        outcome = 'refused:unauthorized'
    elif response.status == 200 and kind in {None, 'done', 'preview'}:
        outcome = str(kind or 'done')
    elif response.status == 200 and kind == 'refused':
        outcome = 'refused:' + (re.fullmatch(r'[a-z0-9_-]{1,40}', str(body.get('rule'))) or ['other'])[0]
    else:
        outcome = f'error:{response.status}'
    stamp = datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
    return f't6.server: request time={stamp} identity={identity or "-"} verb={verb} outcome={outcome} ms={round(seconds * 1000)}'


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


def _read_privacy_names(path: str) -> PrivacyRegistry:
    from t6.privacy import Person
    with open(path, encoding='utf-8') as file:
        names = json.load(file)
    if not isinstance(names, list) or any(not isinstance(name, str) or not name.strip() for name in names):
        raise ValueError('T6_READ_PRIVACY_REGISTRY must contain a JSON array of client names')
    return PrivacyRegistry(people=[Person(name=name) for name in names])


class PrivacyList:
    """The client-name file of masked reads, read again when it changes (no restart).

    A file that cannot be read (missing, bad JSON, wrong shape) keeps the last good list and logs one
    line for that state of the file; the next change is tried again. `status` is shown by `/health`.
    """

    def __init__(self, path: str, registry: PrivacyRegistry, log: Callable[[str], None] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.path, self._registry, self._log, self._clock = path, registry, log, clock
        self._lock = threading.Lock()
        self._seen: tuple[int, int] | None = None  # (mtime_ns, size) of the file last tried
        self._loaded: tuple[int, int] | None = None  # the same, of the list in use
        self._failed = False
        self._names = len(registry.people)
        try:
            self._seen = self._loaded = self._stamp()
        except OSError:
            pass

    def _stamp(self) -> tuple[int, int]:
        info = os.stat(self.path)
        return info.st_mtime_ns, info.st_size

    def current(self) -> PrivacyRegistry:
        with self._lock:
            stamp: tuple[int, int] | None = None
            failure: str | None = None
            try:
                stamp = self._stamp()
            except OSError as error:
                failure = type(error).__name__
            if stamp != self._seen:
                self._seen = stamp
                if stamp is not None:
                    try:
                        self._registry = _read_privacy_names(self.path)
                        self._loaded, self._failed, self._names = stamp, False, len(self._registry.people)
                        return self._registry
                    except (OSError, ValueError) as error:  # JSONDecodeError is a ValueError
                        failure = type(error).__name__
                self._failed = True
                if self._log is not None:
                    self._log(f't6.server: read privacy registry not reloaded ({failure}); the last good list stays in use')
            return self._registry

    def status(self) -> dict[str, Any]:
        """How many names are in use, how old the file they came from is, and whether a later file was refused."""
        self.current()
        with self._lock:
            age = None if self._loaded is None else max(0, round(self._clock() - self._loaded[0] / 1e9))
            return {'names': self._names, 'age_seconds': age, 'reload_failed': self._failed}


def read_config_from_env(environ: Mapping[str, str]) -> tuple[dict[str, str], PrivacyRegistry | None]:
    """Load API-owned Notion source IDs and client-name masking terms."""
    from t6.cli import _read_data_sources
    source_path = environ.get('T6_READ_DATA_SOURCES')
    privacy_path = environ.get('T6_READ_PRIVACY_REGISTRY')
    sources = _read_data_sources(Path(source_path)) if source_path else {}
    if not privacy_path:
        return sources, None
    return sources, _read_privacy_names(privacy_path)
