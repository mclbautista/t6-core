"""The bay scanner's reader (#377) on synthetic disk libraries."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bay_scan_fixtures import library, project, snapshot
from t6 import bay_scanner


class ReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.root = library(self.home)

    def test_lists_every_project_with_folder_name_time_and_timelines(self) -> None:
        project(self.root, (), "Top Level", "uuid-1", 1_700_000_000, 2)
        project(self.root, ("Group", "Nested"), "Deep One", "uuid-2", 1_700_000_500, 0)
        copies, errors = bay_scanner.scan_library(self.home)
        self.assertEqual(errors, [])
        self.assertEqual(copies, [
            {"project_uuid": "uuid-1", "project_name": "Top Level", "folder_path": "", "last_modified": 1_700_000_000, "timeline_count": 2},
            {"project_uuid": "uuid-2", "project_name": "Deep One", "folder_path": "Group/Nested", "last_modified": 1_700_000_500, "timeline_count": 0},
        ])

    def test_no_library_is_an_empty_scan(self) -> None:
        self.assertEqual(bay_scanner.scan_library(Path(self.tmp.name) / "empty"), ([], []))

    def test_every_library_user_is_read(self) -> None:
        project(self.root, (), "First", "uuid-1", 1, 1)
        project(library(self.home, "user-b"), (), "Second", "uuid-2", 2, 1)
        copies, _ = bay_scanner.scan_library(self.home)
        self.assertEqual([copy["project_name"] for copy in copies], ["First", "Second"])

    def test_a_scan_changes_nothing_in_the_library_and_leaves_no_copy(self) -> None:
        project(self.root, ("Group",), "One", "uuid-1", 10, 3)
        project(self.root, (), "Two", "uuid-2", 20, 1)
        (self.root / "Two" / "Notes.txt").write_text("keep")
        before_home = snapshot(self.home)
        scratch = Path(self.tmp.name) / "scratch"
        scratch.mkdir()
        made: list[str] = []
        real = tempfile.mkdtemp

        def mkdtemp(prefix: str) -> str:
            made.append(real(prefix=prefix, dir=scratch))
            return made[-1]

        with patch("tempfile.mkdtemp", mkdtemp):
            copies, _ = bay_scanner.scan_library(self.home)
        self.assertEqual(len(copies), 2)
        self.assertEqual(len(made), 1)
        self.assertFalse(Path(made[0]).exists())
        self.assertEqual(snapshot(self.home), before_home)

    def test_a_corrupt_or_unknown_database_is_reported_and_the_rest_still_scan(self) -> None:
        project(self.root, (), "Good", "uuid-1", 5, 1)
        broken = self.root / "Broken" / "Project.db"
        broken.parent.mkdir()
        broken.write_bytes(b"this is not a sqlite file at all" * 20)
        other = self.root / "Group" / "Other Layout" / "Project.db"
        other.parent.mkdir(parents=True)
        import sqlite3
        connection = sqlite3.connect(other)
        connection.execute("CREATE TABLE Unrelated (x)")
        connection.commit()
        connection.close()
        copies, errors = bay_scanner.scan_library(self.home)
        self.assertEqual([copy["project_name"] for copy in copies], ["Good"])
        self.assertEqual([(error["folder_path"], error["project"]) for error in errors], [("", "Broken"), ("Group", "Other Layout")])
        self.assertTrue(all(error["reason"] for error in errors))
        self.assertIn("unknown database layout", errors[1]["reason"])

    def test_a_project_name_with_odd_characters_opens(self) -> None:
        project(self.root, ("A B#1",), "Name ?x&y", "uuid-1", 5, 1)
        copies, errors = bay_scanner.scan_library(self.home)
        self.assertEqual((len(copies), errors), (1, []))


if __name__ == "__main__":
    unittest.main()
