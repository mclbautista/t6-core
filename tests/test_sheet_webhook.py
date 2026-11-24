"""The Concerns sheet's signed Submit on the VPS server: verified, then queued as `sheets.concerns_submit` on main (#735)."""
from dataclasses import replace
import hashlib
import hmac
import json
from typing import Any

from t6.server import Request, WebhookApp
from t6.server.routes import load_routes
from test_server_app import ServerTestCase

SECRET = "sheet-secret"
PATH = "/webhook/sheets/concerns"
BODY = {"id": "click-1", "email": "cyril@example.com", "rows": [{"concern": "t6c_1", "revision": 3, "action": "Close", "note": ""}]}


class SheetWebhookTests(ServerTestCase):
    def setUp(self) -> None:
        super().setUp()
        routes = load_routes("docs/edge-routes.example.yaml", {"FRAMEIO_WEBHOOK_SECRET": "f", "NOTION_WEBHOOK_TOKEN": "n", "T6_GOOGLE_CHANNEL_TOKEN": "g", "T6_SHEET_SUBMIT_SECRET": SECRET})
        self.app = WebhookApp(self.queue, replace(self.app._config, edge_routes=routes), self.clock, self.tokens.append)

    def request(self, body: dict[str, Any] | bytes = BODY, *, secret: str = SECRET, age: float = 0, headers: dict[str, str] | None = None) -> Request:
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        stamp = str(int(self.clock() - age))
        digest = hmac.new(secret.encode(), stamp.encode() + b":" + raw, hashlib.sha256).hexdigest()  # the documented signing format
        return Request("POST", PATH, {"X-T6-Sheet-Timestamp": stamp, "X-T6-Sheet-Signature": digest, **(headers or {})}, raw)

    def test_a_signed_fresh_submit_is_queued_on_main_with_its_payload(self) -> None:
        answer = self.app.public(self.request())
        self.assertEqual((answer.status, answer.body["created"]), (200, True))
        job = self.queue.claim("main", 60)
        assert job is not None
        self.assertEqual((job.kind, dict(job.payload)), ("sheets.concerns_submit", BODY))

    def test_a_bad_signature_is_refused_and_nothing_is_queued(self) -> None:
        self.assertEqual(self.app.public(self.request(secret="wrong")).status, 401)
        self.assertEqual(self.pending(), 0)

    def test_a_missing_header_is_refused_and_nothing_is_queued(self) -> None:
        for dropped in ("X-T6-Sheet-Timestamp", "X-T6-Sheet-Signature"):
            with self.subTest(dropped=dropped):
                request = self.request()
                request = replace(request, headers={k: v for k, v in request.headers.items() if k != dropped})
                self.assertEqual(self.app.public(request).status, 401)
        self.assertEqual(self.app.public(Request("POST", PATH, {}, b"{}")).status, 401)
        self.assertEqual(self.pending(), 0)

    def test_a_stale_or_future_timestamp_is_refused_even_when_signed(self) -> None:
        for age in (301, -301):
            with self.subTest(age=age):
                self.assertEqual(self.app.public(self.request(age=age)).status, 401)
        self.assertEqual(self.pending(), 0)
        self.assertEqual(self.app.public(self.request(age=299)).status, 200)

    def test_a_body_changed_after_signing_is_refused(self) -> None:
        signed = self.request()
        self.assertEqual(self.app.public(replace(signed, body=signed.body.replace(b"Close", b"Keep "))).status, 401)
        self.assertEqual(self.pending(), 0)

    def test_the_frameio_signature_headers_do_not_pass_this_route(self) -> None:
        request = self.request(headers={"X-Frameio-Signature": "v0=00"})
        request = replace(request, headers={k: v for k, v in request.headers.items() if k != "X-T6-Sheet-Signature"})
        self.assertEqual(self.app.public(request).status, 401)

    def test_a_retry_of_the_same_click_is_queued_once_and_another_click_is_queued_again(self) -> None:
        self.assertTrue(self.app.public(self.request()).body["created"])
        self.assertFalse(self.app.public(self.request()).body["created"])
        self.assertTrue(self.app.public(self.request({**BODY, "id": "click-2"})).body["created"])

    def test_a_signed_body_that_is_not_a_json_object_is_a_400_and_queues_nothing(self) -> None:
        self.assertEqual(self.app.public(self.request(b"[1]")).status, 400)
        self.assertEqual(self.pending(), 0)

    def test_the_route_takes_post_only(self) -> None:
        self.assertEqual(self.app.public(replace(self.request(), method="GET")).status, 405)
