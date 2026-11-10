"""Bring existing Notion Projects into the registry. Standard library only.

Three steps, each safe to repeat:

1. `preview` lists the distinct Project pages, what would be registered and what
   already is, and the references that hang on each page. It writes nothing.
2. `apply` registers every page that has no Project yet, **unclassified**, with
   its recorded code. One page is one Project: rows are never collapsed by code
   or name, so blank and repeated codes each get their own ID. A rerun keeps the
   IDs it already issued.
3. `classify` sets in or out of rotation for the Projects an operator names.
   Rotation is never inferred from production Status. Until a Project is
   classified it blocks its code for everyone else.

The reading of Notion is in `t6.notion.migration`; this module takes plain
records so it can be tested and run without Notion.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
import hashlib
import json

from t6.domain.events import JsonValue
from t6.registry.core import (
    ChangeOutcome,
    Changed,
    CodeHeld,
    PageAlreadyBound,
    ProjectRegistry,
    Registered,
    RegistryError,
    Rotation,
    normalize_code,
    normalize_page_id,
)
from t6.registry.refs import ProjectRefs

__all__ = [
    "ClassifyReport",
    "LegacyProject",
    "MigrationReport",
    "Preview",
    "PreviewEntry",
    "PreviewMismatch",
    "apply",
    "classify",
    "preview",
]

REFERENCES_NOT_LISTED = ("drives",)
"""Reference kinds no reader can list per Project yet. A preview always says so."""


class PreviewMismatch(Exception):
    """The Projects to register changed since the preview was exported."""


@dataclass(frozen=True, slots=True)
class LegacyProject:
    """One existing Notion Project page, as the migration needs it."""

    page_id: str
    name: str
    code: str | None
    references: Mapping[str, int] = field(default_factory=dict)
    """How many rows of each kind point at this page: `episodes`, `job_orders`, ..."""


@dataclass(frozen=True, slots=True)
class PreviewEntry:
    page_id: str
    name: str
    code: str | None
    action: str
    """`register`: no Project yet. `keep`: already registered, nothing to do.
    `blocked`: it cannot be registered as it is; see `problem`."""
    t6_id: str | None
    references: Mapping[str, int]
    problem: str | None = None

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "page_id": self.page_id,
            "name": self.name,
            "code": self.code,
            "action": self.action,
            "t6_id": self.t6_id,
            "references": dict(self.references),
            "problem": self.problem,
        }


@dataclass(frozen=True, slots=True)
class Preview:
    entries: tuple[PreviewEntry, ...]
    shared_codes: tuple[tuple[str, tuple[str, ...]], ...]
    """Codes on more than one page, with the pages. Each page still becomes its own Project."""
    blank_code_pages: tuple[str, ...]
    not_counted: tuple[str, ...] = REFERENCES_NOT_LISTED
    """Reference kinds that are not in `references`, so a missing count is not a zero."""

    @property
    def digest(self) -> str:
        """Identifies what `apply` would do. Changes when a page appears, goes or gets registered."""
        text = json.dumps(sorted((e.page_id, e.action) for e in self.entries))
        return hashlib.sha256(text.encode()).hexdigest()

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "digest": self.digest,
            "pages": len(self.entries),
            "to_register": sum(e.action == "register" for e in self.entries),
            "already_registered": sum(e.action == "keep" for e in self.entries),
            "blocked": sum(e.action == "blocked" for e in self.entries),
            "entries": [e.to_json() for e in self.entries],
            "shared_codes": [{"code": code, "page_ids": list(pages)} for code, pages in self.shared_codes],
            "blank_code_pages": list(self.blank_code_pages),
            "references_not_counted": list(self.not_counted),
        }


def preview(
    registry: ProjectRegistry, projects: Iterable[LegacyProject], *, not_counted: Iterable[str] = ()
) -> Preview:
    """What `apply` would do with these pages. Writes nothing.

    `not_counted` names reference kinds the caller could not read, beside the
    ones nobody can list yet.
    """
    entries: dict[str, PreviewEntry] = {}
    for project in projects:
        page = normalize_page_id(project.page_id)
        if page in entries:
            continue  # one page, one Project, however many times it was read
        held = registry.get_by_page(page)
        code: str | None = None
        problem: str | None = None
        try:
            code = normalize_code(project.code)[0]
        except RegistryError as error:
            problem = f"code {project.code!r}: {error}"
        entries[page] = PreviewEntry(
            page_id=page,
            name=project.name,
            code=code,
            action="keep" if held is not None else "blocked" if problem else "register",
            t6_id=None if held is None else held.id,
            references=dict(project.references),
            problem=problem,
        )
    by_form: dict[str, list[PreviewEntry]] = {}
    for entry in entries.values():
        if entry.code is not None:
            by_form.setdefault(normalize_code(entry.code)[1] or "", []).append(entry)
    shared = tuple(
        (group[0].code or "", tuple(e.page_id for e in group)) for group in by_form.values() if len(group) > 1
    )
    return Preview(
        entries=tuple(entries.values()),
        shared_codes=shared,
        blank_code_pages=tuple(e.page_id for e in entries.values() if e.code is None and e.problem is None),
        not_counted=tuple(dict.fromkeys((*REFERENCES_NOT_LISTED, *not_counted))),
    )


@dataclass(frozen=True, slots=True)
class MigrationReport:
    registered: tuple[tuple[str, str], ...]
    """(page ID, new T6 ID) for the Projects this run created."""
    kept: tuple[tuple[str, str], ...]
    """(page ID, T6 ID) for pages that already had a Project."""
    problems: tuple[tuple[str, str], ...]
    """(page ID, what went wrong) for pages that could not be registered."""
    unresolved: tuple[tuple[str, str | None], ...]
    """(T6 ID, code) of every unclassified Project in the registry, from any run."""

    @property
    def ok(self) -> bool:
        return not self.problems

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "ok": self.ok,
            "registered": [{"page_id": p, "t6_id": t} for p, t in self.registered],
            "kept": [{"page_id": p, "t6_id": t} for p, t in self.kept],
            "problems": [{"page_id": p, "problem": why} for p, why in self.problems],
            "unresolved": [{"t6_id": t, "code": c} for t, c in self.unresolved],
        }


def apply(
    registry: ProjectRegistry,
    projects: Iterable[LegacyProject],
    *,
    actor: str,
    expected_digest: str | None = None,
) -> MigrationReport:
    """Register every page that has no Project, unclassified. Safe to run again.

    With `expected_digest` (from the exported preview) the run is refused with
    `PreviewMismatch` when the pages to register are no longer the ones the
    operator looked at. A page that is already bound keeps its Project.
    """
    projects = tuple(projects)
    if expected_digest is not None and preview(registry, projects).digest != expected_digest:
        raise PreviewMismatch("the Projects changed since the preview was exported; export a new preview")
    registered: list[tuple[str, str]] = []
    kept: list[tuple[str, str]] = []
    problems: list[tuple[str, str]] = []
    seen: set[str] = set()
    for project in projects:
        page = normalize_page_id(project.page_id)
        if page in seen:
            continue
        seen.add(page)
        try:
            made = registry.register(
                actor=actor,
                idempotency_key=f"migrate:{page}",
                notion_page_id=page,
                code=project.code,
                rotation=Rotation.UNCLASSIFIED,
            )
        except RegistryError as error:
            problems.append((page, f"code {project.code!r}: {error}"))
            continue
        if isinstance(made, Registered):
            (registered if made.created else kept).append((page, made.project.id))
        elif isinstance(made, PageAlreadyBound):  # someone registered it first
            kept.append((page, made.project_id))
        else:
            problems.append((page, f"not registered: {type(made).__name__}"))
    unresolved = tuple((p.id, p.code) for p in registry.list_projects(Rotation.UNCLASSIFIED))
    return MigrationReport(tuple(registered), tuple(kept), tuple(problems), unresolved)


@dataclass(frozen=True, slots=True)
class ClassifyReport:
    classified: tuple[str, ...]
    """T6 IDs set to the rotation asked for."""
    unchanged: tuple[str, ...]
    """T6 IDs that were already classified. Classification never changes a classified Project."""
    unknown: tuple[str, ...]
    """References the registry does not know."""
    conflicts: tuple[tuple[str, str, tuple[str, ...]], ...]
    """(T6 ID, code, T6 IDs holding it): the Project cannot enter rotation until resolved."""
    stale: tuple[str, ...]
    """T6 IDs that changed while classifying; run again."""

    @property
    def ok(self) -> bool:
        return not (self.unknown or self.conflicts or self.stale)

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "ok": self.ok,
            "classified": list(self.classified),
            "unchanged": list(self.unchanged),
            "unknown": list(self.unknown),
            "conflicts": [{"t6_id": t, "code": c, "held_by": list(h)} for t, c, h in self.conflicts],
            "stale": list(self.stale),
        }


def classify(registry: ProjectRegistry, decisions: Mapping[str, Rotation], *, actor: str) -> ClassifyReport:
    """Set the rotation of the unclassified Projects named in `decisions`.

    Each key is a T6 ID or a Notion page ID. Only `in` and `out` are decisions,
    and only the Projects named are touched: nothing is inferred for the rest.
    Projects going out are classified before those going in. A Project that
    wants a code another unclassified Project still holds is reported as a
    conflict until that one is classified.
    """
    refs = ProjectRefs(registry)
    # Out first: an unclassified Project blocks its code, so a Project going out
    # of rotation in this batch must be settled before one that takes its code.
    ordered = sorted(decisions.items(), key=lambda item: item[1] is not Rotation.OUT)
    classified: list[str] = []
    unchanged: list[str] = []
    unknown: list[str] = []
    conflicts: list[tuple[str, str, tuple[str, ...]]] = []
    stale: list[str] = []
    for ref, rotation in ordered:
        if rotation is Rotation.UNCLASSIFIED:
            raise RegistryError(f"{ref}: a decision is in or out of rotation")
        project = refs.project(ref)
        if project is None:
            unknown.append(ref)
        elif project.rotation is not Rotation.UNCLASSIFIED:
            unchanged.append(project.id)
        else:
            outcome: ChangeOutcome = registry.set_rotation(
                project.id,
                rotation,
                actor=actor,
                idempotency_key=f"classify:{project.id}:{rotation.value}",
                expected_revision=project.revision,
            )
            if isinstance(outcome, Changed):
                classified.append(project.id)
            elif isinstance(outcome, CodeHeld):
                conflicts.append((project.id, outcome.code, outcome.held_by))
            else:
                stale.append(project.id)
    return ClassifyReport(
        tuple(classified), tuple(unchanged), tuple(unknown), tuple(conflicts), tuple(stale)
    )
