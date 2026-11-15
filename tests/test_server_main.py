"""The server entry point hands Notion's one-time token to the operator."""

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from t6.server import Request
from t6.server.__main__ import main


class NotionHandshakeTest(unittest.TestCase):
    def test_entry_point_prints_signed_handshake_in_both_modes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            routes = Path(directory) / "routes.yaml"
            routes.write_text('- {path: /events/notion, verifier: notion, secret_env: NOTION, job_kind: notion.event, queue: main}')
            for mode in ("full", "edge"):
                with self.subTest(mode=mode):
                    env = {"T6_SERVER_MODE": mode, "T6_ROUTES_FILE": str(routes), "T6_NOTION_BOOTSTRAP": "1", "T6_QUEUE_DB": ":memory:", "T6_REGISTRY_DB": ":memory:"}
                    stderr = io.StringIO()
                    with patch.dict(os.environ, env, clear=True), patch("t6.server.__main__.serve") as serve, patch("t6.server.__main__.make_server") as make_server, patch("t6.server.__main__.threading.Thread"), contextlib.redirect_stderr(stderr):
                        self.assertEqual(main(), 0)
                        app = serve.call_args.args[0] if mode == "edge" else make_server.call_args_list[-1].args[1].__self__
                        path = "/events/notion" if mode == "edge" else "/webhook"
                        response = app.public(Request("POST", path, {"X-Notion-Signature": "sha256=00"}, b'{"verification_token":"secret_abc"}'))
                    self.assertEqual(response.status, 200)
                    self.assertIn("Notion verification token: secret_abc", stderr.getvalue())


class TranslateSwitchTest(unittest.TestCase):
    def test_transcript_translate_is_off_unless_the_env_switch_is_true(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            routes = Path(directory) / "routes.yaml"
            routes.write_text('- {path: /events/notion, verifier: notion, secret_env: NOTION, job_kind: notion.event, queue: main}')
            for value, expected in ((None, False), ("false", False), ("true", True)):
                with self.subTest(value=value):
                    env = {"T6_SERVER_MODE": "full", "T6_ROUTES_FILE": str(routes), "T6_QUEUE_DB": ":memory:", "T6_REGISTRY_DB": ":memory:"}
                    if value is not None:
                        env["T6_TRANSCRIPT_TRANSLATE"] = value
                    with patch.dict(os.environ, env, clear=True), patch("t6.server.__main__.serve"), patch("t6.server.__main__.make_server"), patch("t6.server.__main__.threading.Thread"), patch("t6.server.__main__.VerbAPI") as api, contextlib.redirect_stderr(io.StringIO()):
                        self.assertEqual(main(), 0)
                    self.assertIs(api.call_args.kwargs["translate_enabled"], expected)
