"""Resolve project server reader, tested against a schema fixture per Resolve version."""

from pathlib import Path
from dataclasses import replace
import sqlite3
import unittest

from t6.domain import ExternalRef
from t6.notion.external_links import ExternalLinks
from t6.notion.testing import FakeNotion, external_link_properties
from t6.resolve import (
    SCHEMAS,
    ProjectServer,
    ReadOnlyViolation,
    ResolveProject,
    map_projects,
    schema_for,
)
from t6.resolve.testing import ServerFixture

FIXTURES = Path(__file__).parent / "fixtures" / "resolve"
LINKS = "ds-links"


def load(version: str) -> ServerFixture:
    return ServerFixture((FIXTURES / f"v{version}.sql").read_text())


class PerVersionTests(unittest.TestCase):
    def test_unverified_schema_is_rejected_by_default(self) -> None:
        server = load("19")
        self.addCleanup(server.close)
        db = server.readonly()
        self.addCleanup(db.close)
        with self.assertRaisesRegex(ValueError, "unverified"):
            ProjectServer(db, "db1", schema_for("19"))

    def test_verified_schema_can_be_used_without_opt_in(self) -> None:
        server = load("19")
        self.addCleanup(server.close)
        db = server.readonly()
        self.addCleanup(db.close)
        schema = replace(schema_for("19"), verified=True)
        self.assertEqual(len(ProjectServer(db, "db1", schema).projects()), 4)

    def test_project_server_app_version_20_has_no_assumed_layout(self) -> None:
        with self.assertRaises(KeyError):
            schema_for("20")

    def test_every_supported_version_has_a_fixture_that_passes(self) -> None:
        for version in SCHEMAS:
            with self.subTest(version=version):
                self.assertTrue((FIXTURES / f"v{version}.sql").exists(), "fixture missing")
                server = load(version)
                self.addCleanup(server.close)
                db = server.readonly()
                self.addCleanup(db.close)
                projects = ProjectServer(db, "db1", schema_for(version), allow_unverified_schema=True).projects()
                self.assertEqual(
                    [(p.external_id, p.internal_id) for p in projects],
                    [
                        ("db1/Sizzle", "p-3"),
                        ("db1/Series A/Ep201 Grade", "p-4"),
                        ("db1/Series A/Season 1/Ep101 Grade", "p-1"),
                        ("db1/Series A/Season 1/Ep102 Grade", "p-2"),
                    ],
                )

    def test_unknown_version_names_the_known_ones(self) -> None:
        with self.assertRaisesRegex(KeyError, "known: 18, 19"):
            schema_for("99")


class InventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = load("19")
        self.addCleanup(self.server.close)

    def test_inventory_lists_database_folder_path_name_and_internal_id(self) -> None:
        db = self.server.readonly()
        self.addCleanup(db.close)
        project = next(
            p for p in ProjectServer(db, "db1", schema_for("19"), allow_unverified_schema=True).projects() if p.internal_id == "p-1"
        )
        self.assertEqual(
            project,
            ResolveProject(
                database="db1",
                folder_path=("Series A", "Season 1"),
                name="Ep101 Grade",
                internal_id="p-1",
            ),
        )
        self.assertEqual(
            project.ref,
            ExternalRef(
                system="resolve",
                kind="project",
                external_id="db1/Series A/Season 1/Ep101 Grade",
                internal_id="p-1",
            ),
        )

    def test_a_writable_role_is_refused(self) -> None:
        db = self.server.writable()
        self.addCleanup(db.close)
        with self.assertRaises(ReadOnlyViolation):
            ProjectServer(db, "db1", schema_for("19"), allow_unverified_schema=True)

    def test_the_read_only_connection_cannot_write(self) -> None:
        db = self.server.readonly()
        self.addCleanup(db.close)
        with self.assertRaises(sqlite3.OperationalError):
            db.execute('DELETE FROM "Sm2Project"')
        self.assertEqual(len(ProjectServer(db, "db1", schema_for("19"), allow_unverified_schema=True).projects()), 4)


class MappingTests(unittest.TestCase):
    def test_projects_map_through_external_links_and_unmapped_are_reported(self) -> None:
        server = load("19")
        self.addCleanup(server.close)
        db = server.readonly()
        self.addCleanup(db.close)
        projects = ProjectServer(db, "db1", schema_for("19"), allow_unverified_schema=True).projects()

        fake = FakeNotion()
        fake.add_data_source(LINKS)
        for external_id, internal_id, notion_projects in (
            ("db1/Series A/Season 1/Ep101 Grade", "p-1", ["proj-a"]),
            ("db1/Series A/Season 1/Ep102 Grade", "p-2", []),  # row, but no Notion Project
            ("db1/Gone/Old Show", "p-9", ["proj-z"]),  # not on the server any more
        ):
            fake.add_page(
                LINKS,
                external_link_properties(
                    system="resolve",
                    kind="project",
                    external_id=external_id,
                    internal_id=internal_id,
                    projects=notion_projects,
                ),
            )
        fake.add_page(  # another system's row must not be mistaken for a Resolve one
            LINKS,
            external_link_properties(
                system="frameio", kind="project", external_id="db1/Sizzle", projects=["proj-f"]
            ),
        )
        with fake.client() as client:
            report = map_projects(projects, ExternalLinks(client, LINKS))

        self.assertEqual([(p.internal_id, l.project_ids) for p, l in report.mapped], [("p-1", ("proj-a",))])
        self.assertEqual(sorted(p.internal_id for p in report.unmapped), ["p-2", "p-3", "p-4"])
        self.assertEqual([l.ref.external_id for l in report.orphan_links], ["db1/Gone/Old Show"])


if __name__ == "__main__":
    unittest.main()
