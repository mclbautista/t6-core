"""Config-driven edge intake and its listener boundaries."""
from dataclasses import replace
import json
from pathlib import Path

from t6.server import Request, WebhookApp
from t6.server.routes import load_routes
from test_server_app import ServerTestCase, comment, frameio_request, notion_event, notion_request


class EdgeTests(ServerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.file = Path(self.directory.name) / "routes.yaml"
        self.file.write_text("""
- {path: /events/frame, verifier: frameio_v0, secret_env: FRAME, job_kind: frame.event, queue: frameio}
- {path: /events/notion, verifier: notion, secret_env: NOTION, job_kind: notion.event, queue: main}
- {path: /events/google, verifier: google_channel, secret_env: GOOGLE, job_kind: calendar.event, queue: main}
""")
        self.env = {"FRAME": "frameio-secret", "NOTION": "notion-token", "GOOGLE": "google-secret"}
        config = replace(self.app._config, edge_routes=load_routes(str(self.file), self.env))
        self.app = WebhookApp(self.queue, config, self.clock, self.tokens.append)

    def test_each_verifier_queues_and_deduplicates(self) -> None:
        frame = replace(frameio_request(self.clock, {"id": "frame-1", "type": "file.versioned"}), path="/events/frame")
        notion = replace(notion_request(self.clock, notion_event(self.clock)), path="/events/notion")
        google = Request("POST", "/events/google", {"X-Goog-Channel-Token": "google-secret", "X-Goog-Channel-Id": "ch", "X-Goog-Message-Number": "1"})
        for request, queue, kind in [(frame, "frameio", "frame.event"), (notion, "main", "notion.event"), (google, "main", "calendar.event")]:
            with self.subTest(kind=kind):
                self.assertTrue(self.app.public(request).body["created"])
                self.assertFalse(self.app.public(request).body["created"])
                job = self.queue.claim(queue, 60)
                assert job is not None
                self.assertEqual(job.kind, kind)
                if kind == "calendar.event":
                    headers = job.payload["headers"]
                    assert isinstance(headers, dict)
                    self.assertNotIn("x-goog-channel-token", headers)

    def test_bad_or_missing_signatures_queue_nothing(self) -> None:
        for path in ("frame", "notion", "google"):
            for headers in ({}, {"X-Frameio-Signature": "bad", "X-Notion-Signature": "bad", "X-Goog-Channel-Token": "bad"}):
                self.assertEqual(self.app.public(Request("POST", "/events/" + path, headers, b"{}")).status, 401)
        self.assertEqual(self.pending(), 0)
        self.assertEqual(self.pending("frameio"), 0)

    def test_handshake_and_boundaries(self) -> None:
        response = self.app.public(Request("POST", "/events/notion", {}, json.dumps({"verification_token": "token"}).encode()))
        self.assertEqual(response.status, 401)
        self.assertEqual(self.tokens, [])
        for path in ("/tap", "/registry/projects", "/operator/offers", "/review/timers", "/unknown"):
            self.assertEqual(self.app.tailnet(Request("POST", path)).status, 404)
            self.assertEqual(self.app.public(Request("POST", path)).status, 404)
        self.assertEqual(self.app.public(Request("GET", "/health")).status, 200)

    def test_example_routes_deliver_worker_payloads(self) -> None:
        routes = load_routes("docs/edge-routes.example.yaml", {"FRAMEIO_WEBHOOK_SECRET": "frameio-secret", "NOTION_WEBHOOK_TOKEN": "notion-token"})
        app = WebhookApp(self.queue, replace(self.app._config, edge_routes=routes), self.clock, self.tokens.append)
        frame = replace(frameio_request(self.clock, comment("c1")), path="/events/frameio/comments")
        notion = replace(notion_request(self.clock, notion_event(self.clock)), path="/events/notion/pages")
        self.assertEqual(app.public(frame).status, 200)
        self.assertEqual(app.public(notion).status, 200)
        f = self.queue.claim("frameio", 60)
        n = self.queue.claim("main", 60)
        assert f is not None and n is not None
        self.assertEqual((f.kind, dict(f.payload)), ("frameio.resolve_comment", {"comment_id": "c1", "account_id": "acc-1", "frameio_project_id": "fp1"}))
        self.assertEqual((n.kind, n.payload["page_id"]), ("notion.page_changed", "page-1"))

    def test_invalid_config_stops_startup(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown verifier"):
            self.file.write_text("- {path: /x, verifier: unknown, secret_env: FRAME, job_kind: x, queue: main}")
            load_routes(str(self.file), self.env)
        self.file.write_text("- {path: /x, verifier: notion, secret_env: MISSING, job_kind: x, queue: main}")
        with self.assertRaisesRegex(ValueError, "MISSING"):
            load_routes(str(self.file), self.env)

    def test_bootstrap_and_queue_validation(self) -> None:
        self.file.write_text("- {path: /x, verifier: notion, secret_env: MISSING, job_kind: x, queue: main}")
        routes = load_routes(str(self.file), {"T6_NOTION_BOOTSTRAP": "1"})
        app = WebhookApp(self.queue, replace(self.app._config, edge_routes=routes), self.clock, self.tokens.append)
        self.assertEqual(app.public(Request("POST", "/x", {}, b'{"verification_token":"new"}')).status, 200)
        self.assertEqual(self.tokens[-1], "new")
        self.assertNotEqual(app.public(Request("POST", "/x", {}, b"{}")).status, 200)
        self.file.write_text("- {path: /x, verifier: frameio_v0, secret_env: MISSING, job_kind: x, queue: main}")
        with self.assertRaisesRegex(ValueError, "MISSING"):
            load_routes(str(self.file), {"T6_NOTION_BOOTSTRAP": "1"})
        self.file.write_text("- {path: /x, verifier: notion, secret_env: NOTION, job_kind: x, queue: typo}")
        with self.assertRaisesRegex(ValueError, "queue"):
            load_routes(str(self.file), self.env)
