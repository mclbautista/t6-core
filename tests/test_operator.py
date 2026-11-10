"""Operator actions: Sixto cards that register, bind and classify Projects, end to end in one process.

The server app, a worker with a fake gateway, and taps posted to `/tap` exactly as the
gateway pushes them. No test opens a connection.
"""

import json
from typing import Any

from test_registry_codes import CodesTestCase
from test_registry_service import READER_SECRET, WORKER_SECRET, WRITER_SECRET
from test_server_app import TAP_SECRET, InProcess

from t6.operator import ActionStore, OperatorService
from t6.registry import SqliteProjectRegistry
from t6.server import Grant, Request, Response, ServerConfig, WebhookApp
from t6.worker import QueueClient, Worker
from t6.worker.gateway import GatewayClient
from t6.worker.operator import CardDelivery, OperatorClient

OPERATOR = 42
OUTSIDER = 99
PAGE_A = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
PAGE_B = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
GATEWAY_SECRET = "gateway-secret"
CALLBACK_PREFIX = "mod:t6core:"


class FakeGateway:
    """Records what the worker sends. `settle` holds messages in `queued` until it is called."""

    def __init__(self) -> None:
        self.messages: dict[int, dict[str, Any]] = {}
        self.keys: dict[str, int] = {}
        self.queued = False
        self.sends = 0

    def send(self, method: str, path: str, headers: Any, body: bytes) -> tuple[int, dict[str, Any]]:
        if headers.get("Authorization") != f"Bearer {GATEWAY_SECRET}":
            return 401, {}
        if method == "GET":
            message = self.messages[int(path.rsplit("/", 1)[1])]
            return 200, {"state": "queued" if self.queued else "sent", "telegram_message_id": message["telegram_id"]}
        request = json.loads(body)
        if request["idempotency_key"] in self.keys:
            return 202, {"message_ref": self.keys[request["idempotency_key"]]}
        self.sends += 1
        if path == "/notify":
            ref = len(self.messages) + 1
            self.messages[ref] = {"telegram_id": 1000 + ref, "chat": request["to"]}
        else:
            ref = request["message_ref"]
        self.keys[request["idempotency_key"]] = ref
        self.messages[ref].update(text=request["text"], buttons=request.get("buttons") or [])
        return 202, {"message_ref": ref}


class OperatorTestCase(CodesTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.config = ServerConfig(
            tap_secret=TAP_SECRET,
            operator_ids=(OPERATOR,),
            grants=(
                Grant(
                    WORKER_SECRET,
                    claim=frozenset({"main"}),
                    submit=frozenset({"main"}),
                    registry_read=True,
                    registry_sync=True,
                    operator=True,
                    actor="main-worker",
                ),
                Grant(WRITER_SECRET, registry_read=True, registry_write=True, actor="writer"),
                Grant(READER_SECRET, registry_read=True, actor="reader"),
            ),
        )
        self.gateway = FakeGateway()
        self.start()
        self.updates = 0

    def start(self, **registry_options: Any) -> None:
        """Open the files and build the app, as a server start or restart does."""
        self.registry = SqliteProjectRegistry(self.path, self.clock, **registry_options)
        self.store = ActionStore(str(self.path.with_name("operator.sqlite3")), self.clock)
        # The base setUp builds the app once before this class's config exists; it gets no operator.
        ids = self.config.operator_ids
        service = OperatorService(self.registry, self.store, ids, clock=self.clock) if ids else None
        self.app = WebhookApp(self.queue, self.config, self.clock, registry=self.registry, operator=service)

    # -- the worker side ---------------------------------------------------------

    def worker(self, gateway: FakeGateway | None = None) -> Worker:
        transport = InProcess(self.app)
        delivery = CardDelivery(
            OperatorClient(transport, WORKER_SECRET), GatewayClient(gateway or self.gateway, GATEWAY_SECRET)
        )
        return Worker(QueueClient(transport, WORKER_SECRET), "main", delivery.handlers())

    def drain(self) -> int:
        worker, ran = self.worker(), 0
        while worker.run_once():
            ran += 1
            self.clock.advance(60)  # past the retry delay of a job that failed
        return ran

    def offer(self, kind: str = "unbound_page", **body: Any) -> str:
        answer = self.send("POST", "/operator/offers", {"kind": kind, **body}, secret=WORKER_SECRET)
        self.assertEqual(answer.status, 200, answer.body)
        self.assertIs(answer.body["offered"], True)
        return str(answer.body["token"])

    def offer_and_deliver(self, page: str = PAGE_A, label: str = "Show") -> str:
        token = self.offer(page_id=page, label=label)
        self.drain()
        return token

    def card(self, ref: int = 1) -> dict[str, Any]:
        return self.gateway.messages[ref]

    def payload(self, ref: int, choice_text: str) -> str:
        """The callback data Telegram would send back for the button with this text."""
        for row in self.card(ref)["buttons"]:
            for button in row:
                if button["text"] == choice_text:
                    return CALLBACK_PREFIX + str(button["payload"])
        raise AssertionError(f"no button {choice_text!r} in {self.card(ref)['buttons']}")

    # -- the gateway side --------------------------------------------------------

    def tap(
        self,
        payload: str,
        *,
        who: int = OPERATOR,
        chat: int = OPERATOR,
        message_id: int | None = 1001,
        key: str | None = None,
    ) -> Response:
        """Push a tap the way the gateway does: callback prefix removed, same key on every retry."""
        assert payload.startswith(CALLBACK_PREFIX)
        self.updates += 1
        body = {
            "kind": "tap",
            "data": payload[len(CALLBACK_PREFIX) :],
            "from": {"id": who},
            "chat": chat,
            "message_id": message_id,
            "update_id": self.updates,
        }
        headers = {"Authorization": f"Bearer {TAP_SECRET}", "Idempotency-Key": key or f"tap-{self.updates}"}
        return self.app.tailnet(Request("POST", "/tap", headers, json.dumps(body).encode()))

    def projects(self) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = self.send("GET", "/registry/unclassified").body["projects"]  # type: ignore[assignment]
        return found + [p for p in self.all_projects() if p["rotation"] != "unclassified"]

    def all_projects(self) -> list[dict[str, Any]]:
        return [
            self.read({"id": p.id}) for p in self.registry.list_projects()
        ]

    def audit_actors(self, project_id: str) -> set[str]:
        return {entry.actor for entry in self.registry.audit(project_id)}


class UnboundPageTests(OperatorTestCase):
    def test_a_card_is_sent_once_with_buttons_within_telegrams_limit(self) -> None:
        token = self.offer_and_deliver(label="Show <One> & Two")
        self.assertEqual(self.gateway.sends, 1)
        self.assertIn("Show &lt;One&gt; &amp; Two", self.card()["text"])
        buttons = [b for row in self.card()["buttons"] for b in row]
        self.assertEqual([b["text"] for b in buttons], ["New Project", "Existing Project", "Skip"])
        for button in buttons:
            self.assertTrue(button["payload"].startswith(f"op:{token}:"))
            self.assertLessEqual(len((CALLBACK_PREFIX + button["payload"]).encode()), 64)

        # The same page offered again (a second webhook) sends nothing new.
        again = self.send("POST", "/operator/offers", {"kind": "unbound_page", "page_id": PAGE_A}, secret=WORKER_SECRET)
        self.assertEqual((again.body["created"], again.body["token"]), (False, token))
        self.drain()
        self.assertEqual(self.gateway.sends, 1)

    def test_new_identity_registers_out_of_rotation_with_no_code_and_the_card_reports_notion(self) -> None:
        self.offer_and_deliver()
        answer = self.tap(self.payload(1, "New Project"))
        self.assertEqual((answer.status, answer.body["outcome"]), (200, "registered"))

        (project,) = self.all_projects()
        self.assertEqual((project["rotation"], project["code"]), ("out", None))
        self.assertEqual([b["page_id"] for b in project["bindings"]], [PAGE_A])
        self.assertEqual(self.audit_actors(project["id"]), {f"telegram:{OPERATOR}"})

        self.drain()  # the card is edited: accepted, Notion still pending
        self.assertEqual(self.card()["buttons"], [])
        self.assertIn("Registered as a new Project", self.card()["text"])
        self.assertIn("Notion update pending", self.card()["text"])
        self.assertEqual(self.gateway.sends, 2)

    def test_existing_identity_gets_the_new_page_and_the_old_one_stays_legacy(self) -> None:
        old = self.new("ABC", "in", page=PAGE_B)
        self.registry.observe_page(PAGE_B, actor="t", available=False)  # the old page was deleted
        self.offer_and_deliver()
        picking = self.tap(self.payload(1, "Existing Project"))
        self.assertEqual(picking.body["outcome"], "choose_project")
        self.drain()
        self.assertEqual([b["text"] for row in self.card()["buttons"] for b in row], ["Bind to ABC", "Skip"])

        self.assertEqual(self.tap(self.payload(1, "Bind to ABC")).body["outcome"], "bound")
        project = self.read(old)
        self.assertEqual(project["id"], old["id"])  # identity unchanged
        self.assertEqual(
            sorted((b["page_id"], b["status"]) for b in project["bindings"]),
            [(PAGE_A, "active"), (PAGE_B, "legacy")],
        )
        self.assertEqual((project["code"], project["rotation"]), ("ABC", "in"))

    def test_only_projects_without_a_working_page_are_offered(self) -> None:
        self.new("ABC", "in", page=PAGE_B)  # has a page, so it is not offered
        self.offer_and_deliver()
        self.assertEqual(self.tap(self.payload(1, "Existing Project")).body["outcome"], "no_candidates")
        self.drain()
        self.assertIn("No Project is waiting for a page", self.card()["text"])
        self.assertEqual(len(self.all_projects()), 1)

    def test_skip_changes_nothing(self) -> None:
        self.offer_and_deliver()
        self.assertEqual(self.tap(self.payload(1, "Skip")).body["outcome"], "skipped")
        self.assertEqual(self.all_projects(), [])

    def test_a_page_registered_elsewhere_in_the_meantime_is_not_taken_twice(self) -> None:
        self.offer_and_deliver()
        other = self.new(page=PAGE_A)
        self.assertEqual(self.tap(self.payload(1, "New Project")).body["outcome"], "page_taken")
        self.assertEqual(len(self.all_projects()), 1)
        self.assertEqual(self.all_projects()[0]["id"], other["id"])

    def test_candidates_that_changed_since_the_card_was_sent_are_refused(self) -> None:
        project = self.new("ABC", "in")
        self.offer_and_deliver()
        self.tap(self.payload(1, "Existing Project"))
        self.drain()
        self.assertEqual(self.set_code(project, "XYZ").status, 200)  # revision moved on
        self.assertEqual(self.tap(self.payload(1, "Bind to ABC")).body["outcome"], "stale")
        self.assertEqual(self.read(project)["bindings"], [])


class SafetyTests(OperatorTestCase):
    def test_a_tap_from_someone_not_on_the_allow_list_changes_nothing(self) -> None:
        self.offer_and_deliver()
        answer = self.tap(self.payload(1, "New Project"), who=OUTSIDER)
        self.assertEqual((answer.status, answer.body["outcome"]), (200, "rejected"))  # 200: the gateway stops retrying
        self.assertEqual(self.all_projects(), [])
        self.assertEqual(self.app._operator.store.rejections()[-1][:2], ("not_allowed", OUTSIDER))  # type: ignore[union-attr]
        # The card is still live for the operator.
        self.assertEqual(self.tap(self.payload(1, "New Project")).body["outcome"], "registered")

    def test_a_tap_from_another_chat_or_message_is_rejected(self) -> None:
        self.offer_and_deliver()
        data = self.payload(1, "New Project")
        self.assertEqual(self.tap(data, chat=OUTSIDER).body["outcome"], "rejected")
        self.assertEqual(self.tap(data, message_id=5).body["outcome"], "rejected")
        self.assertEqual(self.all_projects(), [])

    def test_an_unknown_or_made_up_token_is_rejected(self) -> None:
        self.assertEqual(self.tap(CALLBACK_PREFIX + "op:nope:reg").body["outcome"], "rejected")
        self.offer_and_deliver()
        self.assertEqual(self.tap(CALLBACK_PREFIX + "op:nope:reg").body["outcome"], "rejected")
        self.assertEqual(self.all_projects(), [])

    def test_an_unknown_choice_is_rejected_and_the_card_stays_live(self) -> None:
        self.offer_and_deliver()
        payload = self.payload(1, "New Project").rsplit(":", 1)[0] + ":wipe"
        self.assertEqual(self.tap(payload).body["outcome"], "rejected")
        self.assertEqual(self.tap(self.payload(1, "New Project")).body["outcome"], "registered")

    def test_a_card_is_single_use_and_a_redelivered_tap_does_nothing_more(self) -> None:
        self.offer_and_deliver()
        data = self.payload(1, "New Project")
        self.assertEqual(self.tap(data, key="tap-1").body["outcome"], "registered")
        self.assertEqual(self.tap(data, key="tap-1").body["outcome"], "already_handled")  # gateway retry
        self.assertEqual(self.tap(data, key="tap-2").body["outcome"], "already_handled")  # a second tap
        self.assertEqual(len(self.all_projects()), 1)

    def test_a_stale_stage_is_refused(self) -> None:
        self.new("ABC", "in")
        self.offer_and_deliver()
        old_choice = self.payload(1, "Existing Project")
        self.tap(old_choice)
        self.assertEqual(self.tap(self.payload(1, "Skip")).body["outcome"], "skipped")
        self.assertEqual(self.tap(old_choice).body["outcome"], "already_handled")

    def test_an_expired_card_changes_nothing_and_is_closed(self) -> None:
        self.offer_and_deliver()
        self.clock.advance(8 * 24 * 3600)
        self.assertEqual(self.tap(self.payload(1, "New Project")).body["outcome"], "expired")
        self.assertEqual(self.all_projects(), [])
        self.drain()
        self.assertIn("expired", self.card()["text"])
        self.assertEqual(self.card()["buttons"], [])

    def test_a_tap_before_the_gateway_confirmed_the_send_is_retried_later(self) -> None:
        token = self.offer(page_id=PAGE_A)  # not delivered yet
        answer = self.tap(f"{CALLBACK_PREFIX}op:{token}:reg", message_id=1001)
        self.assertEqual(answer.status, 503)
        self.assertEqual(self.all_projects(), [])

    def test_a_tap_works_after_a_restart(self) -> None:
        self.offer_and_deliver()
        data = self.payload(1, "New Project")
        self.start()
        self.assertEqual(self.tap(data).body["outcome"], "registered")
        self.start()
        self.assertEqual(self.tap(data).body["outcome"], "already_handled")
        self.assertEqual(len(self.all_projects()), 1)

    def test_operator_routes_need_the_right_grant(self) -> None:
        token = self.offer(page_id=PAGE_A)
        for secret in (READER_SECRET, "wrong"):
            answer = self.send("POST", "/operator/offers", {"kind": "unbound_page", "page_id": PAGE_B}, secret=secret)
            self.assertIn(answer.status, {401, 403})
            self.assertEqual(self.send("GET", f"/operator/actions/{token}/card", secret=secret).status in {401, 403}, True)
        # A registry writer may offer but not read or confirm cards.
        self.assertEqual(self.send("GET", f"/operator/actions/{token}/card").status, 404)

    def test_operator_actions_are_off_until_ids_are_configured(self) -> None:
        self.app = WebhookApp(self.queue, self.config, self.clock, registry=self.registry)
        self.assertEqual(self.send("POST", "/operator/offers", {"kind": "classify", "project_id": "x"}).status, 503)
        body = {"kind": "tap", "data": "op:t:reg", "from": {"id": OPERATOR}, "chat": OPERATOR, "message_id": 1}
        headers = {"Authorization": f"Bearer {TAP_SECRET}", "Idempotency-Key": "k"}
        self.assertEqual(self.app.tailnet(Request("POST", "/tap", headers, json.dumps(body).encode())).status, 503)


class ClassificationTests(OperatorTestCase):
    def unclassified(self, code: str | None = None) -> dict[str, Any]:
        project = self.new(code, "unclassified")
        return project

    def test_in_rotation_takes_effect_and_the_card_follows_the_notion_sync(self) -> None:
        project = self.unclassified("ABC")
        token = self.offer("classify", project_id=project["id"])
        self.drain()
        self.assertIn("In rotation", [b["text"] for row in self.card()["buttons"] for b in row])
        self.assertEqual(self.tap(self.payload(1, "In rotation")).body["outcome"], "classified")
        self.assertEqual(self.read(project)["rotation"], "in")
        self.assertEqual(self.audit_actors(project["id"]) & {f"telegram:{OPERATOR}"}, {f"telegram:{OPERATOR}"})
        self.drain()
        self.assertIn("Notion update pending", self.card()["text"])

        # Notion catches up: the card is edited to say so.
        self.send("POST", f"/registry/projects/{project['id']}/sync", {"revision": 3, "ok": True}, secret=WORKER_SECRET)
        self.drain()
        self.assertIn("Notion updated", self.card()["text"])
        self.assertEqual(token, self.app._operator.card(token).token)  # type: ignore[union-attr]

    def test_a_failed_notion_sync_is_shown_and_the_change_stands(self) -> None:
        project = self.unclassified()
        self.offer("classify", project_id=project["id"])
        self.drain()
        self.tap(self.payload(1, "Out of rotation"))
        self.drain()
        revision = self.read(project)["revision"]
        self.send(
            "POST",
            f"/registry/projects/{project['id']}/sync",
            {"revision": revision, "ok": False, "detail": "boom", "attempt": 5},
            secret=WORKER_SECRET,
        )
        self.drain()
        self.assertIn("Notion update failed after retries", self.card()["text"])
        self.assertEqual(self.read(project)["rotation"], "out")

    def test_a_held_code_blocks_in_rotation_and_leaves_the_project_unclassified(self) -> None:
        self.new("ABC", "in")
        project = self.unclassified("ABC")
        self.offer("classify", project_id=project["id"])
        self.drain()
        before = self.footprint(project)
        self.assertEqual(self.tap(self.payload(1, "In rotation")).body["outcome"], "code_held")
        self.assertEqual(self.footprint(project), before)
        self.drain()
        self.assertIn("held by another Project", self.card()["text"])

    def test_a_project_changed_since_the_card_was_sent_is_stale(self) -> None:
        project = self.unclassified("ABC")
        self.offer("classify", project_id=project["id"])
        self.drain()
        self.assertEqual(self.set_code(project, "XYZ").status, 200)
        self.assertEqual(self.tap(self.payload(1, "In rotation")).body["outcome"], "stale")
        self.assertEqual(self.read(project)["rotation"], "unclassified")

    def test_only_unclassified_projects_are_offered(self) -> None:
        project = self.new("ABC", "in")
        answer = self.send("POST", "/operator/offers", {"kind": "classify", "project_id": project["id"]})
        self.assertEqual((answer.status, answer.body), (200, {"offered": False}))


class DeliveryTests(OperatorTestCase):
    def test_a_send_the_gateway_has_not_finished_is_retried_and_never_duplicated(self) -> None:
        self.offer(page_id=PAGE_A)
        self.gateway.queued = True
        worker = self.worker()
        self.assertTrue(worker.run_once())  # fails: still queued
        self.assertEqual(self.app._operator.card(next(iter(self.gateway.keys)).split(":")[1]).message_ref, None)  # type: ignore[union-attr]
        self.gateway.queued = False
        self.clock.advance(60)
        self.drain()
        self.assertEqual(self.gateway.sends, 1)
        self.assertEqual(self.tap(self.payload(1, "Skip")).body["outcome"], "skipped")

    def test_the_worker_with_a_gateway_wires_the_delivery_handler(self) -> None:
        from t6.worker.__main__ import WorkerSettings, configured_handlers

        env = {"T6_SERVER_URL": "http://x", "T6_WORKER_SECRET": "s", "T6_WORKER_QUEUE": "main"}
        settings = WorkerSettings.from_env(env)
        none = configured_handlers(env, settings, InProcess(self.app))
        self.assertEqual(none, {})
        wired = configured_handlers(
            {**env, "T6_GATEWAY_URL": "http://gw", "T6_GATEWAY_SECRET": "g"}, settings, InProcess(self.app)
        )
        self.assertIn("operator.deliver", wired["main"])

    def test_project_start_taps_still_queue_a_tap_job_with_the_projects_t6_id(self) -> None:
        project = self.new("ABC", "in", page=PAGE_A)
        body = {"kind": "tap", "data": f"approve:{PAGE_A}", "from": {"id": OPERATOR}, "chat": 1, "message_id": 2}
        headers = {"Authorization": f"Bearer {TAP_SECRET}", "Idempotency-Key": "k"}
        self.assertEqual(self.app.tailnet(Request("POST", "/tap", headers, json.dumps(body).encode())).status, 200)
        client = QueueClient(InProcess(self.app), WORKER_SECRET)
        jobs = []
        while (claim := client.claim("main")) is not None:
            jobs.append(claim)
            client.ack(claim)
        (tap,) = [job for job in jobs if job.kind == "tap"]
        self.assertEqual(tap.payload["project_id"], project["id"])
