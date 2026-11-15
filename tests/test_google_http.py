"""The real Google Calendar adapter on a fake HTTP transport: recorded request shapes, no network, no credential (#260)."""
import base64
import json
import unittest
from typing import Any
from urllib.parse import parse_qs

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from t6.google.calendar import CalendarError, EventExists, EventNotFound
from t6.google.http import GoogleCalendar, ServiceAccount, UrllibHttp
from t6.google.testing import FakeHttp

TOKEN_URI = 'https://oauth2.example.invalid/token'
CALENDAR = 'bay-one@group.calendar.google.example.invalid'
SCOPE = 'https://www.googleapis.com/auth/calendar'
BASE = 'https://www.googleapis.com/calendar/v3/calendars/bay-one%40group.calendar.google.example.invalid'


def b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))


class AdapterCase(unittest.TestCase):
    def setUp(self) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)  # a throwaway test key, never written down
        pem = self.key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
        self.account = ServiceAccount.from_json(json.dumps({'type': 'service_account', 'client_email': 'sync@example.invalid', 'private_key': pem, 'token_uri': TOKEN_URI}))
        self.http = FakeHttp()
        self.now = 1_000_000.0
        self.http.answer('POST', TOKEN_URI, 200, {'access_token': 'token-1', 'expires_in': 3600, 'token_type': 'Bearer'})
        self.calendar = GoogleCalendar(self.account, self.http, clock=lambda: self.now)

    def api_calls(self) -> list[Any]:
        return [c for c in self.http.calls if c.url != TOKEN_URI]


class SignInTests(AdapterCase):
    def test_the_first_call_signs_a_service_account_assertion_and_exchanges_it_for_a_token(self) -> None:
        self.http.answer('POST', f'{BASE}/events', 200, {'id': 'e1'})
        self.calendar.insert_event(CALENDAR, 'e1', {'summary': 's', 'description': 'd', 'start': '2026-11-20T10:00:00+08:00', 'end': '2026-11-20T12:00:00+08:00'})
        token_call = self.http.calls[0]
        self.assertEqual((token_call.method, token_call.url), ('POST', TOKEN_URI))
        self.assertEqual(token_call.headers['Content-Type'], 'application/x-www-form-urlencoded')
        form = parse_qs(token_call.body.decode())
        self.assertEqual(form['grant_type'], ['urn:ietf:params:oauth:grant-type:jwt-bearer'])
        header, claims, signature = form['assertion'][0].split('.')
        self.assertEqual(json.loads(b64decode(header)), {'alg': 'RS256', 'typ': 'JWT'})
        self.assertEqual(json.loads(b64decode(claims)), {'iss': 'sync@example.invalid', 'scope': SCOPE, 'aud': TOKEN_URI, 'iat': 1_000_000, 'exp': 1_003_600})
        self.key.public_key().verify(b64decode(signature), f'{header}.{claims}'.encode(), padding.PKCS1v15(), hashes.SHA256())  # raises when wrong
        self.assertEqual(self.api_calls()[0].headers['Authorization'], 'Bearer token-1')

    def test_the_token_is_reused_until_it_is_about_to_expire(self) -> None:
        self.http.answer('DELETE', f'{BASE}/events/e1', 204, None)
        self.calendar.delete_event(CALENDAR, 'e1')
        self.now += 3000
        self.calendar.delete_event(CALENDAR, 'e1')
        self.assertEqual(len([c for c in self.http.calls if c.url == TOKEN_URI]), 1)
        self.now += 600
        self.http.answer('POST', TOKEN_URI, 200, {'access_token': 'token-2', 'expires_in': 3600})
        self.calendar.delete_event(CALENDAR, 'e1')
        self.assertEqual(len([c for c in self.http.calls if c.url == TOKEN_URI]), 2)
        self.assertEqual(self.api_calls()[-1].headers['Authorization'], 'Bearer token-2')

    def test_a_refused_sign_in_is_a_calendar_error_without_leaking_the_key(self) -> None:
        self.http.answer('POST', TOKEN_URI, 400, {'error': 'invalid_grant'})
        with self.assertRaises(CalendarError) as caught:
            self.calendar.delete_event(CALENDAR, 'e1')
        self.assertNotIn('PRIVATE KEY', str(caught.exception))
        self.assertEqual(self.api_calls(), [])

    def test_a_credential_that_is_not_a_service_account_key_is_refused(self) -> None:
        for text in ('not json', '{}', json.dumps({'client_email': 'a@example.invalid'}), json.dumps({'client_email': 'a', 'private_key': 'nope', 'token_uri': TOKEN_URI})):
            with self.assertRaises(ValueError, msg=text):
                ServiceAccount.from_json(text)


class EventTests(AdapterCase):
    BODY = {'summary': 'Example Show', 'description': 'Person: Ana Example\nBooking ID: t6g_x', 'start': '2026-11-20T10:00:00+08:00', 'end': '2026-11-20T12:00:00+08:00'}
    GOOGLE_BODY = {'summary': 'Example Show', 'description': 'Person: Ana Example\nBooking ID: t6g_x', 'start': {'dateTime': '2026-11-20T10:00:00+08:00'}, 'end': {'dateTime': '2026-11-20T12:00:00+08:00'}}

    def test_insert_posts_the_event_with_its_id_and_starts_and_ends_in_googles_shape(self) -> None:
        self.http.answer('POST', f'{BASE}/events', 200, {'id': 'abc123'})
        self.calendar.insert_event(CALENDAR, 'abc123', self.BODY)
        (call,) = self.api_calls()
        self.assertEqual((call.method, call.url), ('POST', f'{BASE}/events?sendUpdates=none'))
        self.assertEqual(call.headers['Content-Type'], 'application/json')
        self.assertEqual(json.loads(call.body), {**self.GOOGLE_BODY, 'id': 'abc123'})

    def test_update_puts_the_whole_event(self) -> None:
        self.http.answer('PUT', f'{BASE}/events/abc123', 200, {})
        self.calendar.update_event(CALENDAR, 'abc123', self.BODY)
        (call,) = self.api_calls()
        self.assertEqual((call.method, call.url), ('PUT', f'{BASE}/events/abc123?sendUpdates=none'))
        self.assertEqual(json.loads(call.body), {**self.GOOGLE_BODY, 'id': 'abc123', 'status': 'confirmed'})

    def test_delete_sends_a_delete(self) -> None:
        self.http.answer('DELETE', f'{BASE}/events/abc123', 204, None)
        self.calendar.delete_event(CALENDAR, 'abc123')
        (call,) = self.api_calls()
        self.assertEqual((call.method, call.url, call.body), ('DELETE', f'{BASE}/events/abc123?sendUpdates=none', b''))

    def test_conflict_is_event_exists(self) -> None:
        self.http.answer('POST', f'{BASE}/events', 409, {'error': {'code': 409, 'message': 'The requested identifier already exists.'}})
        with self.assertRaises(EventExists):
            self.calendar.insert_event(CALENDAR, 'abc123', self.BODY)

    def test_not_found_and_gone_are_event_not_found(self) -> None:
        for status in (404, 410):
            self.http.answer('PUT', f'{BASE}/events/abc123', status, {'error': {'code': status}})
            self.http.answer('DELETE', f'{BASE}/events/abc123', status, {'error': {'code': status}})
            with self.assertRaises(EventNotFound):
                self.calendar.update_event(CALENDAR, 'abc123', self.BODY)
            with self.assertRaises(EventNotFound):
                self.calendar.delete_event(CALENDAR, 'abc123')

    def test_other_failures_are_plain_calendar_errors_that_retry(self) -> None:
        for status in (401, 403, 429, 500, 503):
            self.http.answer('POST', f'{BASE}/events', status, {'error': {'code': status}})
            with self.assertRaises(CalendarError) as caught:
                self.calendar.insert_event(CALENDAR, 'abc123', self.BODY)
            self.assertNotIsInstance(caught.exception, (EventExists, EventNotFound), status)

    def test_a_network_failure_is_a_calendar_error(self) -> None:
        self.http.fail(OSError('connection reset'))
        with self.assertRaises(CalendarError):
            self.calendar.delete_event(CALENDAR, 'abc123')

    def test_a_body_without_times_is_passed_through_untouched(self) -> None:
        self.http.answer('POST', f'{BASE}/events', 200, {})
        self.calendar.insert_event(CALENDAR, 'abc123', {'summary': 's'})
        self.assertEqual(json.loads(self.api_calls()[0].body), {'summary': 's', 'id': 'abc123'})


class AclTests(AdapterCase):
    def test_list_acl_returns_user_rules_as_email_and_role(self) -> None:
        self.http.answer('GET', f'{BASE}/acl', 200, {'items': [
            {'id': 'user:a@example.invalid', 'role': 'writer', 'scope': {'type': 'user', 'value': 'a@example.invalid'}},
            {'id': 'default', 'role': 'reader', 'scope': {'type': 'default'}},
        ]})
        self.assertEqual(self.calendar.list_acl(CALENDAR), [{'email': 'a@example.invalid', 'role': 'writer'}])
        self.assertEqual((self.api_calls()[0].method, self.api_calls()[0].url), ('GET', f'{BASE}/acl'))

    def test_set_acl_inserts_a_user_rule_without_sending_a_notification(self) -> None:
        self.http.answer('POST', f'{BASE}/acl', 200, {})
        self.calendar.set_acl(CALENDAR, 'a@example.invalid', 'writer')
        (call,) = self.api_calls()
        self.assertEqual((call.method, call.url), ('POST', f'{BASE}/acl?sendNotifications=false'))
        self.assertEqual(json.loads(call.body), {'role': 'writer', 'scope': {'type': 'user', 'value': 'a@example.invalid'}})

    def test_set_acl_changes_the_role_of_an_existing_rule(self) -> None:
        self.http.answer('POST', f'{BASE}/acl', 409, {})
        self.http.answer('PUT', f'{BASE}/acl/user%3Aa%40example.invalid', 200, {})
        self.calendar.set_acl(CALENDAR, 'a@example.invalid', 'reader')
        call = self.api_calls()[-1]
        self.assertEqual((call.method, call.url), ('PUT', f'{BASE}/acl/user%3Aa%40example.invalid?sendNotifications=false'))
        self.assertEqual(json.loads(call.body), {'role': 'reader', 'scope': {'type': 'user', 'value': 'a@example.invalid'}})


class TransportTests(unittest.TestCase):
    def test_the_real_transport_exists_and_is_not_called_by_any_test(self) -> None:
        self.assertTrue(callable(UrllibHttp().send))


if __name__ == '__main__':
    unittest.main()
