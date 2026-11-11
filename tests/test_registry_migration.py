"""Migrating existing Projects: preview, apply, classify, then read the same relationships both ways.

The registry is the real SQLite one in a temp directory. Notion is the in-memory
fake. Nothing opens a connection.
"""

from collections.abc import Iterable
import contextlib
from datetime import date
import io
import json
from pathlib import Path
from typing import Any
import unittest
from unittest import mock

from test_registry_service import PAGE, PAGE_DASHED, RegistryTestCase

from t6.domain import Deliverable, ExternalRef, Project, ProjectStatusChanged
from t6.drives import Assignment, Drive, Ref, Role
from t6.drives.testing import InMemoryDriveStore
from t6.lookup import Index, Lookup
from t6.notion.external_links import ExternalLinks
from t6.notion.migration import read_legacy_projects
from t6.notion.testing import FakeNotion, external_link_properties, project_properties, relation, title
from t6.registry import (
    PROJECT_ID_PREFIX,
    CodeHeld,
    PageAlreadyBound,
    ProjectRefs,
    RegistryError,
    Rotation,
)
from t6.registry.migration import LegacyProject, PreviewMismatch, apply, classify, preview

ACTOR = "operator"
OTHER_PAGE = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
THIRD_PAGE = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
FOURTH_PAGE = "cccccccccccccccccccccccccccccccc"


def legacy(*rows: tuple[str, str | None], **references: int) -> list[LegacyProject]:
    return [LegacyProject(page, f"Show {i}", code, references) for i, (page, code) in enumerate(rows)]


class MigrationTestCase(RegistryTestCase):
    def pages(self, report_pairs: Iterable[tuple[str, str]]) -> dict[str, str]:
        return dict(report_pairs)


class PreviewTests(MigrationTestCase):
    def test_preview_lists_distinct_pages_and_writes_nothing(self) -> None:
        rows = legacy((PAGE, "ABC"), (PAGE_DASHED, "ABC"), (OTHER_PAGE, "abc"), (THIRD_PAGE, None), (FOURTH_PAGE, " "))
        before = self.registry.list_projects()
        shown = preview(self.registry, rows)

        self.assertEqual([e.page_id for e in shown.entries], [PAGE, OTHER_PAGE, THIRD_PAGE, FOURTH_PAGE])
        self.assertEqual({e.action for e in shown.entries}, {"register"})
        self.assertEqual(shown.shared_codes, (("ABC", (PAGE, OTHER_PAGE)),))
        self.assertEqual(shown.blank_code_pages, (THIRD_PAGE, FOURTH_PAGE))
        self.assertEqual(self.registry.list_projects(), before)
        self.assertEqual(self.registry.pending_sync(), ())

    def test_preview_carries_the_references_each_page_has_and_names_what_it_cannot_count(self) -> None:
        shown = preview(self.registry, legacy((PAGE, "ABC"), job_orders=3, deliverables=2), not_counted=["external_links"])
        report = shown.to_json()
        self.assertEqual(shown.entries[0].references, {"job_orders": 3, "deliverables": 2})
        self.assertEqual(json.loads(json.dumps(report))["entries"][0]["references"], {"job_orders": 3, "deliverables": 2})
        self.assertEqual(report["references_not_counted"], ["drives", "external_links"])
        self.assertEqual((report["pages"], report["to_register"], report["already_registered"]), (1, 1, 0))

    def test_a_registered_page_is_kept_and_an_unusable_code_is_blocked(self) -> None:
        apply(self.registry, legacy((PAGE, "ABC")), actor=ACTOR)
        shown = preview(self.registry, legacy((PAGE, "ABC"), (OTHER_PAGE, "---")))
        self.assertEqual([e.action for e in shown.entries], ["keep", "blocked"])
        self.assertTrue(shown.entries[0].t6_id and shown.entries[0].t6_id.startswith(PROJECT_ID_PREFIX))
        self.assertIn("letter or digit", shown.entries[1].problem or "")


class ApplyTests(MigrationTestCase):
    def test_every_page_becomes_its_own_unclassified_project_whatever_its_code(self) -> None:
        report = apply(
            self.registry, legacy((PAGE, "ABC"), (OTHER_PAGE, "ABC"), (THIRD_PAGE, None), (FOURTH_PAGE, None)), actor=ACTOR
        )
        self.assertTrue(report.ok)
        ids = [t6 for _, t6 in report.registered]
        self.assertEqual(len(set(ids)), 4)
        projects = self.registry.list_projects()
        self.assertEqual({p.rotation for p in projects}, {Rotation.UNCLASSIFIED})
        self.assertEqual([p.code for p in projects], ["ABC", "ABC", None, None])
        self.assertEqual([t6 for t6, _ in report.unresolved], ids)
        self.assertEqual({a.actor for p in projects for a in self.registry.audit(p.id)}, {ACTOR})

    def test_a_rerun_keeps_the_ids_and_adds_nothing(self) -> None:
        rows = legacy((PAGE, "ABC"), (OTHER_PAGE, None))
        first = apply(self.registry, rows, actor=ACTOR)
        audits = [len(self.registry.audit(p.id)) for p in self.registry.list_projects()]
        again = apply(self.registry, rows, actor=ACTOR)

        self.assertEqual(again.registered, ())
        self.assertEqual(self.pages(again.kept), self.pages(first.registered))
        self.assertEqual(len(self.registry.list_projects()), 2)
        self.assertEqual([len(self.registry.audit(p.id)) for p in self.registry.list_projects()], audits)

    def test_a_rerun_after_a_restart_and_after_a_partial_run_finishes_the_rest(self) -> None:
        first = apply(self.registry, legacy((PAGE, "ABC")), actor=ACTOR)
        self.start()
        second = apply(self.registry, legacy((PAGE, "ABC"), (OTHER_PAGE, "XYZ")), actor=ACTOR)
        self.assertEqual(self.pages(second.kept), self.pages(first.registered))
        self.assertEqual([p for p, _ in second.registered], [OTHER_PAGE])

    def test_an_unusable_code_is_reported_and_the_other_pages_still_migrate(self) -> None:
        report = apply(self.registry, legacy((PAGE, "---"), (OTHER_PAGE, "ABC")), actor=ACTOR)
        self.assertFalse(report.ok)
        self.assertEqual([p for p, _ in report.problems], [PAGE])
        self.assertEqual([p for p, _ in report.registered], [OTHER_PAGE])
        self.assertIsNone(self.registry.get_by_page(PAGE))

    def test_apply_refuses_when_the_pages_changed_since_the_preview(self) -> None:
        rows = legacy((PAGE, "ABC"))
        digest = preview(self.registry, rows).digest
        with self.assertRaises(PreviewMismatch):
            apply(self.registry, legacy((PAGE, "ABC"), (OTHER_PAGE, "XYZ")), actor=ACTOR, expected_digest=digest)
        self.assertEqual(self.registry.list_projects(), ())
        apply(self.registry, rows, actor=ACTOR, expected_digest=digest)
        self.assertEqual(len(self.registry.list_projects()), 1)

    def test_nothing_is_matched_by_code_or_name(self) -> None:
        existing = apply(self.registry, [LegacyProject(PAGE, "Show", "ABC")], actor=ACTOR).registered[0][1]
        report = apply(self.registry, [LegacyProject(OTHER_PAGE, "Show", "ABC")], actor=ACTOR)
        self.assertNotEqual(report.registered[0][1], existing)


class ClassifyTests(MigrationTestCase):
    def setUp(self) -> None:
        super().setUp()
        report = apply(
            self.registry, legacy((PAGE, "ABC"), (OTHER_PAGE, "abc"), (THIRD_PAGE, "XYZ"), (FOURTH_PAGE, None)), actor=ACTOR
        )
        self.ids = {page: t6 for page, t6 in report.registered}

    def test_an_unclassified_project_blocks_its_code_until_it_is_classified(self) -> None:
        held = self.registry.register(actor=ACTOR, idempotency_key="new", code="ABC", rotation=Rotation.IN)
        assert isinstance(held, CodeHeld)
        self.assertEqual((held.reason, set(held.held_by)), ("unclassified", {self.ids[PAGE], self.ids[OTHER_PAGE]}))

        classify(self.registry, {PAGE: Rotation.OUT, OTHER_PAGE: Rotation.OUT}, actor=ACTOR)
        taken = self.registry.register(actor=ACTOR, idempotency_key="new", code="ABC", rotation=Rotation.IN)
        self.assertNotIsInstance(taken, CodeHeld)

    def test_classifying_by_page_id_or_t6_id_sets_only_the_projects_named(self) -> None:
        done = classify(self.registry, {THIRD_PAGE: Rotation.IN, self.ids[PAGE]: Rotation.OUT}, actor=ACTOR)
        self.assertTrue(done.ok)
        self.assertEqual(set(done.classified), {self.ids[PAGE], self.ids[THIRD_PAGE]})
        by_page = {p: self.registry.get(t6) for p, t6 in self.ids.items()}
        rotations = {p: project.rotation for p, project in by_page.items() if project}
        self.assertEqual(
            rotations,
            {PAGE: Rotation.OUT, OTHER_PAGE: Rotation.UNCLASSIFIED, THIRD_PAGE: Rotation.IN, FOURTH_PAGE: Rotation.UNCLASSIFIED},
        )
        self.assertEqual(self.registry.audit(self.ids[PAGE])[-1].action, "classified")

    def test_two_historical_projects_with_one_code_block_each_other_until_one_is_settled_out(self) -> None:
        report = classify(self.registry, {PAGE: Rotation.IN, OTHER_PAGE: Rotation.IN}, actor=ACTOR)
        self.assertFalse(report.ok)
        self.assertEqual(report.classified, ())
        self.assertEqual(
            report.conflicts,
            (
                (self.ids[PAGE], "ABC", (self.ids[OTHER_PAGE],)),
                (self.ids[OTHER_PAGE], "abc", (self.ids[PAGE],)),
            ),
        )
        for t6 in (self.ids[PAGE], self.ids[OTHER_PAGE]):
            project = self.registry.get(t6)
            assert project is not None
            self.assertIs(project.rotation, Rotation.UNCLASSIFIED)  # refused, nothing changed

    def test_a_batch_settles_the_project_going_out_before_the_one_taking_its_code(self) -> None:
        report = classify(self.registry, {PAGE: Rotation.IN, OTHER_PAGE: Rotation.OUT}, actor=ACTOR)
        self.assertTrue(report.ok, report)
        self.assertEqual(set(report.classified), {self.ids[PAGE], self.ids[OTHER_PAGE]})
        other = self.registry.get(self.ids[OTHER_PAGE])
        assert other is not None
        self.assertEqual((other.rotation, other.code), (Rotation.OUT, "abc"))  # its record keeps the code

    def test_a_rerun_changes_nothing_and_a_classified_project_is_never_reclassified(self) -> None:
        classify(self.registry, {THIRD_PAGE: Rotation.IN}, actor=ACTOR)
        again = classify(self.registry, {THIRD_PAGE: Rotation.OUT}, actor=ACTOR)
        self.assertEqual((again.classified, again.unchanged), ((), (self.ids[THIRD_PAGE],)))
        kept = self.registry.get(self.ids[THIRD_PAGE])
        assert kept is not None
        self.assertIs(kept.rotation, Rotation.IN)

    def test_unknown_references_are_reported_and_unclassified_is_not_a_decision(self) -> None:
        report = classify(self.registry, {"missing-page": Rotation.IN}, actor=ACTOR)
        self.assertEqual((report.ok, report.unknown), (False, ("missing-page",)))
        with self.assertRaises(RegistryError):
            classify(self.registry, {PAGE: Rotation.UNCLASSIFIED}, actor=ACTOR)

    def test_the_unclassified_projects_are_listed_through_the_service(self) -> None:
        classify(self.registry, {PAGE: Rotation.OUT}, actor=ACTOR)
        listed = self.send("GET", "/registry/unclassified")
        self.assertEqual(listed.status, 200)
        found: list[dict[str, Any]] = list(listed.body["projects"])  # type: ignore[arg-type]
        self.assertEqual([p["id"] for p in found], [self.ids[p] for p in (OTHER_PAGE, THIRD_PAGE, FOURTH_PAGE)])
        self.assertEqual(self.send("POST", "/registry/unclassified", {}, key="k").status, 405)
        self.assertEqual(self.send("GET", "/registry/unclassified", secret="nope").status, 401)


class IdentityTests(MigrationTestCase):
    def test_a_project_is_the_same_by_t6_id_whatever_page_it_is_read_from(self) -> None:
        old = Project(page_id="p1", name="Show", t6_id=f"{PROJECT_ID_PREFIX}a")
        replaced = Project(page_id="p2", name="Show renamed", t6_id=f"{PROJECT_ID_PREFIX}a")
        other = Project(page_id="p1", name="Show", t6_id=f"{PROJECT_ID_PREFIX}b")
        self.assertEqual(old, replaced)
        self.assertEqual(len({old, replaced}), 1)
        self.assertNotEqual(old, other)  # the page does not make it the same Project

    def test_an_unregistered_project_is_still_identified_by_its_page(self) -> None:
        self.assertEqual(Project(page_id="p1", name="A"), Project(page_id="p1", name="B"))
        self.assertNotEqual(Project(page_id="p1", name="A"), Project(page_id="p2", name="A"))
        self.assertEqual(Project(page_id="p1", name="A", t6_id=" ").key, "p1")

    def test_episodes_follow_the_projects_identity(self) -> None:
        series = Project(page_id="p1", name="Show", project_type="Limited Series", eps=2)
        registered = Project(page_id="p1", name="Show", project_type="Limited Series", eps=2, t6_id="t6p_a")
        self.assertEqual({e.project_id for e in series.episodes}, {"p1"})
        self.assertEqual({e.project_id for e in registered.episodes}, {"t6p_a"})

    def test_a_replacement_page_resolves_to_the_same_project_and_the_old_page_is_not_reassigned(self) -> None:
        t6 = apply(self.registry, legacy((PAGE, "ABC")), actor=ACTOR).registered[0][1]
        refs = ProjectRefs(self.registry)
        self.assertEqual(self.send("POST", f"/registry/projects/{t6}/bindings", {"notion_page_id": OTHER_PAGE, "expected_revision": 1}, key="bind").status, 200)

        for ref in (t6, PAGE, PAGE_DASHED, OTHER_PAGE):
            self.assertEqual(refs.identity(ref), t6, ref)
        self.assertEqual(refs.pages(t6), (OTHER_PAGE, PAGE))  # the active page first
        self.assertEqual(refs.pages(PAGE), (OTHER_PAGE, PAGE))
        self.assertTrue(refs.same(PAGE, OTHER_PAGE))
        self.assertTrue(refs.same(t6, PAGE))
        rival = self.registry.register(actor=ACTOR, idempotency_key="rival", notion_page_id=PAGE)
        self.assertIsInstance(rival, PageAlreadyBound)

    def test_references_the_registry_does_not_know_are_unmigrated_pages(self) -> None:
        refs = ProjectRefs(self.registry)
        self.assertIsNone(refs.identity("some-page"))
        self.assertEqual(refs.pages("some-page"), ("some-page",))
        self.assertEqual(refs.pages(f"{PROJECT_ID_PREFIX}nope"), ())
        self.assertTrue(refs.same("some-page", "some-page"))
        self.assertFalse(refs.same("some-page", "other-page"))
        self.assertFalse(refs.same(None, None))
        self.assertFalse(refs.same(f"{PROJECT_ID_PREFIX}x", f"{PROJECT_ID_PREFIX}y"))

    def test_the_same_code_never_resolves_a_reference_to_another_project(self) -> None:
        report = apply(self.registry, legacy((PAGE, "ABC"), (OTHER_PAGE, "ABC")), actor=ACTOR)
        first, second = (t6 for _, t6 in report.registered)
        classify(self.registry, {PAGE: Rotation.OUT, OTHER_PAGE: Rotation.IN}, actor=ACTOR)
        refs = ProjectRefs(self.registry)
        self.assertEqual((refs.identity(PAGE), refs.identity(OTHER_PAGE)), (first, second))

    def test_a_project_start_callback_for_any_of_its_pages_resolves_to_one_project(self) -> None:
        t6 = apply(self.registry, legacy((PAGE, "ABC")), actor=ACTOR).registered[0][1]
        self.send("POST", f"/registry/projects/{t6}/bindings", {"notion_page_id": OTHER_PAGE, "expected_revision": 1}, key="bind")
        refs = ProjectRefs(self.registry)
        old = ProjectStatusChanged(page_id=PAGE, status="Project Start")
        new = ProjectStatusChanged(page_id=OTHER_PAGE, status="Project Start")
        # The events stay page-keyed, as Project Start expects; the registry joins them.
        self.assertEqual(old.dedupe_key, f"notion:project.status:{PAGE}:Project Start")
        self.assertNotEqual(old, new)
        self.assertEqual(refs.identity(old.page_id), refs.identity(new.page_id))
        self.assertEqual(refs.identified(Project(page_id=PAGE, name="Show")).t6_id, t6)
        self.assertIsNone(refs.identified(Project(page_id="unmigrated", name="Show")).t6_id)


PROJECTS_SOURCE = "ds-projects"
JOB_ORDERS_SOURCE = "ds-job-orders"
DELIVERABLES_SOURCE = "ds-deliverables"
LINKS_SOURCE = "ds-links"
SOURCES = {
    "projects": PROJECTS_SOURCE,
    "job_orders": JOB_ORDERS_SOURCE,
    "deliverables": DELIVERABLES_SOURCE,
    "external_links": LINKS_SOURCE,
}


class NotionRelationsTests(MigrationTestCase):
    """Preview, apply, classify, then read the same relationships by T6 ID and by page ID."""

    def setUp(self) -> None:
        super().setUp()
        self.notion = FakeNotion()
        self.notion.add_snapshot_data_source(PROJECTS_SOURCE, "projects")
        self.notion.add_snapshot_data_source(JOB_ORDERS_SOURCE, "job_orders")
        self.notion.add_snapshot_data_source(DELIVERABLES_SOURCE, "deliverables")
        self.notion.add_data_source(LINKS_SOURCE)
        self.a = self.show("Show A", "ABC", PAGE_DASHED.lower())
        self.b = self.show("Show B", "abc", OTHER_PAGE)  # historical: same code
        self.c = self.show("Show C", None, THIRD_PAGE, project_type="Limited Series", eps=3)
        self.notion.add_page(JOB_ORDERS_SOURCE, {"Name": title("Conform"), "Projects": relation(self.a)})
        self.notion.add_page(DELIVERABLES_SOURCE, {"Name": title("Final Master"), "\U0001f5fa️ Projects": relation(self.a)}, page_id="d-1")
        self.notion.add_page(DELIVERABLES_SOURCE, {"Name": title("Trailer"), "\U0001f5fa️ Projects": relation(self.b)}, page_id="d-2")
        self.frameio = ExternalRef(system="frameio", kind="project", external_id="f-1")
        self.notion.add_page(
            LINKS_SOURCE,
            external_link_properties(system="frameio", kind="project", external_id="f-1", projects=[self.a]),
        )

    def show(self, name: str, code: str | None, page: str, **fields: Any) -> str:
        return str(self.notion.add_page(PROJECTS_SOURCE, project_properties(name=name, project_code=code, **fields), page_id=page)["id"])

    def test_preview_counts_each_pages_references_and_never_collapses_the_shared_code(self) -> None:
        with self.notion.client() as client:
            projects, not_counted = read_legacy_projects(client, SOURCES)
        shown = preview(self.registry, projects, not_counted=not_counted).to_json()
        entries: list[dict[str, Any]] = list(shown["entries"])  # type: ignore[arg-type]
        by_name = {e["name"]: e["references"] for e in entries}
        self.assertEqual(by_name["Show A"], {"job_orders": 1, "deliverables": 1, "external_links": 1, "episodes": 0})
        self.assertEqual(by_name["Show B"], {"job_orders": 0, "deliverables": 1, "external_links": 0, "episodes": 0})
        self.assertEqual(by_name["Show C"]["episodes"], 3)
        self.assertEqual((shown["to_register"], shown["references_not_counted"]), (3, ["drives"]))
        self.assertEqual(len(shown["shared_codes"]), 1)  # type: ignore[arg-type]

    def test_a_data_source_that_was_not_given_is_named_rather_than_counted_as_zero(self) -> None:
        with self.notion.client() as client:
            _, not_counted = read_legacy_projects(client, {"projects": PROJECTS_SOURCE})
        self.assertEqual(not_counted, ["job_orders", "deliverables", "external_links"])
        with self.notion.client() as client, self.assertRaises(ValueError):
            read_legacy_projects(client, {})

    def migrate(self) -> dict[str, str]:
        with self.notion.client() as client:
            projects, _ = read_legacy_projects(client, SOURCES)
        report = apply(self.registry, projects, actor=ACTOR)
        classify(self.registry, {PAGE: Rotation.OUT, OTHER_PAGE: Rotation.IN, THIRD_PAGE: Rotation.IN}, actor=ACTOR)
        return {page: t6 for page, t6 in report.registered}

    def test_external_links_read_the_same_rows_by_t6_id_and_by_page_id(self) -> None:
        ids = self.migrate()
        refs = ProjectRefs(self.registry)
        with self.notion.client() as client:
            links = ExternalLinks(client, LINKS_SOURCE, refs)
            by_t6 = links.refs_for_project(ids[PAGE])
            by_page = links.refs_for_project(self.a)
            self.assertEqual((by_t6, by_page), ((self.frameio,), (self.frameio,)))
            self.assertEqual(links.refs_for_project(ids[OTHER_PAGE]), ())  # the same code never borrows rows
            self.assertEqual(links.refs_for_project(f"{PROJECT_ID_PREFIX}unknown"), ())
            # Without the registry the old behaviour holds: a Project is its page ID.
            self.assertEqual(ExternalLinks(client, LINKS_SOURCE).refs_for_project(self.a), (self.frameio,))

    def test_rows_stay_attached_after_the_page_is_replaced(self) -> None:
        ids = self.migrate()
        t6 = ids[PAGE]
        self.send("POST", f"/registry/projects/{t6}/bindings", {"notion_page_id": FOURTH_PAGE, "expected_revision": self.registry.get(t6).revision}, key="bind")  # type: ignore[union-attr]
        with self.notion.client() as client:
            links = ExternalLinks(client, LINKS_SOURCE, ProjectRefs(self.registry))
            self.assertEqual(links.refs_for_project(FOURTH_PAGE), (self.frameio,))  # the new page, through the Project
            self.assertEqual(links.refs_for_project(t6), (self.frameio,))
            made = links.ensure(ExternalRef(system="resolve", kind="project", external_id="db/A"), project_id=t6)
            self.assertEqual(made.project_ids, (FOURTH_PAGE,))  # written against the active page
            self.assertEqual(len(links.refs_for_project(PAGE)), 2)  # and found from the old one
            with self.assertRaises(ValueError):
                links.ensure(ExternalRef(system="resolve", kind="project", external_id="x"), project_id=f"{PROJECT_ID_PREFIX}nope")

    def test_deliverable_lookup_takes_either_form_of_the_project(self) -> None:
        ids = self.migrate()
        refs = ProjectRefs(self.registry)
        master = Deliverable(page_id="d-1", name="Final Master", project_id=self.a)
        trailer = Deliverable(page_id="d-2", name="Trailer", project_id=self.b)
        index = Index(deliverables=(master, trailer))
        lookup = Lookup(index, same_project=refs.same)
        for ref in (ids[PAGE], self.a, PAGE):
            found = lookup.deliverable("Final Master", project_id=ref)
            self.assertEqual(found.entity, master, ref)
        wrong = lookup.deliverable("Trailer", project_id=ids[PAGE])
        self.assertIsNone(wrong.entity)  # Show B's Deliverable never answers for Show A, same code or not
        self.assertEqual(Lookup(index).deliverable("Final Master", project_id=ids[PAGE]).entity, None)  # old behaviour: page IDs only

    def test_drive_assignments_are_found_by_t6_id_and_by_any_page(self) -> None:
        ids = self.migrate()
        store = InMemoryDriveStore(ProjectRefs(self.registry).same)
        first = store.add_drive(Drive(serial="D1"))
        second = store.add_drive(Drive(serial="D2"))
        store.add_assignment(first.id, Assignment(label="M01", project=Ref(page_id=PAGE), role=Role.MASTER, start=date(2026, 1, 1)))
        store.add_assignment(second.id, Assignment(label="M01", project=Ref(page_id=OTHER_PAGE), role=Role.MASTER, start=date(2026, 1, 1)))
        store.add_assignment(second.id, Assignment(label="U1", project=Ref(text="Unresolved show"), role=Role.BACKUP, start=date(2026, 1, 1)))
        for ref in (Ref(page_id=ids[PAGE]), Ref(page_id=PAGE_DASHED)):
            self.assertEqual([d.serial for d, _ in store.open_assignments(ref)], ["D1"], ref)
        self.assertEqual([d.serial for d, _ in store.open_assignments(Ref(page_id=ids[OTHER_PAGE]))], ["D2"])
        self.assertEqual([a.label for _, a in store.open_assignments(Ref(text="Unresolved show"))], ["U1"])

    def test_the_project_read_from_notion_gains_its_t6_id_and_keeps_its_episodes(self) -> None:
        ids = self.migrate()
        from t6.notion.models import project_from_page

        refs = ProjectRefs(self.registry)
        series = refs.identified(project_from_page(self.notion.page(THIRD_PAGE)))
        self.assertEqual(series.t6_id, ids[THIRD_PAGE])
        self.assertEqual([e.project_id for e in series.episodes], [ids[THIRD_PAGE]] * 3)

    def test_a_migration_rerun_over_the_same_relations_keeps_every_id(self) -> None:
        first = self.migrate()
        with self.notion.client() as client:
            projects, _ = read_legacy_projects(client, SOURCES)
        again = apply(self.registry, projects, actor=ACTOR)
        self.assertEqual((again.registered, {p: t for p, t in again.kept}), ((), first))


class CommandTests(MigrationTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.notion = FakeNotion()
        self.notion.add_snapshot_data_source(PROJECTS_SOURCE, "projects")
        self.notion.add_page(PROJECTS_SOURCE, project_properties(name="A", project_code="ABC"), page_id=PAGE)
        self.notion.add_page(PROJECTS_SOURCE, project_properties(name="B", project_code="ABC"), page_id=OTHER_PAGE)
        directory = Path(self.directory.name)
        self.sources = directory / "sources.json"
        self.sources.write_text(json.dumps({"projects": PROJECTS_SOURCE}))
        self.db = directory / "cli-registry.sqlite3"

    def run_command(self, *argv: str) -> tuple[int, Any]:
        from t6.cli import main

        out = io.StringIO()
        with (
            mock.patch.dict("os.environ", {"T6_NOTION_TOKEN": "token"}),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            status = main(list(argv), open_notion=lambda token: self.notion.client())
        text = out.getvalue()
        return status, json.loads(text) if text else None

    def test_preview_then_apply_then_classify_then_a_rerun(self) -> None:
        base = ("--data-sources", str(self.sources), "--registry-db", str(self.db))
        status, shown = self.run_command("migrate-projects", *base)
        self.assertEqual((status, shown["to_register"], shown["to_register"] == len(shown["entries"])), (0, 2, True))
        preview_file = Path(self.directory.name) / "preview.json"
        preview_file.write_text(json.dumps(shown))
        self.assertFalse(self.db.exists() and self.registry_count() > 0)

        refused, _ = self.run_command("migrate-projects", *base, "--apply")
        self.assertEqual(refused, 2)  # no preview named
        status, report = self.run_command("migrate-projects", *base, "--apply", "--preview", str(preview_file), "--actor", "cli")
        self.assertEqual((status, len(report["registered"]), len(report["unresolved"])), (0, 2, 2))

        status, again = self.run_command("migrate-projects", *base, "--apply", "--preview", str(preview_file), "--actor", "cli")
        self.assertEqual(status, 2)  # the preview is out of date: both pages are registered now
        status, fresh = self.run_command("migrate-projects", *base)
        self.assertEqual((fresh["to_register"], fresh["already_registered"]), (0, 2))

        decisions = Path(self.directory.name) / "decisions.json"
        decisions.write_text(json.dumps({PAGE: "out", OTHER_PAGE: "in"}))
        status, done = self.run_command("classify-projects", "--registry-db", str(self.db), "--decisions", str(decisions), "--actor", "cli")
        self.assertEqual((status, len(done["classified"])), (0, 2))
        status, done = self.run_command("classify-projects", "--registry-db", str(self.db), "--decisions", str(decisions), "--actor", "cli")
        self.assertEqual((status, done["classified"], len(done["unchanged"])), (0, [], 2))

    def test_a_stale_preview_is_refused_and_a_bad_decision_cannot_run(self) -> None:
        base = ("--data-sources", str(self.sources), "--registry-db", str(self.db))
        _, shown = self.run_command("migrate-projects", *base)
        preview_file = Path(self.directory.name) / "preview.json"
        preview_file.write_text(json.dumps(shown))
        self.notion.add_page(PROJECTS_SOURCE, project_properties(name="C", project_code="XYZ"), page_id=THIRD_PAGE)
        status, _ = self.run_command("migrate-projects", *base, "--apply", "--preview", str(preview_file), "--actor", "cli")
        self.assertEqual(status, 2)
        self.assertEqual(self.registry_count(), 0)

        decisions = Path(self.directory.name) / "bad.json"
        decisions.write_text(json.dumps({PAGE: "maybe"}))
        status, _ = self.run_command("classify-projects", "--registry-db", str(self.db), "--decisions", str(decisions), "--actor", "cli")
        self.assertEqual(status, 2)

    def registry_count(self) -> int:
        from t6.registry import SqliteProjectRegistry

        return len(SqliteProjectRegistry(self.db).list_projects())


if __name__ == "__main__":
    unittest.main()
