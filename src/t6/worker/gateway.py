"""Client for the Sixto gateway's `/notify`, `/edit` and `GET /notify/{ref}`, over a `Transport`."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
from typing import Any

from t6.worker.client import Transport


class GatewayRejected(Exception):
    """The gateway refused a call."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"gateway answered {status}: {detail}")
        self.status = status


class GatewayClient:
    def __init__(self, transport: Transport, secret: str) -> None:
        self._transport = transport
        self._headers = {"Authorization": f"Bearer {secret}", "Content-Type": "application/json"}

    def _call(self, method: str, path: str, body: Mapping[str, Any] | None = None) -> dict[str, Any]:
        status, answer = self._transport.send(
            method, path, self._headers, json.dumps(body).encode() if body is not None else b""
        )
        if status not in {200, 202}:
            raise GatewayRejected(status, str(answer.get("detail", answer)))
        return answer

    def notify(self, chat: int, text: str, buttons: Sequence[Sequence[Mapping[str, str]]], key: str) -> int:
        """Queue a message with inline buttons. Returns the gateway's reference for it."""
        body = {"to": chat, "text": text, "format": "html", "buttons": buttons, "idempotency_key": key}
        return int(self._call("POST", "/notify", body)["message_ref"])

    def edit(self, message_ref: int, text: str, buttons: Sequence[Sequence[Mapping[str, str]]], key: str) -> int:
        """Replace the text and buttons of a message sent earlier. No buttons removes them."""
        body = {"message_ref": message_ref, "text": text, "format": "html", "buttons": buttons, "idempotency_key": key}
        return int(self._call("POST", "/edit", body)["message_ref"])

    def state(self, message_ref: int) -> tuple[str, int | None]:
        """`(queued|sent|failed, Telegram message id)` of a send or edit."""
        answer = self._call("GET", f"/notify/{message_ref}")
        mid = answer.get("telegram_message_id")
        return str(answer["state"]), mid if isinstance(mid, int) else None
