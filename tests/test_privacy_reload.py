import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

from t6.domain.studio_time import MANILA
from t6.person import People
from t6.preview import PreviewTokens
from t6.privacy import Session
from t6.registry.sqlite import SqliteProjectRegistry
from t6.server.app import Request
from t6.server.queue import JobQueue
from t6.server.verbs import PrivacyList, VerbAPI, read_config_from_env
from t6.worker.internal_queue import InternalJobQueue


def _obj(people: list[str], projects: list[str] | None = None) -> dict[str, Any]:
    return {
        'people': [{'name': name} for name in people],
        'projects': [{'name': name} for name in (projects or [])],
        'ids': [],
    }


class PrivacyListTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.file = self.tmp / 'names.json'
        self.write(_obj(['Client Corp']))
        _, registry = read_config_from_env({'T6_READ_PRIVACY_REGISTRY': str(self.file)})
        assert registry is not None
        self.lines: list[str] = []
        self.now = 1_000_000.0
        self.list = PrivacyList(str(self.file), registry, self.lines.append, lambda: self.now)

    def write(self, value: object, age: float = 0) -> None:
        self.file.write_text(json.dumps(value), encoding='utf-8')
        stamp = os.stat(self.file).st_mtime_ns + 1_000_000_000 * int(age or 1)  # always a new modification time
        os.utime(self.file, ns=(stamp, stamp))

    def masked(self, text: str) -> str:
        return Session(self.list.current()).mask(text).text

    def test_a_name_added_to_the_file_is_masked_without_a_restart(self) -> None:
        self.assertEqual(self.masked('New Show and Client Corp'), 'New Show and [person 1]')
        self.write(_obj(['Client Corp'], ['New Show']))
        self.assertNotIn('New Show', self.masked('New Show and Client Corp'))
        self.assertEqual(self.lines, [])

    def test_a_file_that_cannot_be_read_keeps_the_last_good_list_and_logs_one_line(self) -> None:
        self.file.write_text('{"bad": "shape"}', encoding='utf-8')
        os.utime(self.file, ns=(1, 1))
        for _ in range(3):
            self.assertNotIn('Client Corp', self.masked('Client Corp'))
        self.assertEqual(len(self.lines), 1)
        self.assertNotIn(str(self.file), self.lines[0])
        self.assertTrue(self.list.status()['reload_failed'])
        self.write(_obj(['Client Corp', 'New Show']))  # the next good file is used and clears the flag
        self.assertNotIn('New Show', self.masked('New Show'))
        self.assertFalse(self.list.status()['reload_failed'])
        self.file.unlink()
        self.masked('x')
        self.masked('x')
        self.assertEqual(len(self.lines), 2)
        self.assertNotIn('Client Corp', self.masked('Client Corp'))

    def test_status_gives_people_projects_format_and_age_of_the_list(self) -> None:
        self.write(_obj(['Client Corp'], ['Show One', 'Show Two']))
        self.list.current()
        mtime = os.stat(self.file).st_mtime
        self.now = mtime + 3600.4
        self.assertEqual(
            self.list.status(),
            {'people': 1, 'projects': 2, 'format': 'object', 'age_seconds': 3600, 'reload_failed': False},
        )

    def test_legacy_array_format_loads_logs_warning_and_flags_status_and_health(self) -> None:
        legacy_file = self.tmp / 'legacy.json'
        legacy_file.write_text(json.dumps(['Client Corp', 'Other Corp']), encoding='utf-8')
        _, legacy_registry = read_config_from_env({'T6_READ_PRIVACY_REGISTRY': str(legacy_file)})
        assert legacy_registry is not None
        warnings: list[str] = []
        legacy_list = PrivacyList(str(legacy_file), legacy_registry, warnings.append, lambda: self.now)

        self.assertEqual(len(warnings), 1)
        self.assertIn('legacy', warnings[0])
        self.assertNotIn(str(legacy_file), warnings[0])
        self.assertNotIn('Client Corp', warnings[0])
        self.assertEqual(
            legacy_list.status(),
            {
                'people': 2,
                'projects': 0,
                'format': 'legacy format',
                'age_seconds': legacy_list.status()['age_seconds'],
                'reload_failed': False,
            },
        )
        self.assertEqual(len(warnings), 1)  # status() does not re-log when unchanged

        registry = SqliteProjectRegistry(str(self.tmp / 'registry'))
        people = People(registry, PreviewTokens('test'), {})
        api = VerbAPI(people, JobQueue(str(self.tmp / 'queue')), {'secret': 'reports'}, lambda *args: None, privacy_list=legacy_list)
        health = api(Request('GET', '/health', {}, b'')).body['privacy_registry']
        assert isinstance(health, dict)
        self.assertEqual(
            health,
            {
                'people': 2,
                'projects': 0,
                'format': 'legacy format',
                'age_seconds': health['age_seconds'],
                'reload_failed': False,
                'last_rebuild': None,
            },
        )

        # Upgrading the file to object format clears the legacy format flag without warning.
        legacy_file.write_text(json.dumps(_obj(['Client Corp'], ['Show One'])), encoding='utf-8')
        stamp = os.stat(legacy_file).st_mtime_ns + 1_000_000_000
        os.utime(legacy_file, ns=(stamp, stamp))
        self.assertEqual(legacy_list.status()['format'], 'object')
        self.assertEqual(legacy_list.status()['people'], 1)
        self.assertEqual(legacy_list.status()['projects'], 1)
        self.assertEqual(len(warnings), 1)

        # Reloading a legacy array file logs the warning again.
        legacy_file.write_text(json.dumps(['Client Corp']), encoding='utf-8')
        os.utime(legacy_file, ns=(stamp + 1_000_000_000, stamp + 1_000_000_000))
        self.assertEqual(legacy_list.status()['format'], 'legacy format')
        self.assertEqual(len(warnings), 2)

    def test_server_startup_logs_warning_when_privacy_registry_uses_legacy_format(self) -> None:
        from t6.server.__main__ import main as server_main

        legacy_file = self.tmp / 'startup_legacy.json'
        legacy_file.write_text(json.dumps(['Client Corp']), encoding='utf-8')
        routes = self.tmp / 'routes.yaml'
        routes.write_text('- {path: /events/notion, verifier: notion, secret_env: NOTION, job_kind: notion.event, queue: main}', encoding='utf-8')
        env = {
            'T6_SERVER_MODE': 'full',
            'T6_ROUTES_FILE': str(routes),
            'T6_QUEUE_DB': str(self.tmp / 'queue.sqlite'),
            'T6_REGISTRY_DB': ':memory:',
            'T6_READ_PRIVACY_REGISTRY': str(legacy_file),
        }
        stderr = io.StringIO()
        with patch.dict(os.environ, env, clear=True), patch('t6.server.__main__.serve'), patch('t6.server.__main__.make_server'), patch('t6.server.__main__.threading.Thread'), contextlib.redirect_stderr(stderr):
            self.assertEqual(server_main(), 0)
        self.assertIn('legacy', stderr.getvalue())

    def test_health_shows_the_list_last_rebuild_and_masked_reads_use_it(self) -> None:
        registry = SqliteProjectRegistry(str(self.tmp / 'registry'))
        people = People(registry, PreviewTokens('test'), {})
        api = VerbAPI(people, JobQueue(str(self.tmp / 'queue')), {'secret': 'reports'}, lambda *args: None, privacy_list=self.list)

        def shown() -> Any:
            return api(Request('GET', '/health', {}, b'')).body['privacy_registry']

        self.assertEqual(
            shown(),
            {
                'people': 1,
                'projects': 0,
                'format': 'object',
                'age_seconds': shown()['age_seconds'],
                'reload_failed': False,
                'last_rebuild': None,
            },
        )
        self.write(_obj(['Client Corp'], ['New Show']))
        self.assertEqual((shown()['people'], shown()['projects']), (1, 1))
        self.assertEqual(len(api.privacy_registry.people), 1)  # type: ignore[union-attr]
        self.assertEqual(len(api.privacy_registry.projects), 1)  # type: ignore[union-attr]

        # Queued rebuilds are not shown as last_rebuild until finished.
        clock_now = [datetime(2026, 11, 25, 4, 0, tzinfo=MANILA).timestamp()]
        internal = InternalJobQueue(registry, clock=lambda: clock_now[0])
        submitted = internal.submit('main', 'privacy_registry.rebuild', 'privacy_registry.rebuild:daily:2026-11-25', {})
        self.assertIsNone(shown()['last_rebuild'])

        claim = internal.claim('main', 60, ['privacy_registry.rebuild'])
        assert claim is not None
        self.assertTrue(internal.ack(submitted.job_id, claim.token, {'people': 1, 'projects': 1}))
        self.assertEqual(
            shown()['last_rebuild'],
            {
                'at': datetime.fromtimestamp(clock_now[0], MANILA).isoformat(),
                'status': 'ok',
                'result': {'people': 1, 'projects': 1},
            },
        )

        # A later failed rebuild replaces last_rebuild with the failure status and reason.
        clock_now[0] = datetime(2026, 11, 26, 4, 0, tzinfo=MANILA).timestamp()
        failed_sub = internal.submit('main', 'privacy_registry.rebuild', 'privacy_registry.rebuild:daily:2026-11-26', {})
        failed_claim = internal.claim('main', 60, ['privacy_registry.rebuild'])
        assert failed_claim is not None
        self.assertTrue(internal.fail(failed_sub.job_id, failed_claim.token, 'notion unavailable'))
        self.assertEqual(
            shown()['last_rebuild'],
            {
                'at': datetime.fromtimestamp(clock_now[0], MANILA).isoformat(),
                'status': 'failed',
                'result': 'notion unavailable',
            },
        )

        plain = VerbAPI(people, JobQueue(str(self.tmp / 'queue')), {'secret': 'reports'}, lambda *args: None)
        self.assertIsNone(plain.privacy_registry)
        self.assertIsNone(plain(Request('GET', '/health', {}, b'')).body['privacy_registry'])


if __name__ == '__main__':
    unittest.main()


class LLMServiceLiveRegistryTests(unittest.TestCase):
    def test_a_name_added_to_the_file_is_masked_in_the_next_llm_run_without_a_restart(self) -> None:
        from t6.llm.testing import FakeProvider
        from t6.server.llm import LLMService
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / 'privacy_registry.json'
            file.write_text(json.dumps({'people': [{'name': 'Ana Cruz'}], 'projects': [], 'ids': []}), encoding='utf-8')
            _, registry = read_config_from_env({'T6_READ_PRIVACY_REGISTRY': str(file)})
            assert registry is not None
            names = PrivacyList(str(file), registry)
            service = LLMService({'fake': FakeProvider()}, 'fake', names.current)
            self.assertEqual([p.name for p in service.registry.people], ['Ana Cruz'])
            file.write_text(json.dumps({'people': [{'name': 'Ana Cruz'}], 'projects': [{'name': 'Show A', 'code': 'SHA'}], 'ids': []}), encoding='utf-8')
            os.utime(file, ns=(2_000_000_000, 2_000_000_000))
            self.assertEqual([p.name for p in service.registry.projects], ['Show A'])
            file.write_text('not json', encoding='utf-8')
            os.utime(file, ns=(3_000_000_000, 3_000_000_000))
            self.assertEqual([p.name for p in service.registry.projects], ['Show A'])  # last good list stays
            self.assertTrue(names.status()['reload_failed'])
