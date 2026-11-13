"""The lookup pipelines. No I/O: everything works over the `Index` the caller supplies."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Generic, Literal, Protocol, TypeVar

from t6.domain import Contact, Deliverable, JobOrder, Machine, NotionEntity, Project, Studio
from t6.lookup.normalize import Normalizers
from t6.lookup.result import Candidate, Method, Resolution
from t6.lookup.scoring import wratio

E = TypeVar("E", bound=NotionEntity)

ALIASES_PROPERTY = "Aliases"


class Judge(Protocol):
    """The optional LLM strategy: how likely each candidate is the mention, 0 to 1.

    Called with the mention and the best fuzzy candidates only, after cheaper
    strategies failed. Return probabilities keyed by page ID, or `None` for no
    answer (a failed call, no LLM configured). The app owns the prompt and the
    masking (see `t6.llm`); this seam never sees a provider.
    """

    def __call__(self, mention: str, candidates: Sequence[Any]) -> Mapping[str, float] | None: ...


def similarity(a: str, b: str) -> float:
    """Default fuzzy score, 0 to 1, on normalized text. Stdlib only.

    The best of plain similarity and word-order-insensitive similarity; a
    complete subset of words (`santos` in `maria santos`) scores 0.9.
    """
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    wa, wb = a.split(), b.split()
    score = max(
        SequenceMatcher(None, a, b).ratio(),
        SequenceMatcher(None, " ".join(sorted(wa)), " ".join(sorted(wb))).ratio(),
    )
    if set(wa) <= set(wb) or set(wb) <= set(wa):
        score = max(score, 0.9)
    return score


@dataclass(frozen=True, slots=True)
class Policy:
    """Which strategies run for an entity type, and their thresholds.

    A fuzzy match decides only at `fuzzy_accept` or more and `margin` ahead of
    the runner-up. Candidates under `review_floor` are not offered. An LLM
    answer decides only at `llm_accept` or more and `margin` ahead.
    """

    fuzzy: bool = True
    llm: bool = True
    fuzzy_accept: float = 0.92
    review_floor: float = 0.6
    llm_accept: float = 0.8
    margin: float = 0.05
    max_candidates: int = 5
    scorer: Callable[[str, str], float] = similarity


EXACT_ONLY = Policy(fuzzy=False, llm=False)

DEFAULT_POLICIES: dict[str, Policy] = {
    "job_order": EXACT_ONLY,
    "project": EXACT_ONLY,
    "project_name": Policy(llm=False),
    "machine": EXACT_ONLY,
    "deliverable": Policy(),
    "contact": Policy(fuzzy_accept=0.90, margin=0.08, review_floor=0.60, max_candidates=3, scorer=wratio),
    "studio": Policy(),
}


@dataclass(frozen=True, slots=True)
class Index:
    """The entities lookups run over. The caller loads and refreshes them."""

    contacts: tuple[Contact, ...] = ()
    projects: tuple[Project, ...] = ()
    deliverables: tuple[Deliverable, ...] = ()
    job_orders: tuple[JobOrder, ...] = ()
    machines: tuple[Machine, ...] = ()
    studios: tuple[Studio, ...] = ()


@dataclass(frozen=True, slots=True)
class AliasWrite:
    """A proposed write of one alias to a Notion `Aliases` property. Nothing has been written."""

    database: Literal["Contacts", "Studios"]
    page_id: str
    alias: str
    property: str = ALIASES_PROPERTY


@dataclass(frozen=True, slots=True)
class _Kind(Generic[E]):
    """How one entity type is matched: its exact IDs and its normalized forms."""

    ids: Callable[[E], Iterable[str | None]]
    forms: Callable[[E], Iterable[str | None]]
    mention: Callable[[str], Iterable[str]]
    stages: tuple[tuple[Callable[[E], Iterable[str | None]], Callable[[str], Iterable[str]]], ...] = ()
    """Normalized matching in this order (forms of an entity, forms of the mention), first stage with a match
    decides. Empty means one stage of `forms` and `mention`."""
    fuzzy_forms: Callable[[E], Iterable[str | None]] | None = None
    fuzzy_mention: Callable[[str], Iterable[str]] | None = None
    """What fuzzy matching compares, when it is narrower than `forms` and `mention`."""


def _clean(values: Iterable[str | None]) -> tuple[str, ...]:
    return tuple(v for v in values if v)


class Lookup:
    """Resolve mentions to entities in `index`. One method per entity type."""

    def __init__(
        self,
        index: Index,
        *,
        normalizers: Normalizers = Normalizers(),
        policies: Mapping[str, Policy] | None = None,
        judge: Judge | None = None,
        same_project: Callable[[str | None, str | None], bool] | None = None,
    ) -> None:
        self._same_project = same_project or (lambda a, b: a == b)
        self._index = index
        self._n = normalizers
        self._policies = {**DEFAULT_POLICIES, **(policies or {})}
        self._judge = judge
        n = normalizers
        self._job_order: _Kind[JobOrder] = _Kind(
            ids=lambda e: [e.telegram_key], forms=lambda e: [e.telegram_key and n.code(e.telegram_key)], mention=lambda m: [n.code(m)]
        )
        self._project: _Kind[Project] = _Kind(
            ids=lambda e: [e.project_code], forms=lambda e: [e.project_code and n.code(e.project_code)], mention=lambda m: [n.code(m)]
        )
        self._project_name: _Kind[Project] = _Kind(ids=lambda e: [], forms=lambda e: [n.name(e.name)], mention=lambda m: [n.name(m)])
        self._machine: _Kind[Machine] = _Kind(
            ids=lambda e: [e.serial_number, e.hostname],
            forms=lambda e: [e.serial_number and n.host(e.serial_number), e.hostname and n.host(e.hostname)],
            mention=lambda m: [n.host(m)],
        )
        contact_names: Callable[[Contact], list[str | None]] = lambda e: [n.name(e.name), *(n.name(a) for a in e.aliases)]
        self._contact: _Kind[Contact] = _Kind(
            ids=lambda e: [None if e.telegram_user_id is None else str(e.telegram_user_id)],
            forms=lambda e: [*contact_names(e), e.telegram_handle and n.handle(e.telegram_handle)],
            mention=lambda m: [n.name(m), n.handle(m)],
            # the studio policy: Telegram ID, then handle, then alias or name, then fuzzy on names
            stages=(
                (lambda e: [e.telegram_handle and n.handle(e.telegram_handle)], lambda m: [n.handle(m)]),
                (contact_names, lambda m: [n.name(m)]),
            ),
            fuzzy_forms=contact_names,
            fuzzy_mention=lambda m: [n.name(m)],
        )
        self._deliverable: _Kind[Deliverable] = _Kind(ids=lambda e: [], forms=lambda e: [n.name(e.name)], mention=lambda m: [n.name(m)])
        self._studio: _Kind[Studio] = _Kind(
            ids=lambda e: [], forms=lambda e: [n.name(e.name), *(n.name(a) for a in e.aliases)], mention=lambda m: [n.name(m)]
        )

    # One method per entity type

    def job_order(self, telegram_key: str) -> Resolution[JobOrder]:
        """The Job Order a Telegram Key belongs to, or none."""
        return self._run("job_order", self._job_order, telegram_key, self._index.job_orders)

    def project(self, code: str | None, *, name: str | None = None) -> Resolution[Project]:
        """A Project by Project Code.

        Codes are reused, so a code can belong to several Projects over time.
        When the Projects carry their registry rotation (`ProjectRefs.identified`):

        - exactly one Project in rotation holds the code: it is the answer,
          however many historical Projects share the code;
        - none holds it: the result needs review with every Project that has
          the code as candidates, so old work is never picked silently;
        - several hold it, or any Project with the code is unclassified: the
          result needs review and `reason` says why. Nothing is chosen.

        Projects with no known rotation (not registered yet) all count as
        holders, so a code they share needs review, as before.

        A blank code never decides: the result needs review, ranked by `name`
        when one is given, otherwise listing the Projects that have no code.
        To select one Project among those that share a code, use `project_by_id`.
        """
        if code and code.strip():
            return self._project_by_code(code)
        if name and name.strip():
            found = self._run("project_name", self._project_name, name, self._index.projects)
            ranked = found.candidates or (
                (Candidate(found.entity, found.score),) if found.entity else ()
            )
            return Resolution(None, "review", ranked[0].score if ranked else 0.0, ranked, True)
        uncoded = tuple(Candidate(p, 0.0) for p in self._index.projects if not p.project_code)
        return Resolution(None, "review", 0.0, uncoded, True)

    def project_by_id(self, project_id: str) -> Resolution[Project]:
        """The Project a T6 Project ID, or any page it was bound to, names.

        This is the explicit selection: it does not look at codes or rotation,
        so it finds a historical Project whose code another Project holds now.
        """
        wanted = project_id.strip()
        if not wanted:
            return _NONE
        found = tuple(
            p for p in self._index.projects if p.t6_id == wanted or self._same_project(p.page_id, wanted)
        )
        if not found:
            return _NONE
        if len(found) > 1:
            return Resolution(None, "review", 1.0, tuple(Candidate(p, 1.0) for p in found), True)
        return Resolution(found[0], "exact_id", 1.0, (Candidate(found[0], 1.0),), False)

    def _project_by_code(self, code: str) -> Resolution[Project]:
        raw = code.strip()
        wanted = set(_clean(self._project.mention(raw)))
        sharing = tuple(
            p
            for p in self._index.projects
            if raw in _clean(self._project.ids(p)) or wanted & set(_clean(self._project.forms(p)))
        )
        if not any(p.rotation for p in sharing):
            return self._run("project", self._project, code, self._index.projects)
        everyone = tuple(Candidate(p, 1.0) for p in sharing)
        if any(p.rotation == "unclassified" for p in sharing):
            return Resolution(None, "review", 1.0, everyone, True, "unclassified")
        holders = tuple(p for p in sharing if p.rotation in (None, "in"))
        if len(holders) == 1:
            method: Method = "exact_id" if holders[0].project_code == raw else "normalized"
            return Resolution(holders[0], method, 1.0, everyone, False)
        if holders:
            return Resolution(
                None, "review", 1.0, tuple(Candidate(p, 1.0) for p in holders), True, "several_in_rotation_holders"
            )
        return Resolution(None, "review", 1.0, everyone, True, "no_in_rotation_holder")

    def deliverable(self, text: str, *, project_id: str) -> Resolution[Deliverable]:
        """A Deliverable of one Project. Other Projects' Deliverables are never candidates."""
        pool = tuple(d for d in self._index.deliverables if self._same_project(d.project_id, project_id))
        return self._run("deliverable", self._deliverable, text, pool)

    def contact(self, mention: str, *, team: Iterable[str] = ()) -> Resolution[Contact]:
        """A Contact by name, alias, handle or Telegram user ID.

        Searches the Project's team (Contact page IDs from its role relations)
        first, then all Contacts.
        """
        team_ids = set(team)
        if team_ids:
            pool = tuple(c for c in self._index.contacts if c.page_id in team_ids)
            found = self._run("contact", self._contact, mention, pool)
            if found.entity is not None:
                return found
        return self._run("contact", self._contact, mention, self._index.contacts)

    def suggest_contacts(self, mention: str, *, team: Iterable[str] = ()) -> tuple[Candidate[Contact], ...]:
        """Up to 3 Contacts a person could choose from for a mention, best first (WRatio 60 or more).

        These are only suggestions: no call here decides, links or writes anything. Use `contact` to resolve.
        Among equal scores the Project's team (Contact page IDs) comes first.
        """
        policy = self._policies["contact"]
        team_ids = set(team)
        wanted = set(_clean((self._contact.fuzzy_mention or self._contact.mention)(mention.strip())))
        return self._rank(self._contact, wanted, self._index.contacts, policy, team_ids)

    def machine(self, hostname_or_serial: str) -> Resolution[Machine]:
        """A Machine by hostname or serial number."""
        return self._run("machine", self._machine, hostname_or_serial, self._index.machines)

    def studio(self, name: str) -> Resolution[Studio]:
        """A Studio by name or alias."""
        return self._run("studio", self._studio, name, self._index.studios)

    # Learning goes through approval

    def propose_alias(self, entity: Contact | Studio, mention: str) -> AliasWrite | None:
        """The write that would teach Notion that `mention` means `entity`.

        Call it once a human confirmed the match. Returns `None` when the
        entity already answers to the mention. The caller applies the write.
        """
        kind: _Kind[Any] = self._contact if isinstance(entity, Contact) else self._studio
        known = set(_clean(kind.forms(entity)))
        if known & set(_clean(kind.mention(mention))):
            return None
        alias = mention.strip().lstrip("@").strip()
        if not alias:
            return None
        database: Literal["Contacts", "Studios"] = "Contacts" if isinstance(entity, Contact) else "Studios"
        return AliasWrite(database=database, page_id=entity.page_id, alias=alias)

    # The pipeline

    def _run(self, policy_name: str, kind: _Kind[E], mention: str, pool: Sequence[E]) -> Resolution[E]:
        policy = self._policies[policy_name]
        raw = mention.strip()
        if not raw:
            return _NONE
        exact = [e for e in pool if raw in _clean(kind.ids(e))]
        if exact:
            return _pick(exact, "exact_id")
        for forms, mentions in kind.stages or ((kind.forms, kind.mention),):
            stage_wanted = set(_clean(mentions(raw)))
            normalized = [e for e in pool if stage_wanted & set(_clean(forms(e)))]
            if normalized:
                return _pick(normalized, "normalized")
        if not policy.fuzzy:
            return _NONE
        wanted = set(_clean((kind.fuzzy_mention or kind.mention)(raw)))
        ranked = self._rank(kind, wanted, pool, policy)
        if not ranked:
            return _NONE
        top = ranked[0]
        runner_up = ranked[1].score if len(ranked) > 1 else 0.0
        if top.score >= policy.fuzzy_accept and round(top.score - runner_up, 6) >= policy.margin:
            return Resolution(top.entity, "fuzzy", top.score, ranked, False)
        if policy.llm and self._judge is not None:
            decided = self._ask_judge(raw, ranked, policy)
            if decided is not None:
                return decided
        return Resolution(None, "review", top.score, ranked, True)

    @staticmethod
    def _rank(
        kind: _Kind[E], wanted: set[str], pool: Sequence[E], policy: Policy, first: frozenset[str] | set[str] = frozenset()
    ) -> tuple[Candidate[E], ...]:
        scored = []
        for entity in pool:
            forms = _clean((kind.fuzzy_forms or kind.forms)(entity))
            score = max((policy.scorer(w, f) for w in wanted for f in forms), default=0.0)
            if score >= policy.review_floor:
                scored.append(Candidate(entity, score))
        scored.sort(key=lambda c: (-c.score, c.entity.page_id not in first))
        return tuple(scored[: policy.max_candidates])

    def _ask_judge(self, mention: str, ranked: tuple[Candidate[E], ...], policy: Policy) -> Resolution[E] | None:
        assert self._judge is not None
        answer = self._judge(mention, [c.entity for c in ranked])
        if answer is None:
            return None
        known = {c.entity.page_id for c in ranked}
        odds = {page_id: p for page_id, p in answer.items() if page_id in known}
        if not odds:
            return None
        order = sorted(ranked, key=lambda c: -odds.get(c.entity.page_id, 0.0))
        best = odds.get(order[0].entity.page_id, 0.0)
        second = odds.get(order[1].entity.page_id, 0.0) if len(order) > 1 else 0.0
        if best >= policy.llm_accept and best - second >= policy.margin:
            return Resolution(order[0].entity, "llm", best, ranked, False)
        return None


_NONE: Resolution[Any] = Resolution(None, "none", 0.0, (), False)


def _pick(matches: Sequence[E], method: Method) -> Resolution[E]:
    """One match decides. Several are a tie and go to a human, never to the first."""
    candidates = tuple(Candidate(e, 1.0) for e in matches)
    if len(matches) == 1:
        return Resolution(matches[0], method, 1.0, candidates, False)
    return Resolution(None, "review", 1.0, candidates, True)
