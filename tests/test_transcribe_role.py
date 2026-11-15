import ast
import re
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from t6.worker.__main__ import main


class TranscribeRoleTests(unittest.TestCase):
    def test_key_only_named_by_transcribe_worker_configuration(self) -> None:
        root = Path(__file__).parents[1] / 'src' / 't6'
        holders = []
        for path in root.rglob('*.py'):
            tree = ast.parse(path.read_text())
            if any(isinstance(node, ast.Constant) and isinstance(node.value, str) and re.fullmatch(r'[A-Z0-9_]*ELEVENLABS[A-Z0-9_]*KEY[A-Z0-9_]*', node.value) for node in ast.walk(tree)):
                holders.append(path.relative_to(root).as_posix())
        self.assertEqual(holders, ['transcribe/worker.py'])

    def test_transcribe_role_never_opens_database(self) -> None:
        stop = threading.Event()
        stop.set()
        settings = {'T6_SERVER_URL': 'http://example.invalid', 'T6_WORKER_SECRET': 'secret', 'T6_WORKER_QUEUE': 'transcribe', 'T6_DATABASE': '/unavailable'}
        class Transport:
            def send(self, *args: object) -> object:
                raise AssertionError('no requests expected')
        with patch('t6.transcribe.worker.configured', return_value={'transcript.preview': lambda job: None}), patch('t6.worker.internal_queue.InternalJobQueue', side_effect=AssertionError('database opened')), patch('sqlite3.connect', side_effect=AssertionError('database opened')):
            self.assertEqual(main(settings, transport=Transport(), stop=stop), 0)  # type: ignore[arg-type]
