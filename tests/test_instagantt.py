"""Exercise the Instagantt export reader and its linking through Media locations."""

import unittest
from datetime import date
from pathlib import Path

from t6.instagantt import InstaganttError, Task, link_timelines, parse_export, read_export
from t6.media_links import MediaLink
from t6.testing import StaticMediaLinks

FIXTURE = Path(__file__).parent / "fixtures" / "instagantt_export.csv"


def add_link(links: StaticMediaLinks, ref: str, project: str, role: str = "working") -> None:
    links.add(MediaLink(id=f"t6m_{len(links.all())}", owner=project, project_id=project, kind="instagantt-project",
                        ref=ref, role=role, pages=()))


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
    def test_projects_resolve_through_media_locations_and_unmapped_are_reported(self) -> None:
        links = StaticMediaLinks()
        add_link(links, "ig-alpha", "t6p_alpha")
        links.add(MediaLink(id="t6m_x", owner="t6p_x", project_id="t6p_x", kind="resolve-project",
                            ref="ig-beta", role="working", pages=()))  # another kind is not an Instagantt link
        # ig-gamma has no Media location at all
        report = link_timelines(read_export(FIXTURE), links)
        self.assertEqual([(l.timeline.project, l.project_id) for l in report.linked], [("Show Alpha", "t6p_alpha")])
        self.assertEqual(
            [(u.timeline.project, u.reason) for u in report.unmapped],
            [("Show Beta", "no Media location"), ("Show Gamma", "no Media location")],
        )

    def test_a_t6_timeline_location_is_not_an_instagantt_project(self) -> None:
        links = StaticMediaLinks()
        links.add(MediaLink(id="t6m_t", owner="t6p_t", project_id="t6p_t", kind="timeline",
                            ref="ig-alpha", role="working", pages=()))
        report = link_timelines(read_export(FIXTURE)[:1], links)
        self.assertEqual(report.linked, ())
        self.assertEqual([u.reason for u in report.unmapped], ["no Media location"])

    def test_a_timeline_in_several_projects_is_not_guessed(self) -> None:
        links = StaticMediaLinks()
        add_link(links, "ig-alpha", "t6p_1")
        add_link(links, "ig-alpha", "t6p_2", role="backup")
        report = link_timelines(read_export(FIXTURE)[:1], links)
        self.assertEqual(report.linked, ())
        self.assertIn("several", report.unmapped[0].reason)

    def test_one_project_with_two_locations_for_a_timeline_is_one_link(self) -> None:
        links = StaticMediaLinks()
        add_link(links, "ig-alpha", "t6p_1")
        add_link(links, "ig-alpha", "t6p_1", role="backup")
        report = link_timelines(read_export(FIXTURE)[:1], links)
        self.assertEqual([l.project_id for l in report.linked], ["t6p_1"])

    def test_only_active_timeline_locations_are_asked_for(self) -> None:
        links = StaticMediaLinks()
        add_link(links, "ig-alpha", "t6p_1")
        link_timelines(read_export(FIXTURE), links)
        self.assertEqual(links.calls, [("instagantt-project", None)])


if __name__ == "__main__":
    unittest.main()
