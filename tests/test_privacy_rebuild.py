"""The `privacy_registry.rebuild` job replaces privacy_registry.json so masked reads and LLM calls hide a new Project at once (#539)."""
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from t6.notion.generated import StudiosRow
from t6.notion.source import NotionSource
from t6.notion.testing import FakeNotion, multi_select, title
from t6.privacy import Session
from t6.privacy_build import Rebuild
from t6.server.verbs import PrivacyList, read_config_from_env


class RebuildTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.output = Path(tmp.name) / 'privacy_registry.json'
        self.fake = FakeNotion()
        self.fake.add_snapshot_data_source('ds-studios', 'studios')
        self.fake.add_snapshot_data_source('ds-projects', 'projects')
        self.fake.add_page('ds-studios', {StudiosRow.notion_name('name'): title('Acme'), StudiosRow.notion_name('aliases'): multi_select()})
        self.fake.add_page('ds-projects', {'Name': title('Show One')})
        self.rebuild = Rebuild(NotionSource.from_client(self.fake.client()), {'studios': 'ds-studios', 'projects': 'ds-projects'}, self.output)

    def test_it_writes_the_file_and_returns_counts_only(self) -> None:
        result = self.rebuild(object())
        self.assertEqual(result, {'people': 1, 'projects': 1})
        self.assertNotIn('Show One', json.dumps(result))
        self.assertEqual(json.loads(self.output.read_text(encoding='utf-8'))['projects'], [{'name': 'Show One'}])

    def test_the_file_is_private_to_its_owner_even_with_a_loose_umask(self) -> None:
        old = os.umask(0o022)
        self.addCleanup(os.umask, old)
        self.rebuild(object())
        self.rebuild(object())  # a second rebuild replaces it and keeps the mode
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o600)

    def test_a_project_added_later_is_masked_after_the_next_rebuild_with_no_restart(self) -> None:
        self.rebuild(object())
        _, registry = read_config_from_env({'T6_READ_PRIVACY_REGISTRY': str(self.output)})
        assert registry is not None
        names = PrivacyList(str(self.output), registry, None)
        self.assertEqual(Session(names.current()).mask('Show A airs Friday').text, 'Show A airs Friday')
        self.fake.add_page('ds-projects', {'Name': title('Show A')})
        self.rebuild(object())
        self.assertEqual(Session(names.current()).mask('Show A airs Friday').text, '[project 1] airs Friday')
        self.assertFalse(self.output.with_name(self.output.name + '.tmp').exists())


if __name__ == '__main__':
    unittest.main()
