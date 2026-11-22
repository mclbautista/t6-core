"""The CLI waits long enough for slow verbs, and a timeout names the setting that raises it (#596)."""
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from t6.client import Client, Unavailable, cli_timeout


class CliTimeoutTests(unittest.TestCase):
    def test_default_is_10_seconds_and_concern_scan_gets_600(self) -> None:
        self.assertEqual(cli_timeout('project', 'notes', {}), 10.0)
        self.assertEqual(cli_timeout('concern', 'scan', {}), 600.0)

    def test_the_setting_overrides_every_verb(self) -> None:
        env = {'T6_API_TIMEOUT_SECONDS': '45'}
        self.assertEqual(cli_timeout('project', 'notes', env), 45.0)
        self.assertEqual(cli_timeout('concern', 'scan', env), 45.0)

    def test_anything_but_a_positive_number_stops(self) -> None:
        for value in ('0', '-1', 'soon', 'nan', 'inf'):
            with self.subTest(value=value), self.assertRaises(SystemExit) as stop:
                cli_timeout('concern', 'scan', {'T6_API_TIMEOUT_SECONDS': value})
            self.assertIn('T6_API_TIMEOUT_SECONDS', str(stop.exception))

    def test_registered_verbs_use_the_cli_timeout(self) -> None:
        import argparse
        from unittest.mock import patch
        from t6 import verbs
        entry = next(v for v in verbs.VERBS if (v.noun, v.verb) == ('concern', 'scan'))
        seen: list[float] = []

        class Recording(Client):
            def call(self, noun: str, verb: str, inputs: object) -> verbs.Refused:
                seen.append(self.timeout)
                return verbs.Refused(rule='fake', reason='fake')
        with patch('t6.client.Client', Recording):
            verbs.run(argparse.Namespace(registered_verb=entry, project='p', owner='o'), lambda *a, **k: None)
        self.assertEqual(seen, [600.0])

    def test_a_timed_out_cli_call_names_the_setting(self) -> None:
        class Slow(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                self.rfile.read(int(self.headers.get('Content-Length', 0)))
                time.sleep(0.5)
                body = json.dumps({'outcome': 'Refused', 'rule': 'fake', 'reason': 'fake'}).encode()
                try:
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except BrokenPipeError:  # the client gave up first
                    pass

            def log_message(self, *args: object) -> None:
                pass

        server = HTTPServer(('127.0.0.1', 0), Slow)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        from t6.verbs import ConcernScanInput
        reply = Client(f'http://127.0.0.1:{server.server_port}', 'made-up', timeout=0.1).call('concern', 'scan', ConcernScanInput(project='p', owner='o'))
        assert isinstance(reply, Unavailable)
        self.assertIn('after 0.1 s; raise T6_API_TIMEOUT_SECONDS', reply.reason)


if __name__ == '__main__':
    unittest.main()
