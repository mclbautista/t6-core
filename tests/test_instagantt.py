"""Exercise the Instagantt export reader and its External Links linking."""

import unittest
from datetime import date
from pathlib import Path

from t6.instagantt import InstaganttError, Task, link_timelines, parse_export, read_export
from t6.notion.external_links import ExternalLinks
from t6.notion.testing import FakeNotion, external_link_properties

FIXTURE = Path(__file__).parent / "fixtures" / "instagantt_export.csv"
LINKS = "ds-external-links"


def add_link(fake: FakeNotion, external_id: str, projects: list[str]) -> None:
    fake.add_page(
        LINKS,
        external_link_properties(
            system="instagantt", kind="project", external_id=external_id, projects=projects
        ),
    )


class ParseTests(unittest.TestCase):
    def test_export_parses_into_tasks_per_project(self) -> None:
        timelines = read_export(FIXTURE)
        self.assertEqual([t.project for t in timelines], ["Show Alpha", "Show Beta", "Show Gamma"])
        alpha = timelines[0]
        self.assertEqual(alpha.external_id, "ig-alpha")
        self.assertEqual(
            alpha.tasks,
            (
                Task("Offline edit", date(2026, 1, 5), date(2026, 2, 13)),
                Task("Color", date(2026, 2, 16), date(2026, 3, 6)),
                Task("Delivery", date(2026, 3, 13), date(2026, 3, 13)),
            ),
        )
        self.assertEqual(timelines[1].tasks[0].start, date(2026, 4, 6))

    def test_without_a_project_id_the_name_is_the_external_id(self) -> None:
        (timeline,) = parse_export("Project,Task,Start,End\nShow,Cut,2026-01-01,2026-01-02\n")
        self.assertEqual(timeline.external_id, "Show")

    def test_bad_exports_name_the_line(self) -> None:
        cases = {
            "": "empty",
            "Project,Task\nA,B\n": "start",
            "Project,Task,Start\nA,B,soon\n": "line 2",
            "Project,Task,Start\n,B,2026-01-01\n": "no project",
            "Project,Task,Start,End\nA,B,2026-02-01,2026-01-01\n": "before it starts",
        }
        for text, expected in cases.items():
            with self.subTest(text=text), self.assertRaises(InstaganttError) as ctx:
                parse_export(text)
            self.assertIn(expected, str(ctx.exception))


class LinkTests(unittest.TestCase):
    def test_projects_resolve_through_external_links_and_unmapped_are_reported(self) -> None:
        fake = FakeNotion()
        fake.add_data_source(LINKS)
        add_link(fake, "ig-alpha", ["notion-alpha"])
        add_link(fake, "ig-beta", [])
        # ig-gamma has no row at all
        with fake.client() as client:
            report = link_timelines(read_export(FIXTURE), ExternalLinks(client, LINKS))
        self.assertEqual([(l.timeline.project, l.project_id) for l in report.linked], [("Show Alpha", "notion-alpha")])
        self.assertEqual(
            [(u.timeline.project, u.reason) for u in report.unmapped],
            [
                ("Show Beta", "External Links row has no Project"),
                ("Show Gamma", "no External Links row"),
            ],
        )

    def test_a_row_with_several_projects_is_not_guessed(self) -> None:
        fake = FakeNotion()
        fake.add_data_source(LINKS)
        add_link(fake, "ig-alpha", ["p1", "p2"])
        with fake.client() as client:
            report = link_timelines(read_export(FIXTURE)[:1], ExternalLinks(client, LINKS))
        self.assertEqual(report.linked, ())
        self.assertIn("several", report.unmapped[0].reason)

    def test_only_the_external_links_source_is_queried(self) -> None:
        fake = FakeNotion()
        fake.add_data_source(LINKS)
        add_link(fake, "ig-alpha", ["notion-alpha"])
        with fake.client() as client:
            link_timelines(read_export(FIXTURE), ExternalLinks(client, LINKS))
        self.assertTrue(fake.requests)
        for request in fake.requests:
            self.assertIn(LINKS, request.url.path)


if __name__ == "__main__":
    unittest.main()
