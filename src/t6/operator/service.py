"""Operator actions: Sixto cards that let an authorized person change the registry.

Core owns every decision here. The gateway only carries a card to Telegram and a
tap back (see `docs/operator-actions.md`). A tap is accepted only from an
allow-listed Telegram user, from the chat and message the card was sent to, once,
and while the card is live. Each registry change carries an idempotency key
derived from the card and the choice, so a retry never changes anything twice.

`/project` opens a `manage` card: the operator picks a Project by its T6 ID and
changes its code or rotation. A code is typed as `/project NEWCODE` after tapping
Change code. A Project that cannot enter rotation because another holds its code
gets explicit choices; nothing is moved or released without one.
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
    RegistryError,
    RegistryUnavailable,
    RevisionConflict,
    Rotation,
    SyncStatus,
    normalize_code,
)

__all__ = ["Button", "Card", "OperatorService", "TapResult", "PREFIX"]

PREFIX = "op:"
"""Callback payloads are `op:<token>:<choice>`. The gateway adds `mod:t6core:`, and
Telegram allows 64 bytes in all, so 53 bytes are ours. A token is 22 characters."""

MAX_PAYLOAD = 53
MAX_CANDIDATES = 5
MAX_CODE_LENGTH = 40
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

    # -- the /project command ---------------------------------------------------

    def command(self, args: str, *, operator: int | None, chat: object, key: str) -> TapResult:
        """`/project` opens a card to pick a Project. `/project NEWCODE` answers a card that asked for a code."""
        if operator is None or operator not in self._operators or chat != operator:
            reason = "wrong_chat" if operator in self._operators else "not_allowed"
            self._store.reject(reason, operator=operator, token=None, detail="command")
            return TapResult(True, "rejected")
        try:
            if self._store.command_seen(key):
                return TapResult(True, "already_handled")
            text = args.strip()
            waiting = self._store.waiting_for_code(operator, operator, key) if text else None
            if waiting is None:
                params = self._listing("in", 0)
                if text:
                    params["unasked"] = True
                opened, _ = self._store.create("manage", params, chat=operator, offer_key=f"manage:{key}", ttl=self._ttl)
                return TapResult(True, "opened", opened, changed=True)
            claim = self._store.claim(waiting.token, key, operator, "code")
            refused = self._refused(claim)
            if refused is not None:
                return refused
            assert claim.action is not None
            try:
                return self._typed_code(claim.action, text, f"telegram:{operator}")
            except RegistryUnavailable:
                self._store.release(waiting.token)
                raise
        except RegistryUnavailable:
            return TapResult(False, "registry_unavailable")

    def _listing(self, which: str, page: int) -> dict[str, JsonValue]:
        """One page of Projects to pick from: those in rotation, or the historical and unclassified ones."""
        projects = sorted(
            (p for p in self._registry.list_projects() if (p.rotation is Rotation.IN) == (which == "in")),
            key=lambda p: ((p.code or "\uffff").casefold(), p.created_at),
        )
        start = page * MAX_CANDIDATES
        shown: list[JsonValue] = [
            {"project_id": p.id, "revision": p.revision} for p in projects[start : start + MAX_CANDIDATES]
        ]
        return {"list": which, "page": page, "more": len(projects) > start + MAX_CANDIDATES, "candidates": shown}

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
        if action.kind == "manage":
            managed = self._manage(action, choice, actor)
            if managed is not None:
                return managed
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
        released = self._registry.get(str(result["released"])) if "released" in result else None
        if sync is not None and released is not None:
            sync = f"{sync}+{released.sync.value}"
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

    # -- a Project's code and rotation -------------------------------------------

    def _manage(self, action: Action, choice: str, actor: str) -> TapResult | None:
        """Carry out a choice on a `manage` card. None when the card does not offer it at this stage."""
        stage, params = action.stage, action.params
        # The version goes up with every stage, so each step of one card has its own key.
        key = f"op:{action.token}:{action.version}:{choice}"
        if stage == "choose":
            if choice in {"lin", "lhist"}:
                return self._list(action, "in" if choice == "lin" else "hist", 0)
            if choice in {"next", "prev"}:
                page = int(str(params["page"])) + (1 if choice == "next" else -1)
                return self._list(action, str(params["list"]), max(page, 0))
            if choice in _PICKS:
                return self._open(action, int(choice[1:]))
            return None
        revision = int(str(params["revision"]))
        project_id = str(action.project_id)
        if stage == "project" and choice in {"in", "out"}:
            if choice == "in":
                return self._enter(action, revision, actor, key)
            left = self._registry.set_rotation(
                project_id, Rotation.OUT, actor=actor, idempotency_key=key, expected_revision=revision
            )
            if isinstance(left, Changed):
                return self._done(action, {"outcome": "rotation_out"}, left.project)
            return self._done(action, {"outcome": "stale"})
        if stage == "project" and choice == "clr":
            return self._clear(action, revision, actor, key, then_enter=False)
        if stage == "conflict" and choice == "cclr":
            return self._clear(action, revision, actor, key, then_enter=True)
        if stage in {"project", "conflict"} and choice == "chg":
            asked: dict[str, JsonValue] = {"revision": revision, "enter": stage == "conflict"}
            return TapResult(True, "enter_code", self._store.advance(action.token, stage="code", params=asked), changed=True)
        holders = params.get("holders")
        if stage == "conflict" and choice == "bout" and isinstance(holders, list) and len(holders) == 1:
            holder = holders[0]
            assert isinstance(holder, dict)
            freed = self._registry.set_rotation(
                str(holder["project_id"]),
                Rotation.OUT,
                actor=actor,
                idempotency_key=f"{key}:holder",
                expected_revision=int(str(holder["revision"])),
            )
            if not isinstance(freed, Changed):
                return self._done(action, {"outcome": "stale"})
            return self._enter(action, revision, actor, key, {"released": freed.project.id})
        return None

    def _list(self, action: Action, which: str, page: int) -> TapResult:
        moved = self._store.advance(action.token, stage="choose", params=self._listing(which, page))
        return TapResult(True, "choose_project", moved, changed=True)

    def _open(self, action: Action, index: int) -> TapResult:
        candidates = action.params.get("candidates")
        picked = candidates[index] if isinstance(candidates, list) and index < len(candidates) else None
        project = self._registry.get(str(picked["project_id"])) if isinstance(picked, dict) else None
        if project is None:
            return self._done(action, {"outcome": "stale"})
        moved = self._store.advance(
            action.token, stage="project", params={"revision": project.revision}, project_id=project.id
        )
        return TapResult(True, "project", moved, changed=True)

    def _enter(
        self, action: Action, revision: int, actor: str, key: str, also: Mapping[str, JsonValue] | None = None
    ) -> TapResult:
        """Put the card's Project in rotation. A held code changes nothing and asks the operator how to resolve it."""
        outcome = self._registry.set_rotation(
            str(action.project_id), Rotation.IN, actor=actor, idempotency_key=f"{key}:in", expected_revision=revision
        )
        if isinstance(outcome, Changed):
            return self._done(action, {"outcome": "rotation_in", **(also or {})}, outcome.project)
        if isinstance(outcome, CodeHeld):
            holders: list[JsonValue] = []
            for holder_id in outcome.held_by:
                holder = self._registry.get(holder_id)
                if holder is not None:
                    holders.append({"project_id": holder.id, "revision": holder.revision})
            conflict: dict[str, JsonValue] = {
                "revision": revision,
                "code": outcome.code,
                "reason": outcome.reason,
                "holders": holders,
            }
            moved = self._store.advance(action.token, stage="conflict", params=conflict)
            return TapResult(True, "conflict", moved, changed=True)
        return self._done(action, {"outcome": "stale"})

    def _clear(self, action: Action, revision: int, actor: str, key: str, *, then_enter: bool) -> TapResult:
        before = self._registry.get(str(action.project_id))
        outcome = self._registry.set_code(
            str(action.project_id), None, actor=actor, idempotency_key=key, expected_revision=revision
        )
        if not isinstance(outcome, Changed):
            return self._done(action, {"outcome": "stale"})
        cleared = None if before is None else before.code
        if then_enter:
            return self._enter(action, outcome.project.revision, actor, key, {"cleared": cleared})
        return self._done(action, {"outcome": "code_cleared", "cleared": cleared}, outcome.project)

    def _typed_code(self, action: Action, text: str, actor: str) -> TapResult:
        """The operator typed a code for the card's Project (`/project NEWCODE`)."""
        key = f"op:{action.token}:{action.version}:code"
        try:
            usable = len(text) <= MAX_CODE_LENGTH and normalize_code(text)[0] is not None
        except RegistryError:
            usable = False
        if not usable:
            again = self._store.advance(action.token, stage="code", params={**action.params, "refused": text[:MAX_CODE_LENGTH]})
            return TapResult(True, "bad_code", again, changed=True)
        outcome = self._registry.set_code(
            str(action.project_id),
            text,
            actor=actor,
            idempotency_key=key,
            expected_revision=int(str(action.params["revision"])),
        )
        if isinstance(outcome, CodeHeld):
            return self._done(action, {"outcome": "code_held", "code": outcome.code})
        if not isinstance(outcome, Changed):
            return self._done(action, {"outcome": "stale"})
        if action.params.get("enter"):
            return self._enter(action, outcome.project.revision, actor, key, {"code": outcome.project.code})
        return self._done(action, {"outcome": "code_set", "code": outcome.project.code}, outcome.project)

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
            result = action.result or {}
            if result.get("outcome") not in _SHOWS_SYNC or action.message_ref is None:
                continue
            ids = [action.project_id, result.get("released")]
            found = [self._registry.get(i) for i in ids if isinstance(i, str)]
            sync = "+".join(p.sync.value for p in found if p is not None)
            if action.shown_sync != sync:
                moved.append(self._store.show_sync(action.token, sync))
        return tuple(moved)


_PICKS = frozenset(f"p{i}" for i in range(MAX_CANDIDATES))
_SHOWS_SYNC = frozenset({"registered", "bound", "classified", "rotation_in", "rotation_out", "code_set", "code_cleared"})
_SYNC_LINES = {
    SyncStatus.PENDING: "Notion update pending.",
    SyncStatus.SYNCED: "Notion updated.",
    SyncStatus.FAILED: "Notion update failed after retries. The change stands in T6.",
}
_ROTATION_WORDS = {Rotation.IN: "in rotation", Rotation.OUT: "out of rotation", Rotation.UNCLASSIFIED: "unclassified"}


def _needs_page(project: RegisteredProject) -> bool:
    active = [b for b in project.bindings if b.active and b.system == NOTION]
    return not active or not any(b.available for b in active)


def _name(project: RegisteredProject) -> str:
    return html.escape(project.code or project.id)


def _render(
    action: Action, project: RegisteredProject | None, registry: ProjectRegistry
) -> tuple[str, list[list[tuple[str, str]]]]:
    if action.kind == "manage":
        return _render_manage(action, project, registry)
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
        text += "\n" + _SYNC_LINES[project.sync]
    return text, []


def _identity(project: RegisteredProject | None) -> str:
    """A Project as a card names it: its code, if any, and always its T6 ID."""
    if project is None:
        return "(unknown Project)"
    code = f"<b>{html.escape(project.code)}</b>" if project.code else "no code"
    return f"{code} <code>{project.id}</code>"


def _button(project: RegisteredProject) -> str:
    return f"{project.code or 'no code'} · {project.id[-6:]}"


def _render_manage(
    action: Action, project: RegisteredProject | None, registry: ProjectRegistry
) -> tuple[str, list[list[tuple[str, str]]]]:
    params, result = action.params, action.result or {}
    if action.state == "done":
        return _render_managed(result, project, registry), []
    if action.stage == "choose":
        in_rotation = params.get("list") == "in"
        lines = ["<b>Projects in rotation</b>" if in_rotation else "<b>Historical Projects</b> (not in rotation)"]
        rows: list[list[tuple[str, str]]] = []
        candidates = params.get("candidates")
        for index, candidate in enumerate(candidates if isinstance(candidates, list) else []):
            assert isinstance(candidate, dict)
            found = registry.get(str(candidate["project_id"]))
            if found is None:
                continue
            unclassified = ", unclassified" if found.rotation is Rotation.UNCLASSIFIED else ""
            lines.append(f"{_identity(found)}{unclassified}")
            rows.append([(_button(found), f"p{index}")])
        if not rows:
            lines.append("None on this page.")
        if params.get("unasked"):
            lines.append("No Project is waiting for a code. Pick one, then tap Change code.")
        lines.append("Pick a Project to change its code or rotation.")
        turn = [("Previous", "prev")] if int(str(params.get("page", 0))) > 0 else []
        if params.get("more"):
            turn.append(("Next", "next"))
        if turn:
            rows.append(turn)
        rows.append([("Historical Projects", "lhist") if in_rotation else ("Projects in rotation", "lin")])
        return "\n".join(lines), rows + [[("Close", "skip")]]
    head = f"<b>Project</b> {_identity(project)}"
    if action.stage == "code":
        lines = [head]
        if "refused" in params:
            lines.append(
                f"<code>{html.escape(str(params['refused']))}</code> cannot be a code: a code needs a letter or "
                f"digit and at most {MAX_CODE_LENGTH} characters."
            )
        lines.append("Send the new code as <code>/project NEWCODE</code>.")
        if params.get("enter"):
            lines.append("The Project then enters rotation with that code.")
        return "\n".join(lines), [[("Cancel", "skip")]]
    if action.stage == "conflict":
        holders = params.get("holders")
        found_holders = [
            registry.get(str(h["project_id"])) for h in (holders if isinstance(holders, list) else []) if isinstance(h, dict)
        ]
        named = ", ".join(_identity(h) for h in found_holders if h is not None)
        why = "unclassified, so its code is blocked" if params.get("reason") == "unclassified" else "in rotation"
        text = (
            f"{head}\n<b>Cannot enter rotation.</b> Code {html.escape(str(params.get('code')))} is held by "
            f"{named} ({why}).\nNothing changed. How should this be resolved?"
        )
        rows = [[("Clear this Project's code and enter", "cclr")], [("Change this Project's code", "chg")]]
        if len(found_holders) == 1 and found_holders[0] is not None:
            rows.append([(f"Take {_button(found_holders[0])} out of rotation", "bout")])
        return text, rows + [[("Cancel", "skip")]]
    rotation = "unknown" if project is None else _ROTATION_WORDS[project.rotation]
    text = f"{head}\nRotation: {rotation}"
    if project is not None:
        text += "\n" + _SYNC_LINES[project.sync]
    rows = []
    if project is None or project.rotation is not Rotation.IN:
        rows.append([("Put in rotation", "in")])
    if project is None or project.rotation is not Rotation.OUT:
        rows.append([("Take out of rotation", "out")])
    change = [("Change code", "chg")]
    if project is not None and project.code:
        change.append(("Clear code", "clr"))
    return text, rows + [change, [("Close", "skip")]]


def _render_managed(
    result: Mapping[str, JsonValue], project: RegisteredProject | None, registry: ProjectRegistry
) -> str:
    """The text of a used `manage` card: what the registry accepted, then where Notion stands."""
    outcome = str(result.get("outcome", ""))
    head = "<b>Projects</b>" if project is None else f"<b>Project</b> {_identity(project)}"
    released = registry.get(str(result["released"])) if "released" in result else None
    entered = "Now in rotation."
    if result.get("cleared"):
        entered += f" Its code {html.escape(str(result['cleared']))} was cleared."
    if result.get("code"):
        entered += f" Its code is now {html.escape(str(result['code']))}."
    if released is not None:
        entered += f"\n{_identity(released)} was taken out of rotation. It keeps its code on record."
    lines = {
        "rotation_in": entered,
        "rotation_out": "Now out of rotation. Its code stays on record and is free for another Project.",
        "code_set": f"Code set to {html.escape(str(result.get('code')))}.",
        "code_cleared": f"Code {html.escape(str(result.get('cleared')))} cleared.",
        "code_held": f"Not changed: code {html.escape(str(result.get('code')))} is held by another Project.",
        "skipped": "Closed. Nothing changed.",
        "stale": "The Project changed since this card was sent. Nothing changed.",
        "expired": "This card expired. Nothing changed.",
    }
    text = f"{head}\n<b>{lines.get(outcome, html.escape(outcome))}</b>"
    if project is not None and outcome in _SHOWS_SYNC:
        text += "\n" + _SYNC_LINES[project.sync]
        if released is not None:
            text += f"\n{html.escape(_button(released))}: {_SYNC_LINES[released.sync]}"
    return text
