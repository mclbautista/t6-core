"""`t6 admin privacy-registry` builds the file that T6_READ_PRIVACY_REGISTRY names."""
import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from t6.cli import main
from t6.client import Client
from t6.notion.generated import StudiosRow
from t6.notion.testing import FakeNotion, multi_select, title
from t6.person import People
from t6.preview import PreviewTokens
from t6.registry.sqlite import SqliteProjectRegistry
from t6.server.http import make_server
from t6.server.queue import JobQueue
from t6.server.verbs import VerbAPI, read_config_from_env
from t6.verbs import Done, ProjectListInput

EXPECTED = ['Acme Studios', 'Acme', 'Acme Co', 'Borealis Pictures', 'Show One', 'Acme Studios Special']


class PrivacyRegistryCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.fake = FakeNotion()
        self.fake.add_snapshot_data_source('ds-studios', 'studios')
        self.fake.add_snapshot_data_source('ds-projects', 'projects')
        name, aliases = StudiosRow.notion_name('name'), StudiosRow.notion_name('aliases')
        self.fake.add_page('ds-studios', {name: title('Acme  Studios'), aliases: multi_select('Acme', 'Acme Co')})
        self.fake.add_page('ds-studios', {name: title('Borealis Pictures'), aliases: multi_select('Acme')})
        self.fake.add_page('ds-studios', {name: title(' '), aliases: multi_select()})
        self.fake.add_page('ds-projects', {'Name': title('Show One')})
        self.fake.add_page('ds-projects', {'Name': title('Acme Studios Special')})
        self.fake.add_page('ds-projects', {'Name': title('Show One')})
        self.sources = self.dir / 'sources.json'
        self.sources.write_text('{"studios":"ds-studios","projects":"ds-projects"}', encoding='utf-8')

    def run_command(self, *args: str, env: dict[str, str] | None = None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, env if env is not None else {'T6_NOTION_TOKEN': 'fake'}, clear=True), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = main(['admin', 'privacy-registry', *args], open_notion=lambda _token: self.fake.client())
        return status, out.getvalue(), err.getvalue()

    def test_writes_studio_names_and_aliases_then_project_names_once_each(self) -> None:
        target = self.dir / 'names.json'
        status, out, err = self.run_command('--data-sources', str(self.sources), '--output', str(target))
        self.assertEqual((status, out), (0, ''))
        self.assertEqual(json.loads(target.read_text(encoding='utf-8')), EXPECTED)
        self.assertEqual(err.strip(), '6 names; 0 shorter than 4 characters; 1 plain words of 4 to 6 letters')
        self.assertNotIn('Show One', err)

    def test_prints_the_array_to_stdout_without_output(self) -> None:
        status, out, _ = self.run_command(env={'T6_NOTION_TOKEN': 'fake', 'T6_READ_DATA_SOURCES': str(self.sources)})
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(out), EXPECTED)

    def test_refuses_without_token_or_source(self) -> None:
        self.assertEqual(self.run_command('--data-sources', str(self.sources), env={})[0], 2)
        self.sources.write_text('{"projects":"ds-projects"}', encoding='utf-8')
        status, _, err = self.run_command('--data-sources', str(self.sources))
        self.assertEqual(status, 2)
        self.assertIn("'studios'", err)

    def test_the_api_loads_the_file_and_masks_with_it(self) -> None:
        target = self.dir / 'names.json'
        self.run_command('--data-sources', str(self.sources), '--output', str(target))
        _, privacy = read_config_from_env({'T6_READ_PRIVACY_REGISTRY': str(target)})
        registry = SqliteProjectRegistry(str(self.dir / 'registry'))
        api = VerbAPI(People(registry, PreviewTokens('key')), JobQueue(str(self.dir / 'queue')), {'mcp-secret': 'mcp'},
                      lambda _: self.fake.client(), privacy_registry=privacy,
                      data_sources={'projects': 'ds-projects'})
        with patch.dict(os.environ, {'T6_NOTION_TOKEN': 'fake'}):
            server = make_server(('127.0.0.1', 0), api)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(thread.join)
            self.addCleanup(server.shutdown)
            self.addCleanup(server.server_close)
            result = Client(f'http://127.0.0.1:{server.server_port}', 'mcp-secret').project_list(ProjectListInput())
        assert isinstance(result, Done)
        text = json.dumps(result.result)
        self.assertNotIn('Acme', text)
        self.assertNotIn('Show One', text)  # Project names are in the registry too
        self.assertIn('[person 1]', text)
