"""`python -m t6.server`: the Notion verification token reaches the operator."""

import contextlib
import io
import json
import unittest

from t6.server.__main__ import _print_notion_token
from t6.server.app import Request, ServerConfig, WebhookApp
from t6.server.queue import JobQueue


class NotionHandshakeTest(unittest.TestCase):
    def test_token_is_printed_to_stderr_before_the_secret_is_set(self) -> None:
        app = WebhookApp(JobQueue(":memory:"), ServerConfig(), on_notion_verification=_print_notion_token)
        body = json.dumps({"verification_token": "secret_abc"}).encode()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            response = app.public(Request("POST", "/webhook/notion", {}, body))
        self.assertEqual(response.status, 200)
        self.assertIn("Notion verification token: secret_abc", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
