"""Reusable Project Codes through lookup, the schema report and the CLI.

The registry is the real SQLite one in a temp directory; Notion is the fake.
Projects reach `Lookup` the way a caller builds them: read from Notion, then
completed from the registry with `ProjectRefs.identified`.
"""

from contextlib import redirect_stderr, redirect_stdout
import dataclasses
import io
import json
import os
from pathlib import Path
import tempfile
from typing import Any
import unittest
from unittest import mock

from t6.cli import main
from t6.domain import Deliverable, Project
from t6.lookup import Index, Lookup
from t6.notion.models import project_from_page
from t6.notion.schema import check_schema, load_snapshot
from t6.notion.testing import FakeNotion, project_properties
from t6.registry import (
    ProjectRefs,
    RegisteredProject,
    RegistryUnavailable,
    Rotation,
    Shown,
    SqliteProjectRegistry,
)
from t6.registry.check import KINDS
from t6.server.testing import FakeClock

PROJECTS = "ds-projects"
ACTOR = "test"


class Damaged(SqliteProjectRegistry):
    """A registry whose file shows what the registry itself never allows."""

    twin: bool = False

    def list_projects(self, rotation: Rotation | None = None) -> tuple[RegisteredProject, ...]:
        found = super().list_projects(rotation)
        if self.twin and found:
            return (*found, dataclasses.replace(found[0], id="t6p_twin"))
        return found


class Down(SqliteProjectRegistry):
    def list_projects(self, rotation: Rotation | None = None) -> tuple[RegisteredProject, ...]:
        raise RegistryUnavailable("database is locked")


class WorldTestCase(unittest.TestCase):
    """A Notion Projects data source and a registry that agree, built one Project at a time."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "registry.sqlite3"
        self.registry = Damaged(self.path, FakeClock())
        self.notion = FakeNotion()
        self.notion.add_snapshot_data_source(PROJECTS, "projects")
        self.keys = 0

    def show(self, name: str, code: str | None, rotation: Rotation | None = Rotation.OUT, *, sync: bool = True) -> Project:
        """A Project in Notion and, unless `rotation` is None, in the registry, bound to its page."""
        page = str(self.notion.add_page(PROJECTS, project_properties(name=name, project_code=code))["id"])
        if rotation is not None:
            self.keys += 1
            made = self.registry.register(
                actor=ACTOR, idempotency_key=f"k{self.keys}", notion_page_id=page, code=code, rotation=rotation
            )
            assert hasattr(made, "project"), made
            if sync:
                self.registry.report_sync(made.project.id, 1, ok=True)
        return project_from_page(self.notion.page(page))

    def index(self) -> Index:
        refs = ProjectRefs(self.registry)
        with self.notion.client() as client:
            rows = client.query_data_source(PROJECTS)
        return Index(projects=tuple(refs.identified(project_from_page(row)) for row in rows))

    def lookup(self) -> Lookup:
        return Lookup(self.index(), same_project=ProjectRefs(self.registry).same)

    def t6_id(self, project: Project) -> str:
        found = ProjectRefs(self.registry).identity(project.page_id)
        assert found is not None
        return found

    def report(self, registry: SqliteProjectRegistry | None = None) -> dict[str, Any]:
        with self.notion.client() as client:
            return dict(check_schema(client, load_snapshot(), {"projects": PROJECTS}, registry or self.registry))

    def findings(self, report: dict[str, Any]) -> dict[str, Any]:
        found: dict[str, Any] = report["project_codes"]["findings"]
        return found


class BareCodeLookupTests(WorldTestCase):
    def test_a_leaves_rotation_b_reuses_the_code_and_a_bare_code_finds_b(self) -> None:
        a = self.show("Show A", "ABC", Rotation.IN)
        self.registry.set_rotation(
            self.t6_id(a), Rotation.OUT, actor=ACTOR, idempotency_key="a-out", expected_revision=1
        )
        b = self.show("Show B", "abc", Rotation.IN)

        lookup = self.lookup()
        for mention in ("ABC", "abc", "a-b-c", " ABC "):
            found = lookup.project(mention)
            self.assertEqual(found.entity, self.index().projects[1], mention)
            self.assertFalse(found.needs_review)
        self.assertEqual(lookup.project("ABC").entity.project_code, "abc")  # type: ignore[union-attr]  # display spelling kept
        self.assertEqual(lookup.project("abc").method, "exact_id")
        self.assertEqual(lookup.project("ABC").method, "normalized")  # it equals A's spelling, but B holds the code
        self.assertEqual(self.t6_id(b), lookup.project("abc").entity.t6_id)  # type: ignore[union-attr]

    def test_the_t6_id_selects_the_historical_project_whoever_holds_the_code(self) -> None:
        a = self.show("Show A", "ABC", Rotation.IN)
        self.registry.set_rotation(
            self.t6_id(a), Rotation.OUT, actor=ACTOR, idempotency_key="a-out", expected_revision=1
        )
        self.show("Show B", "ABC", Rotation.IN)
        lookup = self.lookup()

        picked = lookup.project_by_id(self.t6_id(a))
        self.assertEqual((picked.entity.name if picked.entity else None, picked.method), ("Show A", "exact_id"))
        self.assertEqual(picked.entity.project_code, "ABC")  # type: ignore[union-attr]  # its record keeps the code
        by_page = lookup.project_by_id(a.page_id)
        self.assertEqual(by_page.entity.name, "Show A")  # type: ignore[union-attr]
        self.assertIsNone(lookup.project_by_id("t6p_unknown").entity)
        self.assertEqual(lookup.project_by_id("  ").method, "none")

    def test_no_in_rotation_holder_needs_review_with_the_historical_candidates(self) -> None:
        self.show("Show A", "ABC", Rotation.OUT)
        self.show("Show B", "ABC", Rotation.OUT)
        self.show("Show C", "XYZ", Rotation.IN)
        found = self.lookup().project("abc")
        self.assertEqual((found.entity, found.needs_review, found.reason), (None, True, "no_in_rotation_holder"))
        self.assertEqual({c.entity.name for c in found.candidates}, {"Show A", "Show B"})

    def test_an_unclassified_project_with_the_code_is_never_resolved_around(self) -> None:
        self.show("Show A", "ABC", Rotation.UNCLASSIFIED)
        self.show("Show B", "XYZ", Rotation.IN)
        found = self.lookup().project("ABC")
        self.assertEqual((found.entity, found.needs_review, found.reason), (None, True, "unclassified"))
        self.assertEqual([c.entity.name for c in found.candidates], ["Show A"])

    def test_several_in_rotation_holders_are_an_integrity_problem_not_a_pick(self) -> None:
        one = Project(page_id="p1", name="One", t6_id="t6p_1", rotation="in", project_code="ABC")
        two = Project(page_id="p2", name="Two", t6_id="t6p_2", rotation="in", project_code="abc")
        old = Project(page_id="p3", name="Old", t6_id="t6p_3", rotation="out", project_code="ABC")
        found = Lookup(Index(projects=(one, two, old))).project("ABC")
        self.assertEqual((found.entity, found.reason), (None, "several_in_rotation_holders"))
        self.assertEqual({c.entity.name for c in found.candidates}, {"One", "Two"})  # the holders, not the history

    def test_projects_that_are_not_registered_yet_behave_as_before(self) -> None:
        a = Project(page_id="p1", name="A", project_code="ABC")
        b = Project(page_id="p2", name="B", project_code="ABC")
        tie = Lookup(Index(projects=(a, b))).project("ABC")
        self.assertEqual((tie.entity, tie.needs_review, tie.reason), (None, True, None))
        self.assertEqual(Lookup(Index(projects=(a,))).project("abc").entity, a)

    def test_a_blank_code_is_never_a_match_and_blank_lookups_still_need_review(self) -> None:
        self.show("No Code", None, Rotation.IN)
        self.show("Show B", "ABC", Rotation.IN)
        lookup = self.lookup()
        self.assertEqual(lookup.project("ABC").entity.name, "Show B")  # type: ignore[union-attr]
        blank = lookup.project(None)
        self.assertEqual([c.entity.name for c in blank.candidates], ["No Code"])
        self.assertTrue(blank.needs_review)

    def test_a_projects_relationships_stay_with_it_when_its_code_is_reused(self) -> None:
        a = self.show("Show A", "ABC", Rotation.IN)
        self.registry.set_rotation(
            self.t6_id(a), Rotation.OUT, actor=ACTOR, idempotency_key="a-out", expected_revision=1
        )
        b = self.show("Show B", "ABC", Rotation.IN)
        master = Deliverable(page_id="d1", name="Final Master", project_id=a.page_id)
        lookup = Lookup(Index(deliverables=(master,)), same_project=ProjectRefs(self.registry).same)
        self.assertEqual(lookup.deliverable("Final Master", project_id=self.t6_id(a)).entity, master)
        self.assertIsNone(lookup.deliverable("Final Master", project_id=self.t6_id(b)).entity)
        self.assertIsNone(lookup.deliverable("Final Master", project_id=b.page_id).entity)


class SchemaReportTests(WorldTestCase):
    def test_blank_codes_and_valid_historical_reuse_pass(self) -> None:
        self.show("No Code", None, Rotation.IN)
        self.show("Also none", None, Rotation.OUT)
        a = self.show("Show A", "ABC", Rotation.IN)
        self.registry.set_rotation(
            self.t6_id(a), Rotation.OUT, actor=ACTOR, idempotency_key="a-out", expected_revision=1
        )
        self.show("Show B", "abc", Rotation.IN)
        self.show("Show C", "A-B-C", Rotation.OUT)  # a third record of the same code, kept out
        report = self.report()
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["project_codes"]["status"], "ok")
        self.assertEqual(report["project_codes"]["checked"], 5)
        self.assertEqual({kind: found for kind, found in self.findings(report).items() if found}, {})

    def test_each_problem_is_reported_under_its_own_name(self) -> None:
        self.show("Fine", "FIN", Rotation.IN)
        self.show("Waiting", "NEW", Rotation.UNCLASSIFIED)
        unbound = self.show("Copied page", "COP", None)
        gone = self.show("Deleted", "DEL", Rotation.OUT)
        self.registry.observe_page(gone.page_id, actor=ACTOR, available=False)
        missing = self.registry.register(
            actor=ACTOR, idempotency_key="missing", notion_page_id="f" * 32, code="MIS", rotation=Rotation.OUT
        )
        edited = self.show("Edited", "EDT", Rotation.IN)
        self.registry.observe_page(
            edited.page_id, actor=ACTOR, available=True, shown=Shown(self.t6_id(edited), "HACK", "in")
        )

        report = self.report()
        self.assertFalse(report["ok"])
        self.assertEqual(report["project_codes"]["status"], "findings")
        found = self.findings(report)
        self.assertEqual(set(found), set(KINDS))
        self.assertEqual([f["page_id"] for f in found["unbound_pages"]], [unbound.page_id.replace("-", "")])
        self.assertEqual([f["t6_id"] for f in found["unavailable_bindings"]], [self.t6_id(gone)])
        self.assertEqual([f["t6_id"] for f in found["missing_pages"]], [missing.project.id])  # type: ignore[union-attr]
        self.assertEqual([f["code"] for f in found["unclassified"]], ["NEW"])
        (conflict,) = found["projection_conflicts"]
        self.assertEqual((conflict["t6_id"], conflict["detail"]["shown"]["code"]), (self.t6_id(edited), "HACK"))
        self.assertEqual(found["active_collisions"], [])  # nothing else leaks into it
        self.assertEqual({kind: len(items) for kind, items in found.items() if items}, {
            "unclassified": 1, "unbound_pages": 1, "unavailable_bindings": 1, "missing_pages": 1, "projection_conflicts": 1
        })

    def test_an_active_collision_is_reported_with_both_projects(self) -> None:
        self.show("Show A", "ABC", Rotation.IN)
        self.registry.twin = True  # the file now shows a second in-rotation Project with that code
        found = self.findings(self.report())
        (collision,) = found["active_collisions"]
        self.assertEqual((collision["code"], len(collision["t6_ids"])), ("ABC", 2))

    def test_a_replaced_page_that_notion_no_longer_returns_is_not_a_finding(self) -> None:
        old_page = "a" * 32  # the page Notion no longer returns
        made = self.registry.register(
            actor=ACTOR, idempotency_key="old", notion_page_id=old_page, code="ABC", rotation=Rotation.IN
        )
        assert hasattr(made, "project")
        replacement = str(self.notion.add_page(PROJECTS, project_properties(name="New page", project_code="ABC"))["id"])
        self.registry.bind_page(
            made.project.id, replacement, actor=ACTOR, idempotency_key="swap", expected_revision=1
        )
        self.registry.report_sync(made.project.id, 2, ok=True)
        report = self.report()
        self.assertTrue(report["ok"], report)  # the old page is a legacy binding: gone is fine, and it is not unbound


class IncompleteCheckTests(WorldTestCase):
    def test_no_registry_is_incomplete_never_clean(self) -> None:
        self.show("Show A", "ABC", Rotation.IN)
        with self.notion.client() as client:
            report = dict(check_schema(client, load_snapshot(), {"projects": PROJECTS}, None))
        self.assertFalse(report["ok"])
        codes = report["project_codes"]
        self.assertEqual((codes["status"], codes["findings"], codes["checked"]), ("incomplete", None, 1))
        self.assertEqual(codes["reasons"][0]["source"], "registry")

    def test_an_unavailable_registry_is_incomplete(self) -> None:
        self.show("Show A", "ABC", Rotation.IN)
        report = self.report(Down(self.path))
        self.assertFalse(report["ok"])
        self.assertEqual(report["project_codes"]["status"], "incomplete")
        self.assertIn("locked", report["project_codes"]["reasons"][0]["message"])

    def test_unavailable_notion_is_incomplete_and_still_lists_what_the_registry_knows(self) -> None:
        self.show("Waiting", "NEW", Rotation.UNCLASSIFIED)
        self.notion.fail_next(500, code="internal_server_error", after=1)  # the schema read passes, the page read fails
        report = self.report()
        self.assertFalse(report["ok"])
        codes = report["project_codes"]
        self.assertEqual((codes["status"], codes["checked"]), ("incomplete", None))
        self.assertEqual(codes["reasons"][0]["source"], "notion")
        self.assertEqual(len(codes["findings"]["unclassified"]), 1)
        self.assertEqual(codes["findings"]["unbound_pages"], [])  # not worked out: no pages were read

    def test_a_check_that_does_not_include_projects_is_unaffected(self) -> None:
        fake = FakeNotion()
        fake.add_snapshot_data_source("ds-bays", "bays")
        with fake.client() as client:
            report = dict(check_schema(client, load_snapshot(), {"bays": "ds-bays"}))
        self.assertTrue(report["ok"])
        self.assertIsNone(report["project_codes"])


class CommandTests(WorldTestCase):
    def run_command(self, *extra: str) -> tuple[int, Any, str]:
        sources = Path(self.directory.name) / "sources.json"
        sources.write_text(json.dumps({"projects": PROJECTS}))
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.dict(os.environ, {"T6_NOTION_TOKEN": "token"}, clear=True),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            status = main(["schema-check", "--data-sources", str(sources), *extra], open_notion=lambda _t: self.notion.client())
        text = out.getvalue()
        return status, json.loads(text) if text else None, err.getvalue()

    def test_a_clean_registry_exits_zero(self) -> None:
        self.show("Show A", "ABC", Rotation.IN)
        self.show("No Code", None, Rotation.OUT)
        status, report, _ = self.run_command("--registry-db", str(self.path))
        self.assertEqual((status, report["project_codes"]["status"]), (0, "ok"))

    def test_findings_exit_one_with_the_kinds_in_the_report(self) -> None:
        self.show("Waiting", "NEW", Rotation.UNCLASSIFIED)
        status, report, _ = self.run_command("--registry-db", str(self.path))
        self.assertEqual((status, report["project_codes"]["status"]), (1, "findings"))
        self.assertEqual(len(report["project_codes"]["findings"]["unclassified"]), 1)

    def test_a_missing_registry_file_is_incomplete_exit_one_and_is_not_created(self) -> None:
        self.show("Show A", "ABC", Rotation.IN)
        missing = Path(self.directory.name) / "nowhere.sqlite3"
        status, report, _ = self.run_command("--registry-db", str(missing))
        self.assertEqual((status, report["project_codes"]["status"]), (1, "incomplete"))
        self.assertFalse(missing.exists())

    def test_the_registry_file_comes_from_the_environment_by_default(self) -> None:
        self.show("Show A", "ABC", Rotation.IN)
        sources = Path(self.directory.name) / "sources.json"
        sources.write_text(json.dumps({"projects": PROJECTS}))
        out = io.StringIO()
        with (
            mock.patch.dict(os.environ, {"T6_NOTION_TOKEN": "token", "T6_REGISTRY_DB": str(self.path)}, clear=True),
            redirect_stdout(out),
        ):
            status = main(["schema-check", "--data-sources", str(sources)], open_notion=lambda _t: self.notion.client())
        self.assertEqual((status, json.loads(out.getvalue())["project_codes"]["status"]), (0, "ok"))


if __name__ == "__main__":
    unittest.main()
