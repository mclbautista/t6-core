"""Projects, Contacts and Deliverables read as domain entities from the fake Notion."""

import datetime as dt
import unittest

from t6.notion.generated import ContactsRow, DeliverablesRow, ProjectsRow
from t6.notion.reads import ContactReader, DeliverableReader, ProjectReader, parse_end_of_shift
from t6.notion.testing import FakeNotion, date, number, relation, rich_text, select, title

PROJECTS, CONTACTS, DELIVERABLES = "ds-projects", "ds-contacts", "ds-deliverables"
ANA, BEN = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


def fake() -> FakeNotion:
    notion = FakeNotion()
    notion.add_snapshot_data_source(PROJECTS, "projects")
    notion.add_snapshot_data_source(CONTACTS, "contacts")
    notion.add_snapshot_data_source(DELIVERABLES, "deliverables")
    return notion


class EndOfShiftTests(unittest.TestCase):
    def test_parses_times_and_treats_flexible_and_blank_as_none(self) -> None:
        for text, expected in [
            ("10:00 PM", dt.time(22, 0)),
            ("6:30 am", dt.time(6, 30)),
            ("12:00 AM", dt.time(0, 0)),
            ("12:00 PM", dt.time(12, 0)),
            ("10 PM", dt.time(22, 0)),
            ("22:15", dt.time(22, 15)),
            ("Flexible / Variable", None),
            ("Variable", None),
            ("", None),
            (None, None),
            ("13:00 PM", None),
        ]:
            with self.subTest(text=text):
                self.assertEqual(parse_end_of_shift(text), expected)


class ReadTests(unittest.TestCase):
    def test_projects_read_with_every_field(self) -> None:
        notion = fake()
        p = ProjectsRow
        page = notion.add_page(
            PROJECTS,
            {
                p.notion_name("name"): title("Show A"),
                p.notion_name("project_code"): select("ABC"),
                p.notion_name("eps"): number(8),
                p.notion_name("status"): select("In Progress"),
                p.notion_name("stage"): select("Edit"),
                p.notion_name("health"): select("On Track"),
                p.notion_name("playdate"): date("2026-12-01"),
                p.notion_name("submission_deadline"): date("2026-11-20"),
                p.notion_name("archival_deadline"): date("2027-01-15"),
                p.notion_name("pm_notes"): rich_text("careful"),
                p.notion_name("has_post_supe"): select("No Post Supe"),
                p.notion_name("editors"): relation(ANA, BEN),
                p.notion_name("t6_post_coordinator"): relation(BEN),
                p.notion_name("colorist"): relation(ANA),
                p.notion_name("studio"): relation("cccccccc-cccc-cccc-cccc-cccccccccccc"),
            },
        )
        with notion.client() as client:
            reader = ProjectReader(client, PROJECTS)
            project = reader.get(str(page["id"]))
            self.assertEqual([x.page_id for x in reader.list()], [str(page["id"])])
        self.assertEqual((project.name, project.project_code, project.eps, project.status), ("Show A", "ABC", 8, "In Progress"))
        self.assertEqual((project.stage, project.health), ("Edit", "On Track"))
        self.assertEqual(project.playdate, dt.date(2026, 12, 1))
        self.assertEqual(project.submission_deadline, dt.date(2026, 11, 20))
        self.assertEqual(project.archival_deadline, dt.date(2027, 1, 15))
        self.assertEqual((project.pm_notes, project.has_post_supe), ("careful", "No Post Supe"))
        self.assertEqual(project.editor_ids, (ANA, BEN))
        self.assertEqual((project.post_coordinator_ids, project.colorist_ids), ((BEN,), (ANA,)))
        self.assertEqual(project.studio_id, "cccccccc-cccc-cccc-cccc-cccccccccccc")
        self.assertEqual(project.director_ids, ())

    def test_contacts_read_handle_telegram_id_aliases_and_shift(self) -> None:
        notion = fake()
        c = ContactsRow
        page = notion.add_page(
            CONTACTS,
            {
                c.notion_name("name"): title("Ana Cruz"),
                c.notion_name("telegram_handle"): rich_text("@ana"),
                c.notion_name("telegram_user_id"): rich_text("12345"),
                c.notion_name("aliases"): rich_text("Ate Ana, Anaki\nAC"),
                c.notion_name("end_of_shift"): select("10:00 PM"),
            },
        )
        flexible = notion.add_page(
            CONTACTS, {c.notion_name("name"): title("Ben"), c.notion_name("end_of_shift"): select("Flexible / Variable")}
        )
        with notion.client() as client:
            reader = ContactReader(client, CONTACTS)
            ana = reader.get(str(page["id"]))
            ben = reader.get(str(flexible["id"]))
            self.assertEqual(len(reader.list()), 2)
        self.assertEqual((ana.telegram_handle, ana.telegram_user_id), ("@ana", 12345))
        self.assertEqual(ana.aliases, ("Ate Ana", "Anaki", "AC"))
        self.assertEqual(ana.end_of_shift, dt.time(22, 0))
        self.assertEqual((ben.end_of_shift, ben.telegram_user_id, ben.aliases), (None, None, ()))

    def test_deliverables_for_a_project(self) -> None:
        notion = fake()
        d = DeliverablesRow
        mine = notion.add_page(
            DELIVERABLES,
            {
                d.notion_name("name"): title("Trailer"),
                d.notion_name("projects"): relation(ANA),
                d.notion_name("deliverable_class"): select("TLR"),
                d.notion_name("playdate"): date("2026-12-25"),
                d.notion_name("deadline"): date("2026-12-01"),
                d.notion_name("status"): select("Pending"),
            },
        )
        notion.add_page(DELIVERABLES, {d.notion_name("name"): title("Other"), d.notion_name("projects"): relation(BEN)})
        with notion.client() as client:
            found = DeliverableReader(client, DELIVERABLES).for_project(ANA)
        self.assertEqual([x.page_id for x in found], [str(mine["id"])])
        only = found[0]
        self.assertEqual((only.deliverable_class, only.playdate, only.due), ("TLR", dt.date(2026, 12, 25), dt.date(2026, 12, 1)))
        self.assertEqual((only.project_id, only.status), (ANA, "Pending"))


if __name__ == "__main__":
    unittest.main()
