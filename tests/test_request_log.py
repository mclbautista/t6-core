import json
import re
import tempfile
import unittest
from typing import Any

from t6.person import People
from t6.preview import PreviewTokens
from t6.registry.sqlite import SqliteProjectRegistry
from t6.server.app import Request, Response
from t6.server.verbs import VerbAPI, request_line

LINE = re.compile(r't6\.server: request time=\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z identity=(\S+) verb=(\S+) outcome=(\S+) ms=\d+')


class RequestLogTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.registry = SqliteProjectRegistry(tmp.name + '/registry')
        self.person = 't6n_' + '1' * 32
        with self.registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES (?, 'staff', '[]', '[]', 42, 0, 1)", (self.person,))
        self.lines: list[str] = []
        people = People(self.registry, PreviewTokens('test'), {'reports': frozenset()})
        self.api = VerbAPI(people, None, {'secret': 'reports', 'gateway-secret': 'gateway'}, lambda *args: None,
                           request_log=self.lines.append)

    def call(self, path: str, body: Any = None, secret: str | None = 'secret', method: str = 'POST', **headers: str) -> Response:
        if secret:
            headers['Authorization'] = 'Bearer ' + secret
        data = body if isinstance(body, bytes) else json.dumps(body or {}).encode()
        return self.api(Request(method, path, headers, data))

    def last(self) -> tuple[str, str, str]:
        self.assertEqual(len(self.lines), 1)
        found = LINE.fullmatch(self.lines[0])
        assert found, self.lines[0]
        return found.group(1), found.group(2), found.group(3)

    def test_a_refusal_logs_its_rule_and_the_service(self) -> None:
        self.call('/v1/llm/run', {'task_name': 'summary', 'prompt_template': 'SECRET-TEXT {input}', 'input_text': 'Jane Doe',
                                  'output_schema': {'type': 'object'}, 'masking_preset': 'names'})
        self.assertEqual(self.last(), ('reports', 'llm.run', 'refused:permission'))
        self.assertNotIn('SECRET-TEXT', self.lines[0])
        self.assertNotIn('Jane', self.lines[0])
        self.assertNotIn('Bearer', self.lines[0])

    def test_a_done_read_logs_done(self) -> None:
        self.call('/v1/commands', method='GET')
        self.assertEqual(self.last(), ('reports', 'commands', 'done'))

    def test_an_unknown_secret_is_refused_without_an_identity(self) -> None:
        self.call('/v1/llm/run', secret='wrong')
        self.assertEqual(self.last(), ('-', 'llm.run', 'refused:unauthorized'))

    def test_a_bad_body_and_an_unknown_path_are_errors_and_the_path_is_not_logged(self) -> None:
        self.call('/v1/llm/run', b'[1]')
        self.assertEqual(self.last(), ('reports', 'llm.run', 'error:422'))
        self.lines.clear()
        self.call('/v1/Jane-Doe/secret-path')
        self.assertEqual(self.last(), ('reports', 'unknown', 'error:404'))
        self.assertNotIn('Jane', self.lines[0])

    def test_the_gateway_on_behalf_of_a_person_logs_the_person_id(self) -> None:
        self.call('/v1/llm/run', {}, secret='gateway-secret', **{'X-Telegram-User-Id': '42'})
        self.assertEqual(self.last()[0], self.person)

    def test_health_is_not_logged_and_the_log_is_off_without_a_sink(self) -> None:
        self.assertEqual(self.api.request_log, self.lines.append)
        self.lines.clear()
        self.api.request_log = None
        self.call('/v1/commands', method='GET')
        self.assertEqual(self.lines, [])

    def test_the_outcome_kinds(self) -> None:
        for response, expected in ((Response(200, {'outcome': 'preview'}), 'preview'), (Response(200, {'outcome': 'refused', 'rule': 'Jane Doe!'}), 'refused:other'),
                                   (Response(500), 'error:500')):
            line = request_line('/v1/commands', 'reports', response, 0.0123, {})
            self.assertTrue(line.endswith(f'outcome={expected} ms=12'), line)

    def test_refused_reads_log_fixed_reason_codes_without_leaking_text(self) -> None:
        from unittest.mock import patch
        from t6.notion import NotionError

        class FailingNotion:
            def __init__(self, error: Exception) -> None:
                self.error = error

            def query_data_source(self, *_args: Any, **_kwargs: Any) -> Any:
                raise self.error

        self.api.data_sources = {'contacts': 'contacts', 'projects': 'projects'}
        self.api.open_notion = lambda _: FailingNotion(NotionError(504, 'timeout', 'secret client name timed out'))
        with patch.dict('os.environ', {'T6_NOTION_TOKEN': 'secret'}):
            response = self.call('/v1/person/find', {'mention': 'secret'})
        self.assertEqual(self.last(), ('reports', 'person.find', 'refused:read:notion_timeout'))
        self.assertNotIn('secret', self.lines[0])
        self.assertNotIn('failure', response.body)
        self.assertNotIn('retry_after', response.body)

        self.lines.clear()
        self.api.open_notion = lambda _: FailingNotion(NotionError(429, 'rate_limited', 'too many requests'))
        with patch.dict('os.environ', {'T6_NOTION_TOKEN': 'secret'}):
            self.call('/v1/person/find', {'mention': 'someone'})
        self.assertEqual(self.last(), ('reports', 'person.find', 'refused:read:notion_rate_limited'))

        self.lines.clear()
        with patch.dict('os.environ', {'T6_NOTION_TOKEN': 'secret'}):
            self.call('/v1/person/get', {'person_id': self.person})
        self.assertEqual(self.last(), ('reports', 'person.get', 'refused:person:no_binding'))


if __name__ == '__main__':
    unittest.main()
