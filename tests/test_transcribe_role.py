import ast
import contextlib
import io
import json
import re
import tempfile
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


class TranscribeSharesStartupTests(unittest.TestCase):
    def start(self, shares: str | None, *, write: bool = True) -> tuple[int, str]:
        stop = threading.Event()
        stop.set()
        errors = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'shares.json'
            settings = {'T6_SERVER_URL': 'http://example.invalid', 'T6_WORKER_SECRET': 'secret', 'T6_WORKER_QUEUE': 'transcribe', 'ELEVENLABS_API_KEY': 'synthetic-key'}
            if shares is not None:
                if write:
                    path.write_text(shares)
                settings['T6_TRANSCRIBE_SHARES'] = str(path)
            class Transport:
                def send(self, *args: object) -> object:
                    raise AssertionError('no requests expected')
            with contextlib.redirect_stderr(errors), patch('elevenlabs.client.ElevenLabs'):
                code = main(settings, transport=Transport(), stop=stop)  # type: ignore[arg-type]
        return code, errors.getvalue()

    def test_valid_mapping_starts(self) -> None:
        self.assertEqual(self.start(json.dumps({'Alpha': '/mnt/alpha'})), (0, ''))

    def test_each_bad_mapping_exits_2_naming_the_variable(self) -> None:
        cases = {
            'unset': self.start(None),
            'unreadable': self.start('{}', write=False),
            'not json': self.start('{nope'),
            'not an object': self.start(json.dumps(['/mnt/alpha'])),
            'value not a string': self.start(json.dumps({'Alpha': 7})),
            'relative mount point': self.start(json.dumps({'Alpha': 'alpha'})),
            'empty': self.start('{}'),
        }
        for label, (code, message) in cases.items():
            with self.subTest(label):
                self.assertEqual(code, 2)
                self.assertIn('T6_TRANSCRIBE_SHARES', message)
