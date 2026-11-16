"""Operator actions: Sixto cards that let an authorized person change the registry.

Core owns every decision here. The gateway only carries a card to Telegram and a
tap back (see `docs/operator-actions.md`). A tap is accepted only from a Person
allowed by the card, from the chat and message the card was sent to, once,
and while the card is live. Each registry change carries an idempotency key
derived from the card and the choice, so a retry never changes anything twice.

`/project` opens a `manage` card: the operator picks a Project by its T6 ID and
changes its code or rotation. A code is typed as `/project NEWCODE` after tapping
Change code. A Project that cannot enter rotation because another holds its code
gets explicit choices; nothing is moved or released without one. `/project TEXT`
with no card waiting for text opens the card on the Project whose code is TEXT, or
lists the Projects whose code contains TEXT or whose T6 ID ends with it (#406).

New Project on an `unbound_page` card registers the Project and the same card then
waits for its code (stage `code`, `enter` set): `/project CODE` sets it and enters
rotation, **Keep out of rotation, no code** closes the card. The stages `code` and
`conflict` of that card use the `manage` logic (#406).

The project card also has Link Frame.io project (#272): it lists the Frame.io projects of the T6 account that no
show holds, newest first, with a typed search (`/project TEXT`), and links the chosen one to the card's show
with `frameio project link`. Any active staff Person may use it: a staff Person who is not an operator gets a
`/project` card that has only that button. The `frameio_link` card is the other way in: the main worker offers
it when review activity arrives for a Frame.io project no show holds, and a coordinator picks the show.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import html
import time
from typing import TYPE_CHECKING, Protocol

from t6.domain.events import JsonValue
from t6.media import MediaStore, active_links
from t6.operator.store import Action, ActionStore, Claim
from t6.person import STAFF_RULE, People
from t6.verbs import Done, Preview, Refused
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

if TYPE_CHECKING:  # t6.frameio imports t6.server, which imports this package, so the names are only for the checker
    from t6.frameio.model import FrameioProject
    from t6.frameio.requests import Failed

__all__ = ["Button", "Card", "OperatorService", "TapResult", "PREFIX"]

PREFIX = "op:"
"""Callback payloads are `op:<token>:<choice>`. The gateway adds `mod:t6core:`, and
Telegram allows 64 bytes in all, so 53 bytes are ours. A token is 22 characters."""

MAX_PAYLOAD = 53
MAX_CANDIDATES = 5
MAX_CODE_LENGTH = 40
MAX_QUERY_LENGTH = 40
FRAMEIO_COORDINATOR_RULE = "permission:coordinator"
"""Who may pick a show on the card that waits for a Frame.io project's link."""
_OPERATOR_ONLY = frozenset({"in", "out", "clr", "chg", "cclr", "bout"})
"""The `manage` choices that change a Project's code or rotation: only a card made for an operator takes them."""
DEFAULT_TTL_SECONDS = 7 * 24 * 3600.0


class FrameioProjectList(Protocol):
    """The Frame.io worker's read API (`t6.frameio.FrameioReader`): the projects of the configured T6 account."""

    def projects(self) -> "tuple[FrameioProject, ...] | Failed": ...


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
        people: People | None = None,
        frameio: FrameioProjectList | None = None,
        ttl: float = DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not operators:
            raise ValueError("at least one operator is needed")
        if frameio is not None and people is None:
            raise ValueError("Frame.io links are made as a Person, so they need people")
        self._frameio = frameio
        self._people = people
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

    def offer_approval(self, approval: Mapping[str, JsonValue]) -> tuple[Action, bool]:
        rule = approval['tapper_rule']
        chat = self._chat
        if isinstance(rule, str) and rule.startswith('person:') and self._people is not None:
            person = self._people.get(rule[7:])
            if person and person['active'] and person['telegram_id'] is not None:
                chat = person['telegram_id']
        return self._store.create('approval', {'approval_id': approval['id'], 'gate': approval['gate'], 'target': approval['target']},
                                  chat=chat, offer_key='approval:' + str(approval['id']), ttl=self._ttl, allowed_tapper=rule)

    def offer_frameio_link(self, frameio_project_id: str) -> tuple[Action, bool]:
        """Ask a coordinator which show a Frame.io project belongs to. One live card per Frame.io project."""
        if self._people is None:
            raise ValueError("Frame.io link cards need people")
        offer_key = f"frameio_link:{frameio_project_id}"
        live = self._store.find_offer(offer_key)
        if live is not None and live.state in {"pending", "claimed"} and live.expires_at > self._clock():
            return live, False
        found = self._frameio.projects() if self._frameio is not None else None
        name = next((p.name for p in found if p.id == frameio_project_id), None) if isinstance(found, tuple) else None
        params: dict[str, JsonValue] = {**self._listing("in", 0), "frameio_project_id": frameio_project_id, "frameio_name": name}
        return self._store.create("frameio_link", params, chat=self._chat, offer_key=offer_key, ttl=self._ttl,
                                  allowed_tapper=FRAMEIO_COORDINATOR_RULE)

    def approval_action(self, approval_id: str) -> Action | None:
        return self._store.find_offer('approval:' + approval_id)

    def recover_approvals(self) -> tuple[Action, ...]:
        if self._people is None:
            return ()
        from t6.approval import Approvals
        approvals = Approvals(self._people.registry, self._people.tokens)
        with self._people.registry._transaction() as db:
            ids = [row[0] for row in db.execute('SELECT id FROM approvals')]
        changed = []
        for approval_id in ids:
            approval = approvals.get(approval_id)
            assert approval is not None
            action = self.approval_action(approval_id)
            if approval['state'] == 'requested' and action is None:
                action, _ = self.offer_approval(approval)
                changed.append(action)
            elif approval['state'] != 'requested' and action is not None and action.state != 'done':
                changed.append(self._store.finish(action.token, {'outcome': approval['state']}))
        return tuple(changed)

    def undelivered(self) -> tuple[Action, ...]:
        return self._store.undelivered()

    def delivered(self, token: str, message_ref: int, message_id: int) -> Action | None:
        return self._store.delivered(token, message_ref, message_id)

    # -- the /project command ---------------------------------------------------

    def command(self, args: str, *, operator: int | None, chat: object, key: str) -> TapResult:
        """`/project` opens a card to pick a Project. `/project TEXT` answers a card that asked for a code or a search.

        An operator gets the full card. When Frame.io can be read, any other active staff Person who writes in
        their own chat gets the same card with only Link Frame.io project on it.
        """
        is_operator = (operator is not None and operator in self._operators and chat == operator
                       and (self._people is None or self._people.allows_telegram(operator, 'permission:operator')))
        staff = (not is_operator and self._frameio is not None and self._people is not None and operator is not None
                 and chat == operator and self._people.allows_telegram(operator, STAFF_RULE))
        if not is_operator and not staff:
            reason = "wrong_chat" if operator in self._operators else "not_allowed"
            self._store.reject(reason, operator=operator, token=None, detail="command")
            return TapResult(True, "rejected")
        assert operator is not None
        try:
            if self._store.command_seen(key):
                return TapResult(True, "already_handled")
            text = args.strip()
            waiting = self._store.waiting_for_code(operator, operator, key) if text else None
            if waiting is not None and self._people is not None and not self._people.allows_telegram(operator, waiting.allowed_tapper):
                self._store.reject('not_allowed', operator=operator, token=waiting.token, rule=waiting.allowed_tapper)
                return TapResult(True, 'rejected')
            if waiting is None:
                params, stage, project_id = self._listing("in", 0), "choose", None
                if text and not staff:
                    text = text[:MAX_QUERY_LENGTH]
                    params = self._listing("find", 0, text)
                    exact = self._with_code(text)
                    if exact is not None:
                        params, stage, project_id = {"revision": exact.revision}, "project", exact.id
                opened, _ = self._store.create("manage", params, chat=operator, offer_key=f"manage:{key}", ttl=self._ttl,
                                               allowed_tapper=STAFF_RULE if staff else "permission:operator",
                                               stage=stage, project_id=project_id)
                return TapResult(True, "opened", opened, changed=True)
            claim = self._store.claim(waiting.token, key, operator, waiting.stage)
            refused = self._refused(claim)
            if refused is not None:
                return refused
            assert claim.action is not None
            try:
                if claim.action.stage == "fio_search":
                    return self._typed_search(claim.action, text)
                return self._typed_code(claim.action, text, f"telegram:{operator}")
            except RegistryUnavailable:
                self._store.release(waiting.token)
                raise
        except RegistryUnavailable:
            return TapResult(False, "registry_unavailable")

    def _with_code(self, text: str) -> RegisteredProject | None:
        """The one Project whose code is `text` (as `normalize_code` compares), or None when none or several have it."""
        try:
            form = normalize_code(text)[1]
        except RegistryError:
            return None
        found = [p for p in self._registry.list_projects() if p.code is not None and normalize_code(p.code)[1] == form]
        return found[0] if len(found) == 1 else None

    def _listing(self, which: str, page: int, query: str | None = None) -> dict[str, JsonValue]:
        """One page of Projects to pick from: those in rotation, or the historical and unclassified ones.

        `which` "find" with a `query` lists the Projects of either rotation whose code contains it or whose T6 ID ends with it.
        """
        if query is None:
            chosen = [p for p in self._registry.list_projects() if (p.rotation is Rotation.IN) == (which == "in")]
        else:
            wanted = query.casefold()
            chosen = [p for p in self._registry.list_projects()
                      if wanted in (p.code or "").casefold() or p.id.casefold().endswith(wanted)]
        projects = sorted(chosen, key=lambda p: ((p.code or "\uffff").casefold(), p.created_at))
        start = page * MAX_CANDIDATES
        shown: list[JsonValue] = [
            {"project_id": p.id, "revision": p.revision} for p in projects[start : start + MAX_CANDIDATES]
        ]
        listing: dict[str, JsonValue] = {"list": which, "page": page, "more": len(projects) > start + MAX_CANDIDATES, "candidates": shown}
        if query is not None:
            listing["query"] = query
        return listing

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
        if self._people is None and (operator is None or operator not in self._operators):
            existing = self._store.get(token)
            self._store.reject("not_allowed", operator=operator, token=token or None,
                               rule=None if existing is None else existing.allowed_tapper)
            return TapResult(True, "rejected")
        action = self._store.get(token)
        if action is None:
            self._store.reject("unknown_token", operator=operator, token=None, detail=token[:40])
            return TapResult(True, "rejected")
        allowed = (self._people.allows_telegram(operator, action.allowed_tapper) if self._people is not None
                   else action.allowed_tapper == "permission:operator" and operator in self._operators)
        if not allowed:
            self._store.reject("not_allowed", operator=operator, token=token, rule=action.allowed_tapper)
            return TapResult(True, "rejected", action)
        if chat != action.chat:
            self._store.reject("wrong_chat", operator=operator, token=token)
            return TapResult(True, "rejected", action)
        if action.message_id is None:
            if not isinstance(message_id, int) or isinstance(message_id, bool):
                return TapResult(False, "card_not_confirmed", action)  # the gateway retries; the send is still settling
            action = self._store.confirm_message(token, message_id) or action  # the tap proves the message: the token is only on the card core sent
        if message_id != action.message_id:
            self._store.reject("wrong_message", operator=operator, token=token)
            return TapResult(True, "rejected", action)
        assert operator is not None
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
        if action.kind == 'approval':
            if choice == 'deny' and action.stage == 'choose':
                moved = self._store.advance(action.token, stage='deny_reason', params=action.params)
                return TapResult(True, 'choose_reason', moved, changed=True)
            if (choice == 'grant' and action.stage == 'choose') or (choice in {'changes', 'requirements'} and action.stage == 'deny_reason'):
                from t6.approval import Approvals
                from t6.verbs import Done, Preview
                assert self._people is not None
                with self._people.registry._transaction() as db:
                    person = db.execute('SELECT id FROM persons WHERE telegram_id = ? AND active = 1', (operator,)).fetchone()
                if person is None:
                    self._store.release(action.token)
                    return TapResult(True, 'rejected', action)
                approvals = Approvals(self._people.registry, self._people.tokens)
                verb = 'grant' if choice == 'grant' else 'deny'
                values = {'approval': str(action.params['approval_id'])}
                if verb == 'deny':
                    values['reason'] = 'Changes requested' if choice == 'changes' else 'Requirements not met'
                result = approvals.write(verb, values, actor=person[0], idempotency_key=key)
                if isinstance(result, Preview):
                    result = approvals.write(verb, values, actor=person[0], idempotency_key=key, confirm=result.token)
                if isinstance(result, Done):
                    return self._done(action, {'outcome': 'granted' if verb == 'grant' else 'denied'})
                current = approvals.get(values['approval'])
                if current and current['state'] != 'requested':
                    return self._done(action, {'outcome': current['state']})
                self._store.release(action.token)
                return TapResult(True, 'rejected', action)
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
        if action.kind == "unbound_page" and action.stage == "code" and choice == "keep":
            kept = self._registry.get(str(action.project_id))
            if kept is not None and kept.code:  # a held code was typed first: the Project keeps it, so "no code" would be wrong
                return self._done(action, {"outcome": "skipped"})
            return self._done(action, {"outcome": "registered"}, kept)
        if action.kind == "manage" or (action.kind == "unbound_page" and action.stage in {"code", "conflict"}):
            managed = self._manage(action, choice, actor, operator)
            if managed is not None:
                return managed
        if action.kind == "frameio_link":
            picked = self._frameio_link(action, choice, operator)
            if picked is not None:
                return picked
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
            asked: dict[str, JsonValue] = {"revision": made.project.revision, "enter": True}
            moved = self._store.advance(action.token, stage="code", params=asked, project_id=made.project.id)
            return TapResult(True, "registered", moved, changed=True)
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

    def _manage(self, action: Action, choice: str, actor: str, operator: int) -> TapResult | None:
        """Carry out a choice on a `manage` card. None when the card does not offer it at this stage."""
        stage, params = action.stage, action.params
        if choice in _OPERATOR_ONLY and action.allowed_tapper != "permission:operator":
            return None  # a card made for any staff Person never changes a code or a rotation
        if stage == "project" and choice == "fio" and self._frameio is not None:
            return self._list_frameio(action, "", 0)
        if stage in {"fio", "fio_search"}:
            return self._manage_frameio(action, choice, operator)
        # The version goes up with every stage, so each step of one card has its own key.
        key = f"op:{action.token}:{action.version}:{choice}"
        if stage == "choose":
            if choice in {"lin", "lhist"}:
                return self._list(action, "in" if choice == "lin" else "hist", 0)
            if choice in {"next", "prev"}:
                page = int(str(params["page"])) + (1 if choice == "next" else -1)
                return self._list(action, str(params["list"]), max(page, 0), params.get("query"))
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

    # -- Frame.io links (#272) --------------------------------------------------------

    def _list_frameio(self, action: Action, query: str, page: int) -> TapResult:
        """Show the Frame.io projects no show holds, newest first, narrowed by `query`, five to a page."""
        assert self._frameio is not None and self._people is not None
        from t6.frameio.client import SignInNeeded

        found = self._frameio.projects()
        params: dict[str, JsonValue] = {"revision": action.params["revision"], "query": query, "page": 0, "more": False, "candidates": []}
        if not isinstance(found, tuple):
            params["error"] = "sign_in" if isinstance(found, SignInNeeded) else "failed"
        else:
            held = {link.ref for link in active_links(self._people.registry, "frameio-project")}
            wanted = query.casefold()
            rows = sorted((p for p in found if p.id not in held and wanted in p.name.casefold()), key=lambda p: p.name)
            rows.sort(key=lambda p: p.updated_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
            page = min(page, max((len(rows) - 1) // MAX_CANDIDATES, 0))
            start = page * MAX_CANDIDATES
            params["page"], params["more"] = page, len(rows) > start + MAX_CANDIDATES
            params["candidates"] = [{"id": p.id, "name": p.name[:200]} for p in rows[start : start + MAX_CANDIDATES]]
        moved = self._store.advance(action.token, stage="fio", params=params)
        return TapResult(True, "frameio_list", moved, changed=True)

    def _manage_frameio(self, action: Action, choice: str, operator: int) -> TapResult | None:
        params = action.params
        if choice == "back" and action.stage == "fio":
            moved = self._store.advance(action.token, stage="project", params={"revision": params["revision"]})
            return TapResult(True, "project", moved, changed=True)
        if choice == "canc" and action.stage == "fio_search":
            return self._list_frameio(action, str(params.get("query") or ""), 0)
        if action.stage != "fio":
            return None
        if choice == "srch":
            moved = self._store.advance(action.token, stage="fio_search", params=params)
            return TapResult(True, "frameio_search", moved, changed=True)
        if choice == "clrq":
            return self._list_frameio(action, "", 0)
        if choice in {"next", "prev"}:
            page = int(str(params["page"])) + (1 if choice == "next" else -1)
            return self._list_frameio(action, str(params.get("query") or ""), max(page, 0))
        candidates = params.get("candidates")
        if choice in _PICKS and isinstance(candidates, list) and int(choice[1:]) < len(candidates):
            picked = candidates[int(choice[1:])]
            assert isinstance(picked, dict)
            key = f"op:{action.token}:{action.version}:{choice}"
            return self._link_frameio(action, str(action.project_id), str(picked["id"]), str(picked["name"]), operator, key)
        return None

    def _typed_search(self, action: Action, text: str) -> TapResult:
        """The person typed a search for the Frame.io list (`/project TEXT`)."""
        return self._list_frameio(action, text[:MAX_QUERY_LENGTH], 0)

    def _frameio_link(self, action: Action, choice: str, operator: int) -> TapResult | None:
        """A choice on the card that waits for a show for a Frame.io project."""
        params = action.params
        frameio_project = str(params["frameio_project_id"])
        if action.stage != "choose":
            return None
        if choice in {"lin", "lhist", "next", "prev"}:
            which = str(params["list"])
            if choice in {"lin", "lhist"}:
                which, page = ("in" if choice == "lin" else "hist"), 0
            else:
                page = max(int(str(params["page"])) + (1 if choice == "next" else -1), 0)
            listing = {**self._listing(which, page), "frameio_project_id": frameio_project, "frameio_name": params.get("frameio_name")}
            return TapResult(True, "choose_project", self._store.advance(action.token, stage="choose", params=listing), changed=True)
        candidates = params.get("candidates")
        if choice in _PICKS and isinstance(candidates, list) and int(choice[1:]) < len(candidates):
            picked = candidates[int(choice[1:])]
            assert isinstance(picked, dict)
            key = f"op:{action.token}:{action.version}:{choice}"
            name = params.get("frameio_name")
            return self._link_frameio(action, str(picked["project_id"]), frameio_project, name if isinstance(name, str) else frameio_project, operator, key)
        return None

    def _link_frameio(self, action: Action, project_id: str, frameio_project: str, name: str, operator: int, key: str) -> TapResult:
        """Run `frameio project link` as the tapper, who must be an active staff Person. A refusal names the holder."""
        assert self._people is not None
        with self._people.registry._transaction() as db:
            person = db.execute("SELECT id FROM persons WHERE telegram_id = ? AND active = 1", (operator,)).fetchone()
        if person is None or self._people.authorize(person[0], "staff") is not None:
            self._store.release(action.token)
            self._store.reject("not_allowed", operator=operator, token=action.token, detail="not staff")
            return TapResult(True, "rejected", action)
        if self._registry.get(project_id) is None:
            return self._done(action, {"outcome": "stale"})
        media = MediaStore(self._people.registry, self._people.tokens)
        values = {"project": project_id, "frameio_project": frameio_project}
        outcome = media.frameio_project_write("link", values, actor=person[0], idempotency_key=key)
        if isinstance(outcome, Preview):
            outcome = media.frameio_project_write("link", values, actor=person[0], idempotency_key=key, confirm=outcome.token)
        base: dict[str, JsonValue] = {"frameio_project": frameio_project, "frameio_name": name, "project_id": project_id}
        if isinstance(outcome, Done):
            return self._done(action, {"outcome": "frameio_linked", **base})
        if isinstance(outcome, Refused) and outcome.rule == "frameio_project":
            holder = next((link.project_id for link in active_links(self._people.registry, "frameio-project") if link.ref == frameio_project), None)
            return self._done(action, {"outcome": "frameio_held", **base, "holder": holder})
        reason = outcome.reason if isinstance(outcome, Refused) else "not linked"
        return self._done(action, {"outcome": "frameio_refused", **base, "reason": reason})

    def _list(self, action: Action, which: str, page: int, query: JsonValue = None) -> TapResult:
        listing = self._listing(which, page, query if isinstance(query, str) else None)
        moved = self._store.advance(action.token, stage="choose", params=listing)
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
        text, rows = _render(action, project, self._registry, self._frameio is not None)
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
    action: Action, project: RegisteredProject | None, registry: ProjectRegistry, frameio: bool = False
) -> tuple[str, list[list[tuple[str, str]]]]:
    if action.kind == "frameio_link":
        return _render_frameio_link(action, registry)
    if action.kind == 'approval':
        head = '<b>Approval: ' + html.escape(str(action.params['gate'])) + '</b>\n' + html.escape(str(action.params['target']))
        if action.state == 'done':
            return head + '\n' + html.escape(str((action.result or {}).get('outcome', 'done')).capitalize()), []
        if action.stage == 'deny_reason':
            return head + '\nChoose a reason:', [[('Changes requested', 'changes'), ('Requirements not met', 'requirements')]]
        return head + '\nGrant this Approval?', [[('Grant', 'grant'), ('Deny', 'deny')]]
    if action.kind == "manage":
        return _render_manage(action, project, registry, frameio)
    if action.kind == "unbound_page" and project is not None and (
        action.stage in {"code", "conflict"} or (action.result or {}).get("outcome") not in {"registered", "bound", None}
    ):
        return _render_manage(action, project, registry, frameio)  # registered on this card, now being given a code
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
    action: Action, project: RegisteredProject | None, registry: ProjectRegistry, frameio: bool = False
) -> tuple[str, list[list[tuple[str, str]]]]:
    params, result = action.params, action.result or {}
    if action.state == "done":
        return _render_managed(result, project, registry, action.kind == "unbound_page"), []
    if action.stage == "choose":
        in_rotation = params.get("list") == "in"
        query = params.get("query")
        if isinstance(query, str):
            lines = ["<b>Projects found</b>", f"Search: <code>{html.escape(query)}</code> (in a code, or at the end of a T6 ID)"]
        else:
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
            lines.append("No Project matches that." if isinstance(query, str) else "None on this page.")
        lines.append("Pick a Project to change its code or rotation.")
        turn = [("Previous", "prev")] if int(str(params.get("page", 0))) > 0 else []
        if params.get("more"):
            turn.append(("Next", "next"))
        if turn:
            rows.append(turn)
        both = [("Projects in rotation", "lin"), ("Historical Projects", "lhist")]
        rows.append(both if isinstance(query, str) else [("Historical Projects", "lhist") if in_rotation else ("Projects in rotation", "lin")])
        return "\n".join(lines), rows + [[("Close", "skip")]]
    head = f"<b>Project</b> {_identity(project)}"
    if action.stage in {"fio", "fio_search"}:
        return _render_frameio_list(action, head)
    if action.stage == "code":
        lines = [head]
        if action.kind == "unbound_page":
            lines.append("Registered as a new Project.")
        if "refused" in params:
            lines.append(
                f"<code>{html.escape(str(params['refused']))}</code> cannot be a code: a code needs a letter or "
                f"digit and at most {MAX_CODE_LENGTH} characters."
            )
        lines.append("Send the new code as <code>/project NEWCODE</code>.")
        if params.get("enter"):
            lines.append("The Project then enters rotation with that code.")
        keeps = action.kind == "unbound_page" and (project is None or not project.code)
        return "\n".join(lines), [[("Keep out of rotation, no code", "keep")] if keeps else [("Cancel", "skip")]]
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
    link = [[("Link Frame.io project", "fio")]] if frameio else []
    if action.allowed_tapper != "permission:operator":
        return text, link + [[("Close", "skip")]]  # a card made for any staff Person changes no code or rotation
    if project is None or project.rotation is not Rotation.IN:
        rows.append([("Put in rotation", "in")])
    if project is None or project.rotation is not Rotation.OUT:
        rows.append([("Take out of rotation", "out")])
    change = [("Change code", "chg")]
    if project is not None and project.code:
        change.append(("Clear code", "clr"))
    return text, rows + [change] + link + [[("Close", "skip")]]


def _render_managed(
    result: Mapping[str, JsonValue], project: RegisteredProject | None, registry: ProjectRegistry, registered: bool = False
) -> str:
    """The text of a used `manage` card: what the registry accepted, then where Notion stands.

    `registered` is a card that registered the Project first (New Project): its line starts by saying so.
    """
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
    lines.update(_frameio_lines(result, registry))
    line = lines.get(outcome, html.escape(outcome))
    if registered:
        line = "Registered as a new Project. " + ("It stays out of rotation." if outcome == "skipped" else line.removesuffix(" Nothing changed."))
    text = f"{head}\n<b>{line}</b>"
    if project is not None and outcome in _SHOWS_SYNC:
        text += "\n" + _SYNC_LINES[project.sync]
        if released is not None:
            text += f"\n{html.escape(_button(released))}: {_SYNC_LINES[released.sync]}"
    return text


def _frameio_lines(result: Mapping[str, JsonValue], registry: ProjectRegistry) -> dict[str, str]:
    """What a used card says about a Frame.io link: the show it went to, or the show that already holds the project."""
    name = html.escape(str(result.get("frameio_name") or result.get("frameio_project") or ""))
    shown = registry.get(str(result["project_id"])) if result.get("project_id") else None
    holder = registry.get(str(result["holder"])) if result.get("holder") else None
    return {
        "frameio_linked": f"Linked Frame.io project {name} to {_identity(shown)}. Waiting review activity is on its way.",
        "frameio_held": f"Not linked: Frame.io project {name} already belongs to {_identity(holder)}. Nothing changed.",
        "frameio_refused": f"Not linked: {html.escape(str(result.get('reason', '')))}",
    }


def _render_frameio_list(action: Action, head: str) -> tuple[str, list[list[tuple[str, str]]]]:
    """The Frame.io projects no show holds, newest first, on the card of one show."""
    params = action.params
    if action.stage == "fio_search":
        return f"{head}\nSend the search as <code>/project TEXT</code>.", [[("Cancel", "canc")]]
    lines = [head, "<b>Link Frame.io project</b>"]
    if params.get("error") == "sign_in":
        lines.append("Frame.io needs a person to sign in on the Frame.io worker. Try again after that.")
        return "\n".join(lines), [[("Back", "back")]]
    if params.get("error"):
        lines.append("Frame.io did not answer. Try again in a moment.")
        return "\n".join(lines), [[("Back", "back")]]
    query = str(params.get("query") or "")
    if query:
        lines.append(f"Search: <code>{html.escape(query)}</code>")
    rows: list[list[tuple[str, str]]] = []
    candidates = params.get("candidates")
    for index, candidate in enumerate(candidates if isinstance(candidates, list) else []):
        assert isinstance(candidate, dict)
        lines.append(f"{index + 1}. {html.escape(str(candidate['name']))}")
        rows.append([(str(candidate["name"])[:40], f"p{index}")])
    if not rows:
        lines.append("No unlinked Frame.io project matches that search." if query else "No unlinked Frame.io project in the T6 account.")
    else:
        lines.append("Newest first. Pick the one for this show.")
    turn = [("Previous", "prev")] if int(str(params.get("page", 0))) > 0 else []
    if params.get("more"):
        turn.append(("Next", "next"))
    if turn:
        rows.append(turn)
    rows.append([("Search", "srch")] + ([("Clear search", "clrq")] if query else []))
    return "\n".join(lines), rows + [[("Back", "back")]]


def _render_frameio_link(action: Action, registry: ProjectRegistry) -> tuple[str, list[list[tuple[str, str]]]]:
    """The card for review activity on a Frame.io project no show holds: a show picker."""
    params, result = action.params, action.result or {}
    name = html.escape(str(params.get("frameio_name") or params.get("frameio_project_id")))
    head = f"<b>Frame.io project needs a show</b>\nReview activity on <b>{name}</b> is waiting: no show holds this Frame.io project."
    if action.state == "done":
        outcome = str(result.get("outcome", ""))
        done_lines = {
            **_frameio_lines(result, registry),
            "skipped": "Left as it is. The activity keeps waiting and this card comes back with the next event.",
            "stale": "The show changed since this card was sent. Nothing changed.",
            "expired": "This card expired. The activity keeps waiting and this card comes back with the next event.",
        }
        return f"{head}\n<b>{done_lines.get(outcome, html.escape(outcome))}</b>", []
    in_rotation = params.get("list") == "in"
    lines = [head, "<b>Shows in rotation</b>" if in_rotation else "<b>Historical shows</b> (not in rotation)"]
    rows: list[list[tuple[str, str]]] = []
    candidates = params.get("candidates")
    for index, candidate in enumerate(candidates if isinstance(candidates, list) else []):
        assert isinstance(candidate, dict)
        found = registry.get(str(candidate["project_id"]))
        if found is None:
            continue
        lines.append(_identity(found))
        rows.append([(_button(found), f"p{index}")])
    if not rows:
        lines.append("None on this page.")
    lines.append("Which show is it for?")
    turn = [("Previous", "prev")] if int(str(params.get("page", 0))) > 0 else []
    if params.get("more"):
        turn.append(("Next", "next"))
    if turn:
        rows.append(turn)
    rows.append([("Historical shows", "lhist") if in_rotation else ("Shows in rotation", "lin")])
    return "\n".join(lines), rows + [[("Not now", "later")]]
