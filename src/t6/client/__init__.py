"""Typed HTTP client for the t6 API. No server adapters are imported."""
from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass
from typing import Any, cast
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pydantic import BaseModel, TypeAdapter
from t6.verbs import (
    Ambiguous, Done, NeedsPerson, Outcome, Preview, Refused,
    JoListInput, JoFindInput, DeliverableListInput, MeetingNotesListInput, ProjectListInput, StudioNoticesInput, ExternalLinkFindInput, PersonListInput, ProjectGetInput, StandingCcInput, ActivityInput, CameraEnrichInput, CameraGetInput, DepartmentsInput,
    JoSubmitInput, JoCloseInput, JoBlockInput, JoUnblockInput, JoDelayInput, JoCancelInput,
    JoAssignInput, JoFormatInput, JoGetInput, JoNoteInput, JoRaiseInput, JoReassignInput,
    LLMRunInput, PersonAddInput, PersonFindInput,
    PersonGetInput, PersonPermissionInput, PersonRebindInput, PersonShiftInput, PersonClassifyInput,
    PersonTelegramInput, ProjectHoldInput, ProjectResolveInput, ProjectResumeInput,
)


@dataclass(frozen=True)
class Unavailable:
    """Transport failed; retain the key and inputs for a safe retry."""
    reason: str
    idempotency_key: str | None = None


class APIError(Exception):
    """The API returned a non-success HTTP response."""


Result = Outcome | Unavailable
_outcome: TypeAdapter[Outcome] = TypeAdapter(Outcome)


class Client:
    def __init__(self, base_url: str | None = None, secret: str | None = None, *, timeout: float = 10,
                 telegram_user_id: int | None = None) -> None:
        self.base_url = (base_url or os.environ.get('T6_API_URL', '')).rstrip('/')
        self.secret = secret if secret is not None else os.environ.get('T6_API_SECRET', '')
        self.timeout = timeout
        self.telegram_user_id = telegram_user_id

    def _request(self, path: str, inputs: BaseModel | dict[str, Any] | None = None,
                 *, idempotency_key: str | None = None) -> Any:
        if not self.base_url:
            return Unavailable('Set T6_API_URL and T6_API_SECRET to use the t6 API',
                               idempotency_key or (inputs.get('idempotency_key') if isinstance(inputs, dict) else getattr(inputs, 'idempotency_key', None)))
        headers = {'Authorization': 'Bearer ' + self.secret}
        if self.telegram_user_id is not None:
            headers['X-Telegram-User-Id'] = str(self.telegram_user_id)
        data = None
        key = None
        if inputs is not None:
            payload = dict(inputs) if isinstance(inputs, dict) else inputs.model_dump(mode='json', exclude_none=True, by_alias=True)
            for name in ('data_sources', 'file'):
                value = getattr(inputs, name, None)
                if isinstance(value, Path):
                    try:
                        payload[name + '_contents'] = value.read_text(encoding='utf-8')
                    except OSError as error:
                        raise APIError(f'Cannot read {value}: {error}') from error
                    payload.pop(name, None)
            # Registry state belongs to the API host, not the caller.
            payload.pop('registry_db', None)
            key = idempotency_key or payload.get('idempotency_key')
            if key:
                headers['Idempotency-Key'] = key
            headers['Content-Type'] = 'application/json'
            data = json.dumps(payload).encode()
        request = Request(self.base_url + path, data=data, headers=headers)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return json.load(response)
        except HTTPError as error:
            raise APIError(f'API returned HTTP {error.code}: {error.read().decode(errors="replace")}') from error
        except (URLError, TimeoutError, OSError) as error:
            return Unavailable(str(error), key)

    def call(self, noun: str, verb: str, inputs: BaseModel) -> Result:
        if 'idempotency_key' in type(inputs).model_fields and not getattr(inputs, 'idempotency_key', None):
            inputs = inputs.model_copy(update={'idempotency_key': secrets.token_hex(16)})
        if (noun, verb) in {('person', 'get'), ('person', 'find'), ('project', 'resolve'),
                            ('jo', 'get'), ('camera', 'get'), ('departments', '')}:
            from t6.verbs import VERBS
            model = next(v.input_model for v in VERBS if (v.noun, v.verb) == (noun, verb))
            inputs = model.model_validate(inputs.model_dump(exclude={'data_sources', 'registry_db'}))
        value = self._request('/v1/' + noun + ('/' + verb if verb else ''), inputs)
        return value if isinstance(value, Unavailable) else _outcome.validate_python(value)

    def health(self) -> dict[str, Any] | Unavailable:
        return cast(dict[str, Any] | Unavailable, self._request('/health'))

    def tap(self, payload: dict[str, Any], *, idempotency_key: str) -> dict[str, Any] | Unavailable:
        """Deliver a gateway tap with its retry key and configured Telegram identity."""
        return cast(dict[str, Any] | Unavailable, self._request('/tap', payload, idempotency_key=idempotency_key))

    def llm_run(self, inputs: LLMRunInput) -> Result:
        return self.call('llm', 'run', inputs)

    def person_add(self, inputs: PersonAddInput) -> Result:
        return self.call('person', 'add', inputs)

    def person_rebind(self, inputs: PersonRebindInput) -> Result:
        return self.call('person', 'rebind', inputs)

    def person_set_telegram(self, inputs: PersonTelegramInput) -> Result:
        return self.call('person', 'set-telegram', inputs)

    def person_shift(self, inputs: PersonShiftInput) -> Result:
        return self.call('person', 'shift', inputs)

    def person_classify(self, inputs: PersonClassifyInput) -> Result:
        return self.call('person', 'classify', inputs)

    def person_grant(self, inputs: PersonPermissionInput) -> Result:
        return self.call('person', 'grant', inputs)

    def person_revoke(self, inputs: PersonPermissionInput) -> Result:
        return self.call('person', 'revoke', inputs)

    def person_get(self, inputs: PersonGetInput) -> Result:
        return self.call('person', 'get', inputs)

    def studio_notices(self, inputs: StudioNoticesInput) -> Result:
        return self.call('studio', 'notices', inputs)

    def external_link_find(self, inputs: ExternalLinkFindInput) -> Result:
        return self.call('external-link', 'find', inputs)

    def project_get(self, inputs: ProjectGetInput) -> Result:
        return self.call('project', 'get', inputs)

    def person_list(self, inputs: PersonListInput) -> Result:
        return self.call('person', 'list', inputs)

    def standing_cc(self, inputs: StandingCcInput) -> Result:
        return self.call('standing-cc', '', inputs)

    def jo_list(self, inputs: JoListInput) -> Result:
        return self.call('jo', 'list', inputs)

    def jo_find(self, inputs: JoFindInput) -> Result:
        return self.call('jo', 'find', inputs)

    def deliverable_list(self, inputs: DeliverableListInput) -> Result:
        return self.call('deliverable', 'list', inputs)

    def meeting_notes_list(self, inputs: MeetingNotesListInput) -> Result:
        return self.call('meeting-notes', 'list', inputs)

    def project_list(self, inputs: ProjectListInput) -> Result:
        return self.call('project', 'list', inputs)

    def project_hold(self, inputs: ProjectHoldInput) -> Result:
        return self.call('project', 'hold', inputs)

    def project_resume(self, inputs: ProjectResumeInput) -> Result:
        return self.call('project', 'resume', inputs)

    def project_activity(self, inputs: ActivityInput) -> Result:
        return self.call('project', 'activity', inputs)

    def jo_get(self, inputs: JoGetInput) -> Result:
        return self.call('jo', 'get', inputs)

    def jo_raise(self, inputs: JoRaiseInput) -> Result:
        return self.call('jo', 'raise', inputs)

    def jo_assign(self, inputs: JoAssignInput) -> Result:
        return self.call('jo', 'assign', inputs)

    def jo_reassign(self, inputs: JoReassignInput) -> Result:
        return self.call('jo', 'reassign', inputs)

    def jo_note(self, inputs: JoNoteInput) -> Result:
        return self.call('jo', 'note', inputs)

    def jo_submit(self, inputs: JoSubmitInput) -> Result:
        return self.call('jo', 'submit', inputs)

    def jo_close(self, inputs: JoCloseInput) -> Result:
        return self.call('jo', 'close', inputs)

    def jo_block(self, inputs: JoBlockInput) -> Result:
        return self.call('jo', 'block', inputs)

    def jo_unblock(self, inputs: JoUnblockInput) -> Result:
        return self.call('jo', 'unblock', inputs)

    def jo_delay(self, inputs: JoDelayInput) -> Result:
        return self.call('jo', 'delay', inputs)

    def jo_cancel(self, inputs: JoCancelInput) -> Result:
        return self.call('jo', 'cancel', inputs)

    def jo_format(self, inputs: JoFormatInput) -> Result:
        return self.call('jo', 'format', inputs)

    def person_find(self, inputs: PersonFindInput) -> Result:
        return self.call('person', 'find', inputs)

    def project_resolve(self, inputs: ProjectResolveInput) -> Result:
        return self.call('project', 'resolve', inputs)

    def camera_get(self, inputs: CameraGetInput) -> Result:
        return self.call('camera', 'get', inputs)

    def camera_enrich(self, inputs: CameraEnrichInput) -> Result:
        return self.call('camera', 'enrich', inputs)

    def departments(self, inputs: DepartmentsInput) -> Result:
        return self.call('departments', '', inputs)
