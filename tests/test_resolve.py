"""Resolve project server reader, tested against a schema fixture per Resolve version."""

from pathlib import Path
from dataclasses import replace
import sqlite3
import unittest

from t6.domain import ExternalRef
from t6.media_links import MediaLink
from t6.testing import StaticMediaLinks
from t6.resolve import (
        ProjectServer,
    ReadOnlyViolation,
    ResolveProject,
    map_projects,
    schema_for,
)
from t6.resolve.testing import ServerFixture

FIXTURES = Path(__file__).parent / "fixtures" / "resolve"


def load() -> ServerFixture:
    return ServerFixture((FIXTURES / "server.sql").read_text())


def pid(n: int) -> str:
    return f"00000000-0000-0000-0000-{n:012d}"


class SchemaTests(unittest.TestCase):
    def test_the_verified_layout_can_be_used_without_opt_in(self) -> None:
        server = load()
        self.addCleanup(server.close)
        db = server.readonly()
        self.addCleanup(db.close)
        self.assertEqual(len(ProjectServer(db, "db1", schema_for("18.5")).projects()), 5)

    def test_schema_matches_on_the_major_version(self) -> None:
        for version in ("18.1.4", "17.4.6", "20", "19"):
            with self.subTest(version=version):
                self.assertTrue(schema_for(version).verified)
                self.assertEqual(schema_for(version).project_table, "SM_Project")

    def test_unknown_major_names_the_known_ones(self) -> None:
        for version in ("16", "99"):
            with self.subTest(version=version), self.assertRaisesRegex(KeyError, "known: 17, 18, 19, 20"):
                schema_for(version)

    def test_only_the_project_and_folder_tables_are_queried(self) -> None:
        queries: list[str] = []

        class Spy:
            def can_write(self) -> bool:
                return False

            def query(self, sql: str) -> list[tuple[str, ...]]:
                queries.append(sql)
                return []

        ProjectServer(Spy(), "db1", schema_for("20")).projects()
        self.assertEqual(len(queries), 2)
        self.assertIn('FROM "SM_ProjectFolder"', queries[0])
        self.assertIn('"Name"', queries[0])
        self.assertIn('"ParentFolder"', queries[0])
        self.assertIn('FROM "SM_Project"', queries[1])
        self.assertIn('"Folder"', queries[1])


class InventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = load()
        self.addCleanup(self.server.close)

    def test_inventory_lists_database_folder_path_name_and_internal_id(self) -> None:
        db = self.server.readonly()
        self.addCleanup(db.close)
        project = next(
            p for p in ProjectServer(db, "db1", schema_for("19")).projects() if p.internal_id == pid(1)
        )
        self.assertEqual(
            project,
            ResolveProject(
                database="db1",
                folder_path=("Alpha", "Gamma"),
                name="Delta",
                internal_id=pid(1),
            ),
        )
        self.assertEqual(
            project.ref,
            ExternalRef(
                system="resolve",
                kind="project",
                external_id="db1/Alpha/Gamma/Delta",
                internal_id=pid(1),
            ),
        )

    def test_folder_paths_include_every_named_folder_and_null_folder_is_top_level(self) -> None:
        db = self.server.readonly()
        self.addCleanup(db.close)
        projects = ProjectServer(db, "db1", schema_for("19")).projects()
        self.assertEqual(
            [(p.folder_path, p.name, p.internal_id) for p in projects],
            [
                ((), "Zeta", pid(3)),
                (("Alpha",), "Delta", pid(4)),
                (("Alpha", "Gamma"), "Delta", pid(1)),
                (("Alpha", "Gamma"), "Epsilon", pid(2)),
                (("Beta",), "Delta", pid(5)),
            ],
        )

    def test_same_name_in_different_folders_gets_different_external_ids(self) -> None:
        db = self.server.readonly()
        self.addCleanup(db.close)
        projects = ProjectServer(db, "db1", schema_for("19")).projects()
        ids = [p.external_id for p in projects if p.name == "Delta"]
        self.assertEqual(len(ids), 3)
        self.assertEqual(len(set(ids)), 3)
        self.assertIn("db1/Zeta", [p.external_id for p in projects])

    def test_a_writable_role_is_refused(self) -> None:
        db = self.server.writable()
        self.addCleanup(db.close)
        with self.assertRaises(ReadOnlyViolation):
            ProjectServer(db, "db1", schema_for("19"))

    def test_the_read_only_connection_cannot_write(self) -> None:
        db = self.server.readonly()
        self.addCleanup(db.close)
        with self.assertRaises(sqlite3.OperationalError):
            db.execute('DELETE FROM "SM_Project"')
        self.assertEqual(len(ProjectServer(db, "db1", schema_for("19")).projects()), 5)


class MappingTests(unittest.TestCase):
    def test_projects_map_through_media_locations_and_unmapped_are_reported(self) -> None:
        server = load()
        self.addCleanup(server.close)
        db = server.readonly()
        self.addCleanup(db.close)
        projects = ProjectServer(db, "db1", schema_for("19")).projects()

        links = StaticMediaLinks()
        for number, (ref, pages) in enumerate((
            ("db1/Alpha/Gamma/Delta", ("proj-a",)),
            ("db1/Gone/Old", ("proj-z",)),  # not on the server any more
        )):
            links.add(MediaLink(id=f"t6m_{number}", owner=f"t6p_{number}", project_id=f"t6p_{number}",
                                kind="resolve-project", ref=ref, role="working", pages=pages))
        # Epsilon has no Media location at all, so it is unmapped.
        links.add(MediaLink(  # another kind must not be mistaken for a Resolve project
            id="t6m_f", owner="t6p_f", project_id="t6p_f", kind="frameio-project",
            ref="db1/Zeta", role="frameio-project", pages=("proj-f",)))
        links.add(MediaLink(  # a Resolve project kept as a master copy is not the working link
            id="t6m_m", owner="t6p_m", project_id="t6p_m", kind="resolve-project",
            ref="db1/Alpha/Gamma/Epsilon", role="master", pages=("proj-m",)))
        report = map_projects(projects, links)

        self.assertEqual([(p.internal_id, l.project_id, l.pages) for p, l in report.mapped], [(pid(1), "t6p_0", ("proj-a",))])
        self.assertEqual(sorted(p.internal_id for p in report.unmapped), [pid(2), pid(3), pid(4), pid(5)])
        self.assertEqual([l.ref for l in report.orphan_links], ["db1/Gone/Old"])
        self.assertEqual(links.calls, [("resolve-project", "working")])


if __name__ == "__main__":
    unittest.main()
