"""The Concerns export on the VPS server (edge mode): the tailnet push and the token-gated public read (#734)."""
from dataclasses import replace
import json
import os
import threading
import urllib.error
import urllib.request
from pathlib import Path

from t6.server import Request, Response, WebhookApp
from t6.server.http import make_server
from test_server_app import ServerTestCase

PUSH = "push-secret"
TOKEN = "read-token"
CSV = "Concern id,revision\r\nt6c_1,1\r\n".encode()
PUBLIC = "/concerns/export.csv"
PUSH_PATH = "/concerns/export"


class ConcernExportServerTests(ServerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.file = Path(self.directory.name) / "export" / "concerns.csv"
        self.start()

    def start(self, push: str = PUSH, token: str = TOKEN) -> None:
        config = replace(self.app._config, edge_routes=(), concerns_export_push_secret=push, concerns_export_read_token=token, concerns_export_file=str(self.file))
        self.app = WebhookApp(self.queue, config, self.clock, self.tokens.append)

    def push(self, body: bytes = CSV, secret: str | None = PUSH, method: str = "PUT") -> Response:
        return self.app.tailnet(Request(method, PUSH_PATH, {"Authorization": f"Bearer {secret}"} if secret else {}, body))

    def read(self, token: str | None = TOKEN, method: str = "GET", path: str = PUBLIC) -> Response:
        return self.app.public(Request(method, path, {"X-T6-Read-Token": token} if token is not None else {}))

    def test_a_pushed_export_is_served_to_the_read_token(self) -> None:
        self.assertEqual(self.push().status, 200)
        answer = self.read()
        self.assertEqual((answer.status, answer.raw, answer.content_type), (200, CSV, "text/csv; charset=utf-8"))

    def test_the_read_path_is_404_without_the_token_with_a_wrong_token_or_before_any_push(self) -> None:
        self.assertEqual(self.read().status, 404)  # right token, no file yet
        self.push()
        for token in (None, "", "wrong", TOKEN + "x"):
            with self.subTest(token=token):
                self.assertEqual(self.read(token).status, 404)
        self.assertEqual(self.read(method="POST").status, 404)
        self.assertEqual(self.read(path="/concerns/other.csv").status, 404)
        self.assertEqual(self.read(path="/concerns/").status, 404)

    def test_an_empty_read_token_setting_turns_the_read_path_off(self) -> None:
        self.start(token="")
        self.push()
        self.assertEqual(self.read("").status, 404)
        self.assertEqual(self.read(None).status, 404)

    def test_the_push_route_refuses_a_missing_or_wrong_bearer_secret_and_stores_nothing(self) -> None:
        self.assertEqual(self.push(secret=None).status, 401)
        self.assertEqual(self.push(secret="wrong").status, 401)
        self.assertEqual(self.push(secret="worker").status, 401)
        self.assertFalse(self.file.exists())
        self.assertEqual(self.read().status, 404)

    def test_an_empty_push_secret_setting_turns_the_push_route_off(self) -> None:
        self.start(push="")
        self.assertEqual(self.push(secret="").status, 503)
        self.assertEqual(self.push(secret=None).status, 503)
        self.assertFalse(self.file.exists())

    def test_the_push_route_takes_only_put_with_a_body_and_is_not_on_the_public_listener(self) -> None:
        self.assertEqual(self.push(method="GET").status, 405)
        self.assertEqual(self.push(body=b"").status, 400)
        self.assertEqual(self.app.public(Request("PUT", PUSH_PATH, {"Authorization": f"Bearer {PUSH}"}, CSV)).status, 404)
        self.assertEqual(self.app.tailnet(Request("GET", PUBLIC, {"X-T6-Read-Token": TOKEN})).status, 404)

    def test_a_second_push_replaces_the_file_and_leaves_no_other_file_behind(self) -> None:
        self.push()
        newer = "Concern id,revision\r\nt6c_2,7\r\n".encode()
        self.push(newer)
        self.assertEqual(self.read().raw, newer)
        self.assertEqual(os.listdir(self.file.parent), ["concerns.csv"])

    def test_a_failed_write_keeps_the_previous_export(self) -> None:
        self.push()
        from unittest.mock import patch
        with patch("os.replace", side_effect=OSError("disk")):
            self.assertEqual(self.push(b"Concern id,revision\r\nbroken\r\n").status, 500)
        self.assertEqual(self.read().raw, CSV)
        self.assertEqual(os.listdir(self.file.parent), ["concerns.csv"])  # the temporary file is removed

    def test_settings_come_from_the_environment(self) -> None:
        from t6.server import ServerConfig
        config = ServerConfig.from_env({"T6_CONCERNS_EXPORT_PUSH_SECRET": "a", "T6_CONCERNS_EXPORT_READ_TOKEN": "b", "T6_CONCERNS_EXPORT_FILE": "/data/c.csv"})
        self.assertEqual((config.concerns_export_push_secret, config.concerns_export_read_token, config.concerns_export_file), ("a", "b", "/data/c.csv"))
        self.assertEqual(ServerConfig.from_env({}).concerns_export_file, "t6-concerns-export.csv")

    def test_over_real_sockets_the_export_is_served_as_csv_not_json(self) -> None:
        tail, public = make_server(("127.0.0.1", 0), self.app.tailnet), make_server(("127.0.0.1", 0), self.app.public)
        for server in (tail, public):
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        put = urllib.request.Request(f"http://127.0.0.1:{tail.server_address[1]}{PUSH_PATH}", data=CSV, method="PUT", headers={"Authorization": f"Bearer {PUSH}"})
        with urllib.request.urlopen(put) as response:
            self.assertEqual(json.loads(response.read())["bytes"], len(CSV))
        get = urllib.request.Request(f"http://127.0.0.1:{public.server_address[1]}{PUBLIC}", headers={"X-T6-Read-Token": TOKEN})
        with urllib.request.urlopen(get) as response:
            self.assertEqual((response.headers["Content-Type"], response.read()), ("text/csv; charset=utf-8", CSV))
        with self.assertRaises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{public.server_address[1]}{PUBLIC}"))
        self.assertEqual(refused.exception.code, 404)
