import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any

from t6.person import People
from t6.preview import PreviewTokens
from t6.privacy import Session
from t6.registry.sqlite import SqliteProjectRegistry
from t6.server.app import Request
from t6.server.queue import JobQueue
from t6.server.verbs import PrivacyList, VerbAPI, read_config_from_env


class PrivacyListTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.file = self.tmp / 'names.json'
        self.write(['Client Corp'])
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
        self.write(['Client Corp', 'New Show'])
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
        self.write(['Client Corp', 'New Show'])  # the next good file is used and clears the flag
        self.assertNotIn('New Show', self.masked('New Show'))
        self.assertFalse(self.list.status()['reload_failed'])
        self.file.unlink()
        self.masked('x')
        self.masked('x')
        self.assertEqual(len(self.lines), 2)
        self.assertNotIn('Client Corp', self.masked('Client Corp'))

    def test_status_gives_the_names_and_the_age_of_the_list(self) -> None:
        mtime = os.stat(self.file).st_mtime
        self.now = mtime + 3600.4
        self.assertEqual(self.list.status(), {'names': 1, 'age_seconds': 3600, 'reload_failed': False})

    def test_health_shows_the_list_and_masked_reads_use_it(self) -> None:
        registry = SqliteProjectRegistry(str(self.tmp / 'registry'))
        people = People(registry, PreviewTokens('test'), {})
        api = VerbAPI(people, JobQueue(str(self.tmp / 'queue')), {'secret': 'reports'}, lambda *args: None, privacy_list=self.list)
        def shown() -> Any:
            return api(Request('GET', '/health', {}, b'')).body['privacy_registry']
        self.assertEqual(shown(), {'names': 1, 'age_seconds': shown()['age_seconds'], 'reload_failed': False})
        self.write(['Client Corp', 'New Show'])
        self.assertEqual(shown()['names'], 2)
        self.assertEqual(len(api.privacy_registry.people), 2)  # type: ignore[union-attr]
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
