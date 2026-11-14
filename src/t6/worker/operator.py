"""Worker side of operator actions: send cards to Sixto and report that they arrived."""

from __future__ import annotations

from collections.abc import Mapping
import json
from typing import Any

from t6.domain.events import JobKind, JsonValue
from t6.worker.client import Transport, WorkerJob
from t6.worker.gateway import GatewayClient
from t6.worker.runner import Handler


class OperatorRejected(Exception):
    """The server refused an operator call."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"operator route answered {status}: {detail}")
        self.status = status


class OperatorClient:
    def __init__(self, transport: Transport, secret: str) -> None:
        self._transport = transport
        self._headers = {"Authorization": f"Bearer {secret}", "Content-Type": "application/json"}

    def _call(self, method: str, path: str, body: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
        status, answer = self._transport.send(
            method, path, self._headers, json.dumps(body).encode() if body is not None else b""
        )
        if status == 404:
            return None
        if status != 200:
            raise OperatorRejected(status, str(answer.get("error", answer)))
        return answer

    def card(self, token: str) -> dict[str, Any] | None:
        answer = self._call("GET", f"/operator/actions/{token}/card")
        return None if answer is None else dict(answer["card"])

    def delivered(self, token: str, message_ref: int, message_id: int) -> None:
        self._call("POST", f"/operator/actions/{token}/delivered", {"message_ref": message_ref, "message_id": message_id})

    def offer_unbound_page(self, page_id: str, label: str) -> None:
        self._call("POST", "/operator/offers", {"kind": "unbound_page", "page_id": page_id, "label": label})


class CardDelivery:
    """`operator.deliver`: put the card's current text and buttons in Telegram.

    The first delivery sends the card; later ones edit it. Both use idempotency
    keys, so running a job twice sends nothing twice. A send the gateway has not
    finished raises, and the queue retries the job.
    """

    def __init__(self, operator: OperatorClient, gateway: GatewayClient) -> None:
        self._operator = operator
        self._gateway = gateway

    def handlers(self) -> dict[str, Handler]:
        return {JobKind.OPERATOR_DELIVER.value: self.deliver}

    def deliver(self, job: WorkerJob) -> JsonValue:
        token = str(job.payload["token"])
        card = self._operator.card(token)
        if card is None:
            return {"skipped": "no such card"}
        buttons = card["buttons"]
        first = card["message_ref"] is None
        if first:
            ref = self._gateway.notify(card["chat"], card["text"], buttons, f"op-card:{token}")
        else:
            ref = self._gateway.edit(card["message_ref"], card["text"], buttons, f"op-edit:{token}:{card['version']}")
        state, message_id = self._gateway.state(ref)
        if state != "sent" or message_id is None:
            raise RuntimeError(f"gateway message {ref} is {state}")
        if first:
            self._operator.delivered(token, ref, message_id)
        return {"delivered": card["version"], "message_ref": ref}


class StateEditNotification:
    """Deliver a durable flagged-edit notice to configured operators."""

    def __init__(self, gateway: GatewayClient, operators: tuple[int, ...]) -> None:
        self.gateway = gateway
        self.operators = operators

    def deliver(self, job: WorkerJob) -> JsonValue:
        from html import escape
        if not self.operators:
            raise RuntimeError('No operator IDs configured for flagged Notion edits')
        payload = job.payload
        message = 'Notion edit needs review\n' + '\n'.join(
            f"{escape(label)}: {escape(str(payload.get(key) or ''))}"
            for key, label in (('page_id', 'Page'), ('property', 'Property'), ('value', 'Value'), ('editor', 'Editor'), ('reason', 'Reason'))
        )
        for operator in self.operators:
            ref = self.gateway.notify(operator, message, [], f'notion-state:{job.dedupe_key}:{operator}')
            state, _ = self.gateway.state(ref)
            if state != 'sent':
                raise RuntimeError(f'gateway message {ref} is {state}')
        return {'delivered': len(self.operators)}
