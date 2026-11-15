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
from t6.verbs import VERBS, Done, Preview, Refused, catalogue, invoke


class VerbAPI:
    def __init__(self, people: People, queue: Any, secrets: Mapping[str, str],
                 open_notion: Callable[..., Any], tap_app: WebhookApp | None = None,
                 *, llm_service: Any = None, data_sources: Mapping[str, str] | None = None,
                 privacy_registry: PrivacyRegistry | None = None, transcribe_rate: float = 0.4,
                 translate_enabled: bool = False) -> None:
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
        self.privacy_registry = privacy_registry
        self.transcribe_rate = transcribe_rate
        self.translate_enabled = translate_enabled
        self.routes = {f'/v1/{v.noun}' + ('/' + v.verb.replace(' ', '/') if v.verb else ''): v for v in VERBS}
        for verb in VERBS:
            if verb.noun == 'approval':
                for alias in verb.aliases:
                    self.routes['/v1/' + alias.replace(' ', '/')] = verb

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
                if not token or not signing or not self.data_sources.get('external_links'):
                    return self.refused('config', 'Notion and preview configuration are required')
                with self.open_notion(token) as client:
                    rows = read_external_links_once_for_migration(client, self.data_sources['external_links'])
                result = MediaStore(self.people.registry, self.people.tokens).migrate_write(
                    rows, actor=actor, idempotency_key=key or inputs.idempotency_key, confirm=inputs.confirm)
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
        """Mask unless the secret is bound to an operator Person; the `mcp` service is always masked."""
        if actor in SERVICES:
            return actor == 'mcp' or mcp_header
        return mcp_header and self.people.authorize(actor, 'operator') is not None

    def _mask_mcp(self, response: Response) -> Response:
        if self.privacy_registry is None:
            return self.refused('masking', 'Read privacy registry is not configured')
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
            return Response(response.status, mask(response.body))
        except ValueError:
            return self.refused('masking', 'A client name occurs in an identifier')

    def _dispatch(self, entry: Any, data: dict[str, Any], actor: str) -> Response:
        outcome: Any
        try:
            inputs = entry.input_model.model_validate(data)
        except ValidationError as error:
            return Response(422, {'errors': cast(JsonValue, error.errors(include_url=False, include_context=False, include_input=False))})
        if (entry.noun, entry.verb) == ('transcript', 'get') and inputs.transcript.isdecimal():
            person = self.people.get(actor)
            if not person or not person['active']:
                return self.refused('person', 'An active Person is required')
            result = self._transcribe_route('/v1/transcribe/result', Request('POST', '/v1/transcribe/result', body=json.dumps({'job_id': int(inputs.transcript)}).encode()), actor)
            if result.status != 200:
                return self.refused('job', 'Job not found')
            value = result.body.get('result')
            if value is None:
                return Response(200, Done(result={'job_id': int(inputs.transcript), 'state': 'pending'}).model_dump(mode='json'))
            return Response(200, cast(Mapping[str, JsonValue], value))
        if (entry.noun, entry.verb) in {('transcript', 'create'), ('transcript', 'speakers'), ('transcript', 'translate')}:
            refusal = self.people.authorize(actor, entry.role)
            if refusal:
                outcome = refusal
            elif entry.verb == 'translate' and not self.translate_enabled:
                outcome = Refused(rule='translate_disabled', reason='Transcript translation is disabled')
            else:
                from t6.transcript import TranscriptStore
                transcripts, values = TranscriptStore(self.people.registry), inputs.model_dump(exclude={'idempotency_key'})
                outcome = transcripts.replay(entry.verb, actor, values, inputs.idempotency_key)
                if outcome is None:
                    outcome = {'translate': self._transcript_translate, 'create': self._transcript_create, 'speakers': self._transcript_speakers}[entry.verb](inputs, actor)
                    transcripts.remember(entry.verb, actor, values, inputs.idempotency_key, outcome)
        elif (entry.noun, entry.verb) in {('project', 'list'), ('project', 'get'), ('project', 'flags'), ('project', 'resolve'), ('jo', 'list'), ('jo', 'find'), ('jo', 'get'), ('person', 'get'), ('person', 'find'), ('person', 'list'), ('camera', 'get'), ('departments', ''), ('standing-cc', ''), ('external-link', 'find'), ('studio', 'notices'), ('deliverable', 'list'), ('deliverable', 'get'), ('meeting-notes', 'list'), ('version', 'find'), ('version', 'get')}:
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
        elif (entry.noun, entry.verb) == ('llm', 'run'):
            refusal = self.people.authorize(actor, entry.role)
            outcome = refusal or (self.llm_service.run(inputs, actor=actor, registry=self.people.registry) if self.llm_service else Refused(rule='config', reason='LLM service is not configured'))
        elif (entry.noun, entry.verb) == ('camera', 'enrich'):
            refusal = self.people.authorize(actor, entry.role)
            outcome = refusal or self._camera_enrich(inputs, actor)
        elif entry.handler is None:
            refusal = self.people.authorize(actor, entry.role)
            outcome = refusal or self._legacy_read(entry.noun, entry.verb, inputs)
        else:
            outcome = invoke(entry, inputs, actor=actor, people=self.people, open_notion=self.open_notion)
        if entry.noun == 'project' and entry.verb in ('link-chat', 'unlink-chat') and isinstance(outcome, Preview):
            if self.tap_app and self.tap_app._operator and outcome.plan.get('approval_id'):
                from t6.approval import Approvals
                approval = Approvals(self.people.registry, self.people.tokens).get(outcome.plan['approval_id'])
                if approval:
                    action, _ = self.tap_app._operator.offer_approval(approval)
                    self.tap_app._deliver(action.token, action.version)
        if (entry.noun, entry.verb) == ('frameio', 'project link') and isinstance(outcome, Done) and self.tap_app:
            self.tap_app.release_held_activity()  # review activity that waited for this Frame.io project goes out
        if (entry.noun, entry.verb) == ('project', 'start') and isinstance(outcome, Done) and outcome.result.get('approval_id') and self.tap_app:
            self.tap_app._redeliver_cards()  # the Approval the Start created gets its card
        return Response(200, outcome.model_dump(mode='json'))

    def _transcribe_route(self, path: str, request: Request, actor: str) -> Response:
        import math
        from t6.worker.internal_queue import InternalJobQueue
        if request.method != 'POST':
            return Response(405)
        try:
            body = json.loads(request.body)
            if not isinstance(body, dict):
                raise ValueError('Expected an object')
            action = path.removeprefix('/v1/transcribe/')
            if action == 'claim':
                if actor != 'transcribe':
                    return Response(403)
                queue = InternalJobQueue(self.people.registry, clock=self.people.registry._clock)
                claim = queue.claim('transcribe', 300)
                from t6.transcript import TranscriptStore
                TranscriptStore(self.people.registry).fail_dead_runs()
                job = None if claim is None else {'job_id': claim.job_id, 'kind': claim.kind, 'payload': dict(claim.payload), 'dedupe_key': claim.dedupe_key, 'token': claim.token, 'attempt': claim.attempt}
                return Response(200, {'job': cast(JsonValue, job)})
            if action == 'llm':
                if actor != 'transcribe' or not self.translate_enabled or not self.llm_service:
                    return Response(403)
                with self.people.registry._transaction() as db:
                    row = db.execute("SELECT 1 FROM internal_jobs WHERE id=? AND kind='transcript.translate.run' AND state='claimed' AND lease_token=? AND lease_until>?", (body.get('job_id'), body.get('token'), self.people.registry._clock())).fetchone()
                if not row or not isinstance(body.get('text'), str):
                    return Response(403)
                from t6.transcribe.translate import task_input
                outcome = self.llm_service.run(task_input(body['text']), actor=actor, registry=self.people.registry)
                return Response(200, outcome.model_dump(mode='json'))
            job_id = body.get('job_id')
            if not isinstance(job_id, int):
                return Response(400, {'error': 'job_id required'})
            with self.people.registry._transaction() as db:
                row = db.execute("SELECT kind, payload, state, lease_token, lease_until, result FROM internal_jobs WHERE id=? AND queue='transcribe'", (job_id,)).fetchone()
                if not row:
                    return Response(404)
                payload = json.loads(row[1])
                if action == 'result':
                    if actor != payload['actor']:
                        return Response(403)
                    return Response(200, {'result': cast(JsonValue, json.loads(row[5]) if row[5] else None)})
                if actor != 'transcribe':
                    return Response(403)
                if row[2] != 'claimed' or row[3] != body.get('token') or row[4] <= self.people.registry._clock():
                    return Response(409, {'error': 'claim lost'})
                if action == 'extend':
                    db.execute('UPDATE internal_jobs SET lease_until=? WHERE id=?', (self.people.registry._clock() + 300, job_id))
                    return Response(200, {'accepted': True})
                if action == 'fail':
                    if row[0] == 'transcript.translate.run':
                        from t6.transcript import TranscriptStore
                        TranscriptStore(self.people.registry).fail_translation(db, payload['transcript'], actor=payload['actor'])
                    if row[0] == 'transcript.run':
                        identifier = payload['transcript_id']
                        project = db.execute('SELECT project_id FROM transcripts WHERE id=?', (identifier,)).fetchone()
                        db.execute("UPDATE transcripts SET state='failed' WHERE id=?", (identifier,))
                        from t6.transcript import TranscriptStore
                        TranscriptStore(self.people.registry)._entry(db, identifier, project[0], 'failed', payload['actor'])
                    db.execute("UPDATE internal_jobs SET state='failed', result=?, lease_token=NULL, lease_until=NULL WHERE id=?", (json.dumps({'outcome': 'refused', 'rule': 'transcribe', 'reason': 'Transcription job failed'}), job_id))
                    return Response(200, {'accepted': True})
                kind, data = body.get('kind'), body.get('data')
                if action != 'report' or not isinstance(data, dict):
                    return Response(400)
                if row[0] in ('transcript.preview', 'transcript.speakers.preview', 'transcript.translate.preview') and kind == 'failed':
                    from t6.transcript import failure_refusal
                    refusal = failure_refusal(row[0], data)
                    if refusal is None:
                        return Response(400)
                    db.execute("UPDATE internal_jobs SET state='done', result=?, lease_token=NULL, lease_until=NULL WHERE id=?", (json.dumps(refusal), job_id))
                    return Response(200, {'accepted': True})
                if row[0] == 'transcript.translate.preview' and kind == 'preview':
                    if set(data) != {'clips', 'cue_count', 'estimated_tokens', 'snapshot'} or not isinstance(data['clips'], list) or not all(isinstance(x, str) for x in data['clips']) or any(type(data[k]) is not int or data[k] < 0 for k in ('cue_count', 'estimated_tokens')) or not isinstance(data['snapshot'], str) or len(data['snapshot']) != 64:
                        return Response(400)
                    values = {'transcript': payload['transcript']}
                    preview = self.people.tokens.create('transcript translate', payload['actor'], values, {k: data[k] for k in ('clips', 'cue_count', 'estimated_tokens')})
                    nonce = self.people.tokens.verify(preview.token, 'transcript translate', payload['actor'], values)
                    if not isinstance(nonce, str):
                        return Response(400)
                    db.execute('INSERT INTO transcript_previews VALUES (?, ?)', (nonce, json.dumps({'snapshot': data['snapshot'], 'audio_folder': payload['audio_folder']})))
                    db.execute("UPDATE internal_jobs SET state='done', result=?, lease_token=NULL, lease_until=NULL WHERE id=?", (json.dumps(preview.model_dump(mode='json')), job_id))
                    return Response(200, {'accepted': True})
                if row[0] == 'transcript.translate.run' and kind in ('complete', 'failed'):
                    if data not in ({}, {'rule': 'input_changed'}, {'rule': 'translation'}, {'rule': 'outside_shares'}):
                        return Response(400)
                    if kind == 'complete':
                        from t6.transcript import TranscriptStore
                        project = db.execute('SELECT project_id FROM transcripts WHERE id=?', (payload['transcript'],)).fetchone()
                        TranscriptStore(self.people.registry)._entry(db, payload['transcript'], project[0], 'translated', payload['actor'])
                        result = {'outcome': 'done', 'result': {'job_id': job_id, 'transcript_id': payload['transcript']}}
                    else:
                        from t6.transcript import TranscriptStore
                        TranscriptStore(self.people.registry).fail_translation(db, payload['transcript'], actor=payload['actor'])
                        result = {'outcome': 'refused', 'rule': data.get('rule', 'translation'), 'reason': 'Translation failed'}
                    db.execute("UPDATE internal_jobs SET state='done', result=?, lease_token=NULL, lease_until=NULL WHERE id=?", (json.dumps(result), job_id))
                    return Response(200, {'accepted': True})
                if row[0] == 'transcript.speakers.preview' and kind == 'preview':
                    changes, snapshot, missing = data.get('changes'), data.get('snapshot'), data.get('missing_audio')
                    if set(data) != {'changes', 'missing_audio', 'snapshot'} or not isinstance(changes, list) or not isinstance(snapshot, str) or len(snapshot) != 64 or not isinstance(missing, list) or not all(isinstance(x, str) for x in missing):
                        return Response(400)
                    if any(not isinstance(c, dict) or set(c) != {'source', 'speaker', 'from', 'to'} or not all(isinstance(v, str) for v in c.values()) for c in changes):
                        return Response(400)
                    values = {'transcript': payload['transcript'], 'csv': payload['csv']}
                    preview = self.people.tokens.create('transcript speakers', payload['actor'], values, {'changes': changes, 'missing_audio': missing})
                    nonce = self.people.tokens.verify(preview.token, 'transcript speakers', payload['actor'], values)
                    if not isinstance(nonce, str):
                        return Response(400)
                    db.execute('INSERT INTO transcript_previews VALUES (?, ?)', (nonce, json.dumps({'snapshot': snapshot, 'audio_folder': payload['audio_folder'], 'previous_map': payload['previous_map']})))
                    db.execute("UPDATE internal_jobs SET state='done', result=?, lease_token=NULL, lease_until=NULL WHERE id=?", (json.dumps(preview.model_dump(mode='json')), job_id))
                    return Response(200, {'accepted': True})
                if row[0] == 'transcript.speakers.run' and kind in ('complete', 'failed'):
                    if (kind == 'complete' and data != {}) or (kind == 'failed' and data not in ({'rule': 'input_changed'}, {'rule': 'outside_shares'})):
                        return Response(400)
                    if kind == 'complete':
                        from t6.transcript import TranscriptStore
                        project = db.execute('SELECT project_id FROM transcripts WHERE id=?', (payload['transcript'],)).fetchone()
                        TranscriptStore(self.people.registry)._entry(db, payload['transcript'], project[0], 'speakers changed', payload['actor'], payload['csv'])
                        result = {'outcome': 'done', 'result': {'job_id': job_id, 'transcript_id': payload['transcript']}}
                    else:
                        result = {'outcome': 'refused', 'rule': data['rule'], 'reason': 'Transcript inputs changed' if data['rule'] == 'input_changed' else 'Folder is outside the worker shares'}
                    db.execute("UPDATE internal_jobs SET state='done', result=?, lease_token=NULL, lease_until=NULL WHERE id=?", (json.dumps(result), job_id))
                    return Response(200, {'accepted': True})
                if kind == 'preview' and row[0] == 'transcript.preview':
                    keys = ('files_found', 'already_done', 'hours_pending', 'estimated_cost', 'snapshot')
                    if set(data) != set(keys) or any(not isinstance(data[k], (int, float)) or isinstance(data[k], bool) or not math.isfinite(data[k]) or data[k] < 0 for k in keys[:-1]) or not isinstance(data['snapshot'], str) or len(data['snapshot']) != 64 or any(c not in '0123456789abcdef' for c in data['snapshot']):
                        return Response(400, {'error': 'invalid preview'})
                    if data['already_done'] > data['files_found'] or not math.isclose(data['estimated_cost'], data['hours_pending'] * payload['rate']):
                        return Response(400, {'error': 'invalid estimate'})
                    if payload['max_hours'] is not None and data['hours_pending'] > payload['max_hours']:
                        return Response(400, {'error': 'hour limit exceeded'})
                    values = {k: v for k, v in payload.items() if k not in ('actor', 'audio_folder', 'rate')}
                    preview = self.people.tokens.create('transcript create', payload['actor'], values, {k: data[k] for k in keys[:-1]})
                    nonce = self.people.tokens.verify(preview.token, 'transcript create', payload['actor'], values)
                    if not isinstance(nonce, str):
                        return Response(400)
                    db.execute('INSERT INTO transcript_previews VALUES (?, ?)', (nonce, json.dumps({'snapshot': data['snapshot'], 'audio_folder': payload['audio_folder'], 'rate': payload['rate'], 'already_done': data['already_done'], 'hours_pending': data['hours_pending']})))
                    result = preview.model_dump(mode='json')
                    db.execute("UPDATE internal_jobs SET state='done', result=?, lease_token=NULL, lease_until=NULL WHERE id=?", (json.dumps(result), job_id))
                    return Response(200, {'accepted': True})
                if row[0] == 'transcript.run' and kind in ('progress', 'complete', 'failed'):
                    identifier = payload['transcript_id']
                    if kind == 'progress':
                        safe = {k: data[k] for k in ('state', 'completed', 'failed', 'total', 'current_clip', 'processed_seconds', 'total_seconds') if k in data}
                        if set(safe) != set(data) or safe.get('state') not in ('completed', 'failed') or any(not isinstance(safe.get(k), (int, float)) or isinstance(safe.get(k), bool) or not math.isfinite(safe[k]) or safe[k] < 0 for k in ('completed', 'failed', 'total', 'processed_seconds', 'total_seconds')) or not isinstance(safe.get('current_clip'), str):
                            return Response(400, {'error': 'invalid progress'})
                        db.execute('UPDATE transcripts SET progress=? WHERE id=?', (json.dumps(safe), identifier))
                    else:
                        safe = {k: data[k] for k in ('clips_done', 'failed', 'skipped', 'audio_hours', 'estimated_cost', 'rule') if k in data}
                        if set(safe) != set(data) or (kind == 'complete' and 'rule' in safe) or ('rule' in safe and safe['rule'] not in ('input_changed', 'outside_shares')) or any(not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) or v < 0 for k, v in safe.items() if k != 'rule'):
                            return Response(400, {'error': 'invalid summary'})
                        state = 'failed' if kind == 'failed' else 'done'
                        db.execute('UPDATE transcripts SET state=?, summary=? WHERE id=?', (state, json.dumps(safe), identifier))
                        if state == 'failed':
                            from t6.transcript import TranscriptStore
                            project = db.execute('SELECT project_id FROM transcripts WHERE id=?', (identifier,)).fetchone()
                            TranscriptStore(self.people.registry)._entry(db, identifier, project[0], 'failed', payload['actor'])
                        result = {'outcome': 'refused', 'rule': safe.get('rule', 'transcribe'), 'reason': 'Transcription job failed'} if state == 'failed' else {'outcome': 'done', 'result': {'transcript_id': identifier}}
                        db.execute("UPDATE internal_jobs SET state='done', result=?, lease_token=NULL, lease_until=NULL WHERE id=?", (json.dumps(result), job_id))
                    return Response(200, {'accepted': True})
                return Response(400, {'error': 'invalid report'})
        except (ValueError, KeyError, TypeError):
            return Response(400, {'error': 'invalid request'})

    def _transcript_translate(self, inputs: Any, actor: str) -> Any:
        import secrets
        values = {'transcript': inputs.transcript}
        nonce = self.people.tokens.verify(inputs.confirm, 'transcript translate', actor, values) if inputs.confirm else None
        if isinstance(nonce, Refused):
            return nonce
        with self.people.registry._transaction() as db:
            row = db.execute("SELECT m.ref FROM transcripts t JOIN media_locations m ON m.id=t.media_id WHERE t.id=? AND m.active=1 AND m.kind='nas-path'", (inputs.transcript,)).fetchone()
            if not row:
                return Refused(rule='transcript', reason='Transcript not found')
            from t6.transcript import TranscriptStore
            if TranscriptStore(self.people.registry).is_running(transcript=inputs.transcript, _db=db):
                return Refused(rule='transcript_running', reason='Transcript is running')
            payload = {**values, 'actor': actor, 'audio_folder': row[0], 'speaker_map': TranscriptStore(self.people.registry).speaker_map(inputs.transcript, _db=db)}
            kind = 'transcript.translate.preview'
            if nonce:
                if db.execute('SELECT 1 FROM used_preview_tokens WHERE nonce=?', (nonce,)).fetchone():
                    return Refused(rule='token_used', reason='Preview token has already been used')
                saved = db.execute('SELECT snapshot FROM transcript_previews WHERE nonce=?', (nonce,)).fetchone()
                if not saved or json.loads(saved[0])['audio_folder'] != row[0]:
                    return Refused(rule='input_changed', reason='Transcript inputs changed')
                if db.execute("SELECT 1 FROM internal_jobs WHERE kind='transcript.translate.run' AND state IN ('queued', 'claimed') AND json_extract(payload, '$.transcript')=?", (inputs.transcript,)).fetchone():
                    return Refused(rule='transcript_running', reason='Translation is already running')
                payload['snapshot'] = json.loads(saved[0])['snapshot']
                db.execute('INSERT INTO used_preview_tokens VALUES (?)', (nonce,))
                kind = 'transcript.translate.run'
            now = self.people.registry._clock()
            job = db.execute("INSERT INTO internal_jobs (queue, kind, dedupe_key, payload, run_at, state, created_at) VALUES ('transcribe', ?, ?, ?, ?, 'queued', ?)", (kind, secrets.token_urlsafe(24), json.dumps(payload), now, now))
            return Done(result={'job_id': job.lastrowid})

    def _transcript_speakers(self, inputs: Any, actor: str) -> Any:
        import secrets
        from t6.transcribe.speakers import parse_speaker_map
        try:
            parse_speaker_map(inputs.csv)
        except ValueError as error:
            return Refused(rule='speaker_map', reason=str(error))
        values = inputs.model_dump(exclude={'confirm', 'idempotency_key'})
        nonce = self.people.tokens.verify(inputs.confirm, 'transcript speakers', actor, values) if inputs.confirm else None
        if isinstance(nonce, Refused):
            return nonce
        with self.people.registry._transaction() as db:
            row = db.execute("SELECT m.ref, t.settings FROM transcripts t JOIN media_locations m ON m.id=t.media_id WHERE t.id=? AND m.active=1 AND m.kind='nas-path'", (inputs.transcript,)).fetchone()
            if not row:
                return Refused(rule='transcript', reason='Transcript not found')
            from t6.transcript import TranscriptStore
            if TranscriptStore(self.people.registry).is_running(transcript=inputs.transcript, _db=db):
                return Refused(rule='transcript_running', reason='Transcript is running')
            previous = db.execute('SELECT m.csv FROM transcript_speaker_maps m JOIN transcript_history h ON h.id=m.history_id WHERE h.transcript_id=? ORDER BY h.id DESC LIMIT 1', (inputs.transcript,)).fetchone()
            previous_map = previous[0] if previous else ''
            payload = {**values, 'actor': actor, 'audio_folder': row[0], 'previous_map': previous_map, 'start_timecode': json.loads(row[1]).get('start_timecode'), 'formats': json.loads(row[1]).get('formats')}
            kind = 'transcript.speakers.preview'
            if nonce:
                if db.execute('SELECT 1 FROM used_preview_tokens WHERE nonce=?', (nonce,)).fetchone():
                    return Refused(rule='token_used', reason='Preview token has already been used')
                if db.execute("SELECT 1 FROM internal_jobs WHERE kind='transcript.speakers.run' AND state IN ('queued', 'claimed') AND json_extract(payload, '$.transcript')=?", (inputs.transcript,)).fetchone():
                    return Refused(rule='transcript_running', reason='Speaker job is already running')
                saved = db.execute('SELECT snapshot FROM transcript_previews WHERE nonce=?', (nonce,)).fetchone()
                if not saved:
                    return Refused(rule='preview', reason='Preview not found')
                snapshot = json.loads(saved[0])
                if snapshot['audio_folder'] != row[0] or snapshot['previous_map'] != previous_map:
                    return Refused(rule='input_changed', reason='Transcript inputs changed')
                payload['snapshot'] = snapshot['snapshot']
                db.execute('INSERT INTO used_preview_tokens VALUES (?)', (nonce,))
                kind = 'transcript.speakers.run'
            now = self.people.registry._clock()
            job = db.execute("INSERT INTO internal_jobs (queue, kind, dedupe_key, payload, run_at, state, created_at) VALUES ('transcribe', ?, ?, ?, ?, 'queued', ?)", (kind, secrets.token_urlsafe(24), json.dumps(payload), now, now))
            return Done(result={'job_id': job.lastrowid})

    def _transcript_create(self, inputs: Any, actor: str) -> Any:
        import secrets
        from t6.transcript import TranscriptStore
        values = inputs.model_dump(exclude={'confirm', 'idempotency_key'})
        nonce = self.people.tokens.verify(inputs.confirm, 'transcript create', actor, values) if inputs.confirm else None
        if isinstance(nonce, Refused):
            return nonce
        with self.people.registry._transaction() as db:
            media = db.execute("SELECT ref, project_id FROM media_locations WHERE id=? AND kind='nas-path' AND active=1", (inputs.media,)).fetchone()
            if not media:
                return Refused(rule='media', reason='Active NAS Media location required')
            if inputs.deliverable and not db.execute('SELECT 1 FROM deliverables WHERE id=? AND project_id=?', (inputs.deliverable, media[1])).fetchone():
                return Refused(rule='deliverable', reason='Deliverable must belong to the Project')
            if nonce:
                if db.execute('SELECT 1 FROM used_preview_tokens WHERE nonce=?', (nonce,)).fetchone():
                    return Refused(rule='token_used', reason='Preview token has already been used')
                saved = db.execute('SELECT snapshot FROM transcript_previews WHERE nonce=?', (nonce,)).fetchone()
                if not saved:
                    return Refused(rule='preview', reason='Preview not found')
                snapshot = json.loads(saved[0])
                if snapshot['audio_folder'] != media[0]:
                    return Refused(rule='media_changed', reason='Media location changed since preview')
                if db.execute("SELECT 1 FROM internal_jobs j JOIN transcripts t ON t.id=json_extract(j.payload, '$.transcript') WHERE j.kind='transcript.speakers.run' AND j.state IN ('queued', 'claimed') AND t.media_id=?", (inputs.media,)).fetchone():
                    return Refused(rule='transcript_running', reason='Speaker job is already running')
                if TranscriptStore(self.people.registry).is_running(media=inputs.media, _db=db):
                    return Refused(rule='transcript_running', reason='A transcription is already running')
                try:
                    transcript = TranscriptStore(self.people.registry).start(inputs.media, inputs.deliverable, {**values, 'rate': snapshot['rate']}, actor=actor, _db=db)
                except ValueError as error:
                    return Refused(rule='transcript', reason=str(error))
                payload = {**values, 'actor': actor, 'audio_folder': media[0], 'rate': snapshot['rate'], 'snapshot': snapshot['snapshot'], 'already_done': snapshot.get('already_done'), 'hours_pending': snapshot.get('hours_pending'), 'transcript_id': transcript, 'speaker_map': TranscriptStore(self.people.registry).speaker_map(transcript, _db=db)}
                now = self.people.registry._clock()
                row = db.execute("INSERT INTO internal_jobs (queue, kind, dedupe_key, payload, run_at, state, created_at) VALUES ('transcribe', 'transcript.run', ?, ?, ?, 'queued', ?)", (secrets.token_urlsafe(24), json.dumps(payload), now, now))
                db.execute('INSERT INTO used_preview_tokens VALUES (?)', (nonce,))
                return Done(result={'job_id': row.lastrowid, 'transcript_id': transcript})
            payload = {**values, 'actor': actor, 'audio_folder': media[0], 'rate': self.transcribe_rate}
            now = self.people.registry._clock()
            row = db.execute("INSERT INTO internal_jobs (queue, kind, dedupe_key, payload, run_at, state, created_at) VALUES ('transcribe', 'transcript.preview', ?, ?, ?, 'queued', ?)",
                             (secrets.token_urlsafe(24), json.dumps(payload), now, now))
            return Done(result={'job_id': row.lastrowid})

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
        if noun == 'version':
            from t6.version import VersionStore
            versions = VersionStore(self.people.registry, self.people.tokens)
            if verb == 'get':
                record = versions.get(inputs.version)
                return Done(result={'version': record}) if record else Refused(rule='version', reason='Version not found')
            return Done(result={'version': versions.for_file(inputs.frameio_file)})
        if noun == 'deliverable':
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
                        item.update({key: person[key] for key in ('kind', 'departments', 'roles', 'project_roles', 'shift', 'permissions', 'master_brief', 'active')})
                    return Done(result={'person': item})
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
                    if verb in ('get', 'flags'):
                        pages = refs.pages(inputs.project)
                        active = (pages[0] if pages else inputs.project).replace('-', '').lower()
                        project = next((p for p in projects if p.page_id.replace('-', '').lower() == active), None)
                        if verb == 'flags' and project:
                            from t6.domain.project_rules import date_flags, health_missing
                            from t6.domain.studio_time import MANILA
                            today = datetime.fromtimestamp(self.people.registry._clock(), MANILA).date()
                            flags = [{'kind': 'data_gap' if f.kind == 'missing' else f.kind, 'field': f.field, 'detail': f.kind, **({'days': f.days} if f.days is not None else {})} for f in date_flags(project, today)]
                            if health_missing(project):
                                flags.append({'kind': 'data_gap', 'field': 'health', 'detail': 'missing health update'})
                            return Done(result={'flags': flags})
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
                            if page is None:
                                return Done(result={'job_order': {**record['fields'], **record, 't6_id': record['id']}})
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
        if body.get('kind') == 'command' and body.get('command') in ('link', 'unlink'):
            if self.people.authorize(person[0], 'coordinator'):
                return Response(200, {'outcome': 'rejected'})
            args = body.get('args')
            chat, topic = body.get('chat'), body.get('topic')
            key = request.header('idempotency-key') or body.get('idempotency_key')
            if isinstance(args, str) and not args.strip() and body['command'] == 'link' and type(chat) is int and type(topic) is int:
                import re
                with self.people.registry._transaction() as db:
                    row = db.execute('SELECT name FROM linked_chat_topics WHERE chat_id = ? AND topic_id = ?', (chat, topic)).fetchone()
                match = re.match(r'^\s*([A-Za-z0-9]+)\s+-\s+', row[0]) if row else None
                if match:
                    args = match.group(1)
            if not isinstance(args, str) or len(args.split()) != 1 or type(chat) is not int or (topic is not None and type(topic) is not int) or not isinstance(key, str) or not key:
                return Response(200, {'outcome': 'rejected'})
            entry = self.routes['/v1/project/' + ('link-chat' if body['command'] == 'link' else 'unlink-chat')]
            response = self._dispatch(entry, {'project': args.strip(), 'chat': chat, 'topic': topic, 'idempotency_key': key}, person[0])
            return Response(200, {'outcome': 'accepted' if response.body.get('outcome') in ('preview', 'done') else 'rejected'})
        data = body.get('data')
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
