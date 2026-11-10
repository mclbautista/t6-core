"""Operator actions: Sixto cards that let an authorized person change the registry.

Core owns every decision here. The gateway only carries a card to Telegram and a
tap back (see `docs/operator-actions.md`). A tap is accepted only from an
allow-listed Telegram user, from the chat and message the card was sent to, once,
and while the card is live. Each registry change carries an idempotency key
derived from the card and the choice, so a retry never changes anything twice.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import html
import time

from t6.domain.events import JsonValue
from t6.operator.store import Action, ActionStore, Claim
from t6.registry import (
    NOTION,
    Changed,
    CodeHeld,
    NotFound,
    PageAlreadyBound,
    ProjectRegistry,
    Registered,
    RegisteredProject,
    RegistryUnavailable,
    RevisionConflict,
    Rotation,
    SyncStatus,
)

__all__ = ["Button", "Card", "OperatorService", "TapResult", "PREFIX"]

PREFIX = "op:"
"""Callback payloads are `op:<token>:<choice>`. The gateway adds `mod:t6core:`, and
Telegram allows 64 bytes in all, so 53 bytes are ours. A token is 22 characters."""

MAX_PAYLOAD = 53
MAX_CANDIDATES = 5
DEFAULT_TTL_SECONDS = 7 * 24 * 3600.0


@dataclass(frozen=True, slots=True)
class Button:
    text: str
    payload: str


@dataclass(frozen=True, slots=True)
class Card:
    token: str
    version: int
    chat: int
    message_ref: int | None
    message_id: int | None
    text: str
    """HTML for Telegram. Every value from a Notion page is escaped."""
    buttons: tuple[tuple[Button, ...], ...]


@dataclass(frozen=True, slots=True)
class TapResult:
    accepted: bool
    """False asks the gateway to deliver the tap again later (nothing was decided)."""
    outcome: str
    action: Action | None = None
    changed: bool = False
    """True when the card has to be shown again."""


class OperatorService:
    def __init__(
        self,
        registry: ProjectRegistry,
        store: ActionStore,
        operators: Sequence[int],
        *,
        ttl: float = DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not operators:
            raise ValueError("at least one operator is needed")
        self._registry = registry
        self._store = store
        self._operators = frozenset(operators)
        self._chat = operators[0]
        self._ttl = ttl
        self._clock = clock

    @property
    def store(self) -> ActionStore:
        return self._store

    # -- offers ----------------------------------------------------------------

    def offer_unbound_page(self, page_id: str, label: str) -> tuple[Action, bool]:
        """Ask the operator what a Projects page that no Project owns should become."""
        return self._store.create(
            "unbound_page",
            {"page_id": page_id, "label": label},
            chat=self._chat,
            offer_key=f"unbound:{page_id}",
            ttl=self._ttl,
        )

    def offer_classification(self, project_id: str) -> tuple[Action, bool] | None:
        """Ask whether an unclassified Project is in rotation. None when it needs no answer."""
        project = self._registry.get(project_id)
        if project is None or project.rotation is not Rotation.UNCLASSIFIED:
            return None
        return self._store.create(
            "classify",
            {"revision": project.revision},
            chat=self._chat,
            offer_key=f"classify:{project_id}:{project.revision}",
            ttl=self._ttl,
            project_id=project_id,
        )

    def undelivered(self) -> tuple[Action, ...]:
        return self._store.undelivered()

    def delivered(self, token: str, message_ref: int, message_id: int) -> Action | None:
        return self._store.delivered(token, message_ref, message_id)

    # -- taps ------------------------------------------------------------------

    def tap(self, data: str, *, operator: int | None, chat: object, message_id: object, tap_key: str) -> TapResult:
        token, _, choice = data[len(PREFIX) :].partition(":")
        try:
            return self._tap(token, choice, operator, chat, message_id, tap_key)
        except RegistryUnavailable:
            self._store.release(token)
            return TapResult(False, "registry_unavailable")

    def _tap(
        self, token: str, choice: str, operator: int | None, chat: object, message_id: object, tap_key: str
    ) -> TapResult:
        if operator is None or operator not in self._operators:
            self._store.reject("not_allowed", operator=operator, token=token or None)
            return TapResult(True, "rejected")
        action = self._store.get(token)
        if action is None:
            self._store.reject("unknown_token", operator=operator, token=None, detail=token[:40])
            return TapResult(True, "rejected")
        if chat != action.chat:
            self._store.reject("wrong_chat", operator=operator, token=token)
            return TapResult(True, "rejected", action)
        if action.message_id is None:
            return TapResult(False, "card_not_confirmed", action)  # the gateway retries; the send is still settling
        if message_id != action.message_id:
            self._store.reject("wrong_message", operator=operator, token=token)
            return TapResult(True, "rejected", action)
        claim = self._store.claim(token, tap_key, operator, action.stage)
        refused = self._refused(claim)
        if refused is not None:
            return refused
        assert claim.action is not None
        return self._carry_out(claim.action, choice, operator)

    def _refused(self, claim: Claim) -> TapResult | None:
        if claim.kind in {"ok", "replay"}:
            return None
        if claim.kind == "expired" and claim.action is not None:
            done = self._store.finish(claim.action.token, {"outcome": "expired"})
            return TapResult(True, "expired", done, changed=True)
        return TapResult(True, {"handled": "already_handled", "busy": "busy"}.get(claim.kind, claim.kind), claim.action)

    def _carry_out(self, action: Action, choice: str, operator: int) -> TapResult:
        actor = f"telegram:{operator}"
        key = f"op:{action.token}:{choice}"
        if choice == "skip" or choice == "later":
            return self._done(action, {"outcome": "skipped"})
        if action.kind == "unbound_page" and action.stage == "choose" and choice == "reg":
            return self._register(action, actor, key)
        if action.kind == "unbound_page" and action.stage == "choose" and choice == "bind":
            return self._offer_candidates(action)
        if action.kind == "unbound_page" and action.stage == "pick" and choice in _PICKS:
            return self._bind(action, int(choice[1:]), actor, key)
        if action.kind == "classify" and choice in {"in", "out"}:
            return self._classify(action, Rotation(choice), actor, key)
        self._store.release(action.token)
        self._store.reject("unknown_choice", operator=operator, token=action.token, detail=choice[:20])
        return TapResult(True, "rejected", action)

    def _done(
        self,
        action: Action,
        result: Mapping[str, JsonValue],
        project: RegisteredProject | None = None,
    ) -> TapResult:
        sync = None if project is None else project.sync.value
        done = self._store.finish(action.token, result, project_id=None if project is None else project.id, sync=sync)
        return TapResult(True, str(result["outcome"]), done, changed=True)

    def _register(self, action: Action, actor: str, key: str) -> TapResult:
        made = self._registry.register(
            actor=actor, idempotency_key=key, notion_page_id=str(action.params["page_id"]), rotation=Rotation.OUT
        )
        if isinstance(made, Registered):
            return self._done(action, {"outcome": "registered"}, made.project)
        if isinstance(made, PageAlreadyBound):
            return self._done(action, {"outcome": "page_taken", "project_id": made.project_id})
        return self._done(action, {"outcome": "stale"})

    def _offer_candidates(self, action: Action) -> TapResult:
        waiting = [project for project in self._registry.list_projects() if _needs_page(project)]
        candidates: list[JsonValue] = [{"project_id": p.id, "revision": p.revision} for p in waiting[:MAX_CANDIDATES]]
        if not candidates:
            return self._done(action, {"outcome": "no_candidates"})
        moved = self._store.advance(action.token, stage="pick", params={**action.params, "candidates": candidates})
        return TapResult(True, "choose_project", moved, changed=True)

    def _bind(self, action: Action, index: int, actor: str, key: str) -> TapResult:
        candidates = action.params.get("candidates")
        if not isinstance(candidates, list) or index >= len(candidates):
            return self._done(action, {"outcome": "stale"})
        picked = candidates[index]
        assert isinstance(picked, dict)
        outcome = self._registry.bind_page(
            str(picked["project_id"]),
            str(action.params["page_id"]),
            actor=actor,
            idempotency_key=key,
            expected_revision=int(str(picked["revision"])),
        )
        if isinstance(outcome, Changed):
            return self._done(action, {"outcome": "bound"}, outcome.project)
        if isinstance(outcome, PageAlreadyBound):
            return self._done(action, {"outcome": "page_taken", "project_id": outcome.project_id})
        return self._done(action, {"outcome": "stale"})

    def _classify(self, action: Action, rotation: Rotation, actor: str, key: str) -> TapResult:
        outcome = self._registry.set_rotation(
            str(action.project_id),
            rotation,
            actor=actor,
            idempotency_key=key,
            expected_revision=int(str(action.params["revision"])),
        )
        if isinstance(outcome, Changed):
            return self._done(action, {"outcome": "classified", "rotation": rotation.value}, outcome.project)
        if isinstance(outcome, CodeHeld):
            return self._done(action, {"outcome": "code_held", "code": outcome.code})
        if isinstance(outcome, (RevisionConflict, NotFound)):
            return self._done(action, {"outcome": "stale"})
        return self._done(action, {"outcome": "stale"})

    # -- the card --------------------------------------------------------------

    def card(self, token: str) -> Card | None:
        action = self._store.get(token)
        if action is None:
            return None
        project = None if action.project_id is None else self._registry.get(action.project_id)
        text, rows = _render(action, project, self._registry)
        buttons = tuple(
            tuple(Button(label, f"{PREFIX}{token}:{choice}") for label, choice in row) for row in rows
        )
        for row in buttons:
            for button in row:
                assert len(button.payload.encode()) <= MAX_PAYLOAD, button.payload
        return Card(token, action.version, action.chat, action.message_ref, action.message_id, text, buttons)

    def sync_changed(self, project_id: str) -> tuple[Action, ...]:
        """Cards that promised a Notion sync status that is now different, marked to be shown again."""
        project = self._registry.get(project_id)
        if project is None:
            return ()
        moved: list[Action] = []
        for action in self._store.done_for_project(project_id):
            if action.shown_sync != project.sync.value and action.message_ref is not None:
                moved.append(self._store.show_sync(action.token, project.sync.value))
        return tuple(moved)


_PICKS = frozenset(f"p{i}" for i in range(MAX_CANDIDATES))


def _needs_page(project: RegisteredProject) -> bool:
    active = [b for b in project.bindings if b.active and b.system == NOTION]
    return not active or not any(b.available for b in active)


def _name(project: RegisteredProject) -> str:
    return html.escape(project.code or project.id)


def _render(
    action: Action, project: RegisteredProject | None, registry: ProjectRegistry
) -> tuple[str, list[list[tuple[str, str]]]]:
    label = html.escape(str(action.params.get("label") or "(untitled)"))
    result = action.result or {}
    outcome = str(result.get("outcome", ""))
    if action.kind == "unbound_page":
        head = f"<b>New Projects page</b>\n{label}\nNo Project owns this page."
    else:
        shown = None if project is None else _name(project)
        head = f"<b>Rotation needed</b>\nProject {shown} has not been classified."
    if action.state != "done":
        if action.kind == "classify":
            return head + "\nIs it in rotation?", [[("In rotation", "in"), ("Out of rotation", "out")], [("Later", "later")]]
        if action.stage == "pick":
            rows: list[list[tuple[str, str]]] = []
            candidates = action.params.get("candidates")
            for index, candidate in enumerate(candidates if isinstance(candidates, list) else []):
                assert isinstance(candidate, dict)
                found = registry.get(str(candidate["project_id"]))
                rows.append([(("Bind to " + (found.code or found.id[-6:])) if found else "Bind", f"p{index}")])
            return head + "\nWhich Project is it the page for?", rows + [[("Skip", "skip")]]
        return head + "\nWhat should it be?", [[("New Project", "reg"), ("Existing Project", "bind")], [("Skip", "skip")]]
    lines = {
        "registered": "Registered as a new Project (out of rotation, no code).",
        "bound": "Bound as the Project's page. The old page stays as a legacy page.",
        "classified": f"Classified as {'in' if result.get('rotation') == 'in' else 'out of'} rotation.",
        "skipped": "Left as it is.",
        "no_candidates": "No Project is waiting for a page. Nothing changed.",
        "page_taken": "Another Project already owns this page. Nothing changed.",
        "code_held": f"Not changed: code {html.escape(str(result.get('code')))} is held by another Project.",
        "stale": "The Project changed since this card was sent. Nothing changed.",
        "expired": "This card expired. Nothing changed.",
    }
    text = f"{head}\n<b>{lines.get(outcome, html.escape(outcome))}</b>"
    if project is not None and outcome in {"registered", "bound", "classified"}:
        text += "\n" + {
            SyncStatus.PENDING: "Notion update pending.",
            SyncStatus.SYNCED: "Notion updated.",
            SyncStatus.FAILED: "Notion update failed after retries. The change stands in T6.",
        }[project.sync]
    return text, []
