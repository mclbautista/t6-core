"""The Google Calendar adapter over HTTP, signed in as a service account (#260, findings of #186).

No Google client library: the standard library does the HTTP, and `cryptography` (extra `google`)
signs the service account's sign-in assertion. A transport is injected so tests send nothing.
"""
from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote, urlencode

from t6.google.calendar import CalendarError, EventExists, EventNotFound

SCOPE = 'https://www.googleapis.com/auth/calendar'
API = 'https://www.googleapis.com/calendar/v3'
GRANT = 'urn:ietf:params:oauth:grant-type:jwt-bearer'
TOKEN_LIFETIME = 3600
REFRESH_MARGIN = 300


class Http(Protocol):
    def send(self, method: str, url: str, headers: Mapping[str, str], body: bytes) -> tuple[int, bytes]: ...


class UrllibHttp:
    """The real transport. Tests use `FakeHttp` instead."""

    def send(self, method: str, url: str, headers: Mapping[str, str], body: bytes) -> tuple[int, bytes]:
        request = urllib.request.Request(url, data=body or None, headers=dict(headers), method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return int(response.status), bytes(response.read())
        except urllib.error.HTTPError as error:
            return int(error.code), bytes(error.read())


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


@dataclass(frozen=True)
class ServiceAccount:
    email: str
    private_key: str
    token_uri: str

    @classmethod
    def from_json(cls, text: str) -> ServiceAccount:
        try:
            value = json.loads(text)
            account = cls(value['client_email'], value['private_key'], value['token_uri'])
        except (ValueError, KeyError, TypeError) as error:
            raise ValueError('the Google credential is not a service account key file') from error
        if not all(isinstance(v, str) and v for v in (account.email, account.private_key, account.token_uri)):
            raise ValueError('the Google credential is not a service account key file')
        account._key()
        return account

    def _key(self) -> Any:
        try:
            from cryptography.hazmat.primitives import serialization
            return serialization.load_pem_private_key(self.private_key.encode(), password=None)
        except ImportError as error:
            raise ValueError("the Google calendar needs the 'google' extra: pip install 't6[google]'") from error
        except (ValueError, TypeError) as error:
            raise ValueError('the Google credential has no usable private key') from error

    def assertion(self, now: float) -> str:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        issued = int(now)
        header = _b64(json.dumps({'alg': 'RS256', 'typ': 'JWT'}, separators=(',', ':')).encode())
        claims = _b64(json.dumps({'iss': self.email, 'scope': SCOPE, 'aud': self.token_uri, 'iat': issued, 'exp': issued + TOKEN_LIFETIME}, separators=(',', ':')).encode())
        signature = self._key().sign(f'{header}.{claims}'.encode(), padding.PKCS1v15(), hashes.SHA256())
        return f'{header}.{claims}.{_b64(signature)}'


class GoogleCalendar:
    """The `Calendar` protocol on Google Calendar v3. Every failure that is not "exists" or "not found" is a `CalendarError`."""

    def __init__(self, account: ServiceAccount, http: Http, *, clock: Callable[[], float] = time.time) -> None:
        self._account, self._http, self._clock = account, http, clock
        self._token: str | None = None
        self._expires = 0.0

    def _bearer(self) -> str:
        now = self._clock()
        if self._token is None or now >= self._expires - REFRESH_MARGIN:
            form = urlencode({'grant_type': GRANT, 'assertion': self._account.assertion(now)}).encode()
            status, body = self._send('POST', self._account.token_uri, {'Content-Type': 'application/x-www-form-urlencoded'}, form)
            try:
                answer = json.loads(body)
                token, lifetime = str(answer['access_token']), float(answer.get('expires_in', TOKEN_LIFETIME))
            except (ValueError, KeyError, TypeError, AttributeError):
                token, lifetime = '', 0.0
            if status != 200 or not token:
                raise CalendarError(f'Google sign-in failed ({status})')
            self._token, self._expires = token, now + lifetime
        return self._token

    def _send(self, method: str, url: str, headers: Mapping[str, str], body: bytes) -> tuple[int, bytes]:
        try:
            return self._http.send(method, url, headers, body)
        except OSError as error:
            raise CalendarError(f'Google calendar unreachable: {error.__class__.__name__}') from error

    def _call(self, method: str, path: str, body: Any = None, *, query: str = '') -> tuple[int, Any]:
        headers = {'Authorization': f'Bearer {self._bearer()}'}
        data = b''
        if body is not None:
            headers['Content-Type'] = 'application/json'
            data = json.dumps(body, separators=(',', ':')).encode()
        status, raw = self._send(method, f'{API}{path}{query}', headers, data)
        try:
            answer = json.loads(raw) if raw else None
        except ValueError:
            answer = None
        return status, answer

    def _fail(self, status: int, what: str) -> CalendarError:
        return CalendarError(f'Google calendar {what} failed ({status})')

    @staticmethod
    def _event_path(calendar: str, event: str | None = None) -> str:
        path = f'/calendars/{quote(calendar, safe="")}/events'
        return path if event is None else f'{path}/{quote(event, safe="")}'

    @staticmethod
    def _google_body(body: Mapping[str, Any]) -> dict[str, Any]:
        shaped = dict(body)
        for field in ('start', 'end'):
            if isinstance(shaped.get(field), str):
                shaped[field] = {'dateTime': shaped[field]}
        return shaped

    def insert_event(self, calendar: str, event: str, body: dict[str, Any]) -> None:
        status, _ = self._call('POST', self._event_path(calendar), {**self._google_body(body), 'id': event}, query='?sendUpdates=none')
        if status == 409:
            raise EventExists(event)
        if status != 200:
            raise self._fail(status, 'insert')

    def update_event(self, calendar: str, event: str, body: dict[str, Any]) -> None:
        status, _ = self._call('PUT', self._event_path(calendar, event), {**self._google_body(body), 'id': event, 'status': 'confirmed'}, query='?sendUpdates=none')
        if status in (404, 410):
            raise EventNotFound(event)
        if status != 200:
            raise self._fail(status, 'update')

    def delete_event(self, calendar: str, event: str) -> None:
        status, _ = self._call('DELETE', self._event_path(calendar, event), query='?sendUpdates=none')
        if status in (404, 410):
            raise EventNotFound(event)
        if status not in (200, 204):
            raise self._fail(status, 'delete')

    def list_acl(self, calendar: str) -> list[dict[str, str]]:
        status, answer = self._call('GET', f'/calendars/{quote(calendar, safe="")}/acl')
        if status != 200 or not isinstance(answer, dict):
            raise self._fail(status, 'ACL list')
        return [{'email': item['scope']['value'], 'role': item['role']} for item in answer.get('items', []) if item.get('scope', {}).get('type') == 'user']

    def set_acl(self, calendar: str, email: str, role: str) -> None:
        path = f'/calendars/{quote(calendar, safe="")}/acl'
        rule = {'role': role, 'scope': {'type': 'user', 'value': email}}
        status, _ = self._call('POST', path, rule, query='?sendNotifications=false')
        if status == 409:
            status, _ = self._call('PUT', f'{path}/{quote("user:" + email, safe="")}', rule, query='?sendNotifications=false')
        if status != 200:
            raise self._fail(status, 'ACL set')
