"""Projects, Contacts, Studios, Deliverables, Job Orders and NotionSource read against fake Notion."""

import datetime as dt
import unittest
import warnings
from typing import Any
from unittest.mock import patch

from t6.notion import Freshness, NotionSource, as_source
from t6.notion.generated import ContactsRow, DeliverablesRow, JobOrdersRow, ProjectsRow, StudiosRow
from t6.notion.job_orders import JobOrders
from t6.notion.reads import ContactReader, DeliverableReader, ProjectReader, StudioReader, parse_end_of_shift
from t6.notion.sync import NotionClient
from t6.notion.testing import FakeNotion, date, multi_select, number, relation, rich_text, select, title

PROJECTS, CONTACTS, DELIVERABLES, STUDIOS, JOB_ORDERS = (
    "ds-projects",
    "ds-contacts",
    "ds-deliverables",
    "ds-studios",
    "ds-job-orders",
)
ANA, BEN = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


def fake() -> FakeNotion:
    notion = FakeNotion()
    notion.add_snapshot_data_source(PROJECTS, "projects")
    notion.add_snapshot_data_source(CONTACTS, "contacts")
    notion.add_snapshot_data_source(DELIVERABLES, "deliverables")
    notion.add_snapshot_data_source(STUDIOS, "studios")
    notion.add_snapshot_data_source(JOB_ORDERS, "job_orders")
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


class _RecordingNotionSource(NotionSource):
    def __init__(self, target: Any) -> None:
        super().__init__(target)
        self.calls: list[tuple[str, str, Freshness]] = []

    def query_data_source(
        self,
        data_source_id: str,
        *,
        filter: dict[str, Any] | None = None,
        sorts: list[dict[str, Any]] | None = None,
        page_size: int = 100,
        freshness: Freshness = "mirror",
    ) -> list[dict[str, Any]]:
        self.calls.append(("query_data_source", data_source_id, freshness))
        return super().query_data_source(
            data_source_id, filter=filter, sorts=sorts, page_size=page_size, freshness=freshness
        )

    def retrieve_page(self, page_id: str, *, freshness: Freshness = "mirror") -> dict[str, Any]:
        self.calls.append(("retrieve_page", page_id, freshness))
        return super().retrieve_page(page_id, freshness=freshness)

    def retrieve_data_source(self, data_source_id: str, *, freshness: Freshness = "mirror") -> dict[str, Any]:
        self.calls.append(("retrieve_data_source", data_source_id, freshness))
        return super().retrieve_data_source(data_source_id, freshness=freshness)

    def list_block_children(
        self, block_id: str, *, page_size: int = 100, freshness: Freshness = "live"
    ) -> list[dict[str, Any]]:
        self.calls.append(("list_block_children", block_id, freshness))
        return super().list_block_children(block_id, page_size=page_size, freshness=freshness)

    def retrieve_user(self, user_id: str, *, freshness: Freshness = "live") -> dict[str, Any]:
        self.calls.append(("retrieve_user", user_id, freshness))
        return super().retrieve_user(user_id, freshness=freshness)


class NotionSourceTests(unittest.TestCase):
    def test_both_mirror_and_live_reach_fake_notion_and_close_per_call_clients(self) -> None:
        notion = fake()
        notion.add_user("user-1", "Ana Cruz")
        page = notion.add_page(PROJECTS, {ProjectsRow.notion_name("name"): title("Show A")})
        opened: list[NotionClient] = []
        closed = 0

        def open_client() -> NotionClient:
            client = notion.client()
            real_close = client.close

            def tracked_close() -> None:
                nonlocal closed
                closed += 1
                real_close()

            client.close = tracked_close  # type: ignore[method-assign]
            opened.append(client)
            return client

        source = NotionSource(open_client)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            for freshness in ("mirror", "live"):
                mode: Freshness = freshness
                with self.subTest(freshness=mode):
                    rows = source.query_data_source(PROJECTS, freshness=mode)
                    got_page = source.retrieve_page(str(page["id"]), freshness=mode)
                    got_ds = source.retrieve_data_source(PROJECTS, freshness=mode)
                    blocks = source.list_block_children(str(page["id"]), freshness=mode)
                    user = source.retrieve_user("user-1", freshness=mode)
                    self.assertEqual([r["id"] for r in rows], [page["id"]])
                    self.assertEqual(got_page["id"], page["id"])
                    self.assertEqual(got_ds["id"], PROJECTS)
                    self.assertEqual(blocks, [])
                    self.assertEqual(user["name"], "Ana Cruz")
        self.assertEqual(closed, len(opened))
        self.assertEqual(closed, 10)
        self.assertEqual([w for w in caught if issubclass(w.category, ResourceWarning)], [])

    def test_wrapping_an_already_open_client_does_not_close_it_prematurely(self) -> None:
        notion = fake()
        page = notion.add_page(PROJECTS, {ProjectsRow.notion_name("name"): title("Show A")})
        with notion.client() as client:
            source = NotionSource(client)
            self.assertIs(as_source(source), source)
            with patch.object(client, "close", wraps=client.close) as close_spy:
                self.assertEqual(source.retrieve_page(str(page["id"]), freshness="mirror")["id"], page["id"])
                self.assertEqual(as_source(client).query_data_source(PROJECTS, freshness="live")[0]["id"], page["id"])
                with source.open_write_client() as write_client:
                    self.assertIs(write_client, client)
                close_spy.assert_not_called()
            # The caller's client remains usable after NotionSource calls.
            self.assertEqual(client.retrieve_page(str(page["id"]))["id"], page["id"])

    def test_write_passthroughs_and_open_write_client_work_with_factory(self) -> None:
        notion = fake()
        source = NotionSource(notion.client)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            created = source.create_page(
                {
                    "parent": {"type": "data_source_id", "data_source_id": PROJECTS},
                    "properties": {ProjectsRow.notion_name("name"): title("Show B")},
                }
            )
            source.update_page(str(created["id"]), {"properties": {ProjectsRow.notion_name("name"): title("Show B2")}})
            source.append_block_children(
                str(created["id"]),
                [{"type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": "note"}}]}}],
            )
            blocks = source.list_block_children(str(created["id"]))
        self.assertEqual(source.retrieve_page(str(created["id"]), freshness="live")["properties"]["Name"]["title"][0]["plain_text"], "Show B2")
        self.assertEqual(blocks[0]["paragraph"]["rich_text"][0]["text"]["content"], "note")
        self.assertEqual([w for w in caught if issubclass(w.category, ResourceWarning)], [])

    def test_invalid_freshness_raises_value_error(self) -> None:
        notion = fake()
        source = NotionSource(notion.client)
        with self.assertRaises(ValueError):
            source.query_data_source(PROJECTS, freshness="stale")  # type: ignore[arg-type]

    def test_readers_and_job_orders_accept_source_and_forward_freshness(self) -> None:
        notion = fake()
        proj = notion.add_page(PROJECTS, {ProjectsRow.notion_name("name"): title("Show A")})
        contact = notion.add_page(CONTACTS, {ContactsRow.notion_name("name"): title("Ana Cruz")})
        notion.add_page(
            STUDIOS,
            {StudiosRow.notion_name("name"): title("Client Corp"), StudiosRow.notion_name("aliases"): multi_select("CC")},
        )
        notion.add_page(
            DELIVERABLES,
            {DeliverablesRow.notion_name("name"): title("Trailer"), DeliverablesRow.notion_name("projects"): relation(str(proj["id"]))},
        )
        jo = notion.add_page(
            JOB_ORDERS,
            {JobOrdersRow.notion_name("name"): title("JO 1"), JobOrdersRow.notion_name("telegram_key"): rich_text("-100:1")},
        )

        source = _RecordingNotionSource(notion.client)
        projects = ProjectReader(source, PROJECTS)
        contacts = ContactReader(source, CONTACTS)
        studios = StudioReader(source, STUDIOS)
        deliverables = DeliverableReader(source, DELIVERABLES)
        orders = JobOrders(source, JOB_ORDERS)

        self.assertEqual(projects.get(str(proj["id"])).name, "Show A")
        self.assertEqual(projects.get(str(proj["id"]), freshness="live").name, "Show A")
        self.assertEqual(len(projects.list()), 1)
        self.assertEqual(len(projects.list(freshness="live")), 1)

        self.assertEqual(contacts.get(str(contact["id"])).name, "Ana Cruz")
        self.assertEqual(contacts.get(str(contact["id"]), freshness="live").name, "Ana Cruz")
        self.assertEqual(len(contacts.list()), 1)
        self.assertEqual(len(contacts.list(freshness="live")), 1)

        self.assertEqual(studios.names(), ("Client Corp", "CC"))
        self.assertEqual(studios.names(freshness="live"), ("Client Corp", "CC"))

        self.assertEqual(len(deliverables.for_project(str(proj["id"]))), 1)
        self.assertEqual(len(deliverables.for_project(str(proj["id"]), freshness="live")), 1)

        self.assertEqual(orders.get(str(jo["id"])).name, "JO 1")
        self.assertEqual(orders.get(str(jo["id"]), freshness="live").name, "JO 1")
        self.assertIsNotNone(orders.find_by_telegram_key("-100:1"))
        self.assertIsNotNone(orders.find_by_telegram_key("-100:1", freshness="live"))
        self.assertEqual(len(orders.find_all_by_telegram_key("-100:1")), 1)
        self.assertEqual(len(orders.find_all_by_telegram_key("-100:1", freshness="live")), 1)
        self.assertEqual(len(orders.list()), 1)
        self.assertEqual(len(orders.list(freshness="live")), 1)

        expected = [
            ("retrieve_page", str(proj["id"]), "mirror"),
            ("retrieve_page", str(proj["id"]), "live"),
            ("query_data_source", PROJECTS, "mirror"),
            ("query_data_source", PROJECTS, "live"),
            ("retrieve_page", str(contact["id"]), "mirror"),
            ("retrieve_page", str(contact["id"]), "live"),
            ("query_data_source", CONTACTS, "mirror"),
            ("query_data_source", CONTACTS, "live"),
            ("query_data_source", STUDIOS, "mirror"),
            ("query_data_source", STUDIOS, "live"),
            ("query_data_source", DELIVERABLES, "mirror"),
            ("query_data_source", DELIVERABLES, "live"),
            ("retrieve_page", str(jo["id"]), "mirror"),
            ("list_block_children", str(jo["id"]), "live"),
            ("retrieve_page", str(jo["id"]), "live"),
            ("list_block_children", str(jo["id"]), "live"),
            ("query_data_source", JOB_ORDERS, "mirror"),
            ("list_block_children", str(jo["id"]), "live"),
            ("query_data_source", JOB_ORDERS, "live"),
            ("list_block_children", str(jo["id"]), "live"),
            ("query_data_source", JOB_ORDERS, "mirror"),
            ("query_data_source", JOB_ORDERS, "live"),
            ("query_data_source", JOB_ORDERS, "mirror"),
            ("query_data_source", JOB_ORDERS, "live"),
        ]
        self.assertEqual(source.calls, expected)

    def test_job_orders_create_and_ensure_body_work_via_factory_source_with_live_readback(self) -> None:
        notion = fake()
        source = _RecordingNotionSource(notion.client)
        orders = JobOrders(source, JOB_ORDERS)
        saved = orders.create({"name": "JO 2", "source_text": "Body line"})
        self.assertEqual(getattr(saved, "source_text", None), "Body line")
        page_id = getattr(saved, "page_id")
        self.assertIsNone(orders.ensure_body(page_id, "Body line"))
        self.assertEqual(
            [call for call in source.calls if call[0] in ("retrieve_page", "list_block_children")],
            [
                ("retrieve_page", page_id, "live"),
                ("list_block_children", page_id, "live"),
                ("retrieve_page", page_id, "live"),
                ("list_block_children", page_id, "live"),
                ("list_block_children", page_id, "live"),
            ],
        )

    def test_all_other_notion_readers_and_helpers_route_through_notion_source(self) -> None:
        import tempfile
        from unittest.mock import MagicMock
        from t6.fleet import Fleet, MachineFacts
        from t6.google.names import NotionNames
        from t6.media_migration import read_external_links_once_for_migration
        from t6.notion.cameras import get_cameras
        from t6.notion.contacts import Contacts
        from t6.notion.deliverable_projection import DeliverableProjection
        from t6.notion.departments import get_departments
        from t6.notion.editor import editor as notion_editor
        from t6.notion.jo_projection import JobOrderProjection
        from t6.notion.meeting_notes import MeetingNotes
        from t6.notion.migration import read_legacy_projects
        from t6.notion.package_specs import PackageSpecReader
        from t6.notion.projection import ProjectProjection
        from t6.notion.schema import check_schema, load_snapshot, refresh_snapshot
        from t6.notion.swing_drives import read_swing_drives
        from t6.notion.testing import external_link_properties, package_spec_properties
        from t6.registry import Registered, SqliteProjectRegistry
        from t6.worker.client import WorkerJob

        notion = fake()
        for ds_id, db_name in (
            ("ds-cameras", "cameras"),
            ("ds-meeting-notes", "meeting_notes"),
            ("ds-package-specs", "package_specs"),
            ("ds-bays", "bays"),
            ("ds-swing", "swing_drives"),
        ):
            notion.add_snapshot_data_source(ds_id, db_name)
        notion.add_data_source("ds-links")
        notion.add_option(JOB_ORDERS, "Department", "AE")
        notion.add_user("user-1", "Ana Cruz")
        proj = notion.add_page(PROJECTS, {ProjectsRow.notion_name("name"): title("Show A")})
        contact = notion.add_page(CONTACTS, {ContactsRow.notion_name("name"): title("Ana Cruz")})
        notion.add_page("ds-cameras", {"Name": title("Camera One")})
        notion.add_page("ds-package-specs", package_spec_properties(package_kind="delivery", version="1"))
        notion.add_page("ds-bays", {"Name": title("Bay 1"), "Hostname": rich_text("bay-1")})
        notion.add_page("ds-swing", {"Name": title("Swing 1")})
        notion.add_page("ds-links", external_link_properties(system="resolve", kind="project", external_id="db/show", projects=[str(proj["id"])], active=True))

        source = _RecordingNotionSource(notion.client)

        contacts = Contacts(source, CONTACTS)
        self.assertEqual(len(contacts.list()), 1)
        self.assertEqual(contacts.get(str(contact["id"])).name, "Ana Cruz")
        contacts.set_telegram_user_id(str(contact["id"]), 42)
        self.assertEqual(
            source.calls,
            [
                ("query_data_source", CONTACTS, "mirror"),
                ("retrieve_page", str(contact["id"]), "mirror"),
                ("retrieve_page", str(contact["id"]), "live"),
            ],
        )

        source.calls.clear()
        self.assertEqual(len(get_cameras(source, "ds-cameras")), 1)
        self.assertEqual(source.calls, [("query_data_source", "ds-cameras", "mirror")])

        source.calls.clear()
        self.assertIn("AE", [d.name for d in get_departments(source, JOB_ORDERS)])
        self.assertEqual(source.calls, [("retrieve_data_source", JOB_ORDERS, "live")])

        source.calls.clear()
        notes = MeetingNotes(source, "ds-meeting-notes")
        created_note = notes.create(title="Weekly", meeting_date=dt.date(2026, 11, 16))
        notes.with_title("Weekly")
        self.assertEqual(
            source.calls,
            [
                ("retrieve_page", created_note.page_id, "live"),  # type: ignore[union-attr]
                ("retrieve_page", created_note.page_id, "live"),  # type: ignore[union-attr]
                ("query_data_source", "ds-meeting-notes", "mirror"),
            ],
        )

        source.calls.clear()
        self.assertEqual(len(PackageSpecReader(source, "ds-package-specs").find("delivery")), 1)
        self.assertEqual(len(PackageSpecReader(source, "ds-package-specs", freshness="live").find("delivery")), 1)
        self.assertEqual(
            source.calls,
            [
                ("query_data_source", "ds-package-specs", "mirror"),
                ("query_data_source", "ds-package-specs", "live"),
            ],
        )

        source.calls.clear()
        self.assertEqual(len(read_swing_drives(source, "ds-swing")), 1)
        self.assertEqual(len(read_external_links_once_for_migration(source, "ds-links")), 1)
        legacy_projects, _ = read_legacy_projects(source, {"projects": PROJECTS})
        self.assertEqual(len(legacy_projects), 1)
        self.assertEqual(
            source.calls,
            [
                ("query_data_source", "ds-swing", "live"),
                ("query_data_source", "ds-links", "live"),
                ("query_data_source", PROJECTS, "live"),
            ],
        )

        source.calls.clear()
        snap = refresh_snapshot(source, load_snapshot(), {"projects": PROJECTS})
        check_schema(source, snap, {"projects": PROJECTS})
        self.assertEqual(
            source.calls,
            [
                ("retrieve_data_source", PROJECTS, "live"),
                ("retrieve_data_source", PROJECTS, "live"),
                ("query_data_source", PROJECTS, "live"),
            ],
        )

        source.calls.clear()
        self.assertEqual(
            notion_editor(source, WorkerJob(1, "main", "notion.page_changed", "k", {"authors": [{"id": "user-1"}]}, "t", 1)),
            ("Ana Cruz", "user-1"),
        )
        self.assertEqual(source.calls, [("retrieve_user", "user-1", "live")])

        source.calls.clear()
        fleet = Fleet(source, "ds-bays")
        self.assertEqual(len(fleet.machines()), 1)
        fleet.sync([MachineFacts(names=("bay-1",), serial_number="S1", observed_at=dt.datetime.now(dt.timezone.utc))], apply=True)
        self.assertEqual(
            source.calls,
            [
                ("query_data_source", "ds-bays", "mirror"),
                ("retrieve_data_source", "ds-bays", "live"),
                ("query_data_source", "ds-bays", "mirror"),
                ("retrieve_page", source.calls[3][1], "live"),
                ("retrieve_page", source.calls[3][1], "live"),
                ("retrieve_page", source.calls[3][1], "live"),
            ],
        )

        with tempfile.TemporaryDirectory() as tmp:
            reg = SqliteProjectRegistry(f"{tmp}/registry.sqlite3")
            reg_proj = reg.register(actor="setup", idempotency_key="p", notion_page_id=str(proj["id"]))
            assert isinstance(reg_proj, Registered)
            with reg._transaction() as db:
                db.execute("INSERT INTO persons VALUES ('t6n_1', 'staff', '[]', '[]', NULL, 0, 1)")
                db.execute(
                    "INSERT INTO bindings (system, page_id, project_id, bound_at, active) VALUES ('notion', ?, 't6n_1', 'now', 1)",
                    (str(contact["id"]).replace("-", ""),),
                )
            source.calls.clear()
            names = NotionNames(reg, source)
            self.assertEqual(names.project(reg_proj.project.id), "Show A")
            self.assertEqual(names.person("t6n_1"), "Ana Cruz")
            self.assertEqual(
                source.calls,
                [
                    ("retrieve_page", str(proj["id"]), "mirror"),
                    ("retrieve_page", str(contact["id"]), "mirror"),
                ],
            )

        mock_reg = MagicMock()
        mock_reg.observed.return_value = "bound"
        mock_reg.observe_job_order.return_value = {}
        mock_reg.observe_deliverable.return_value = {}
        source.calls.clear()
        ProjectProjection(source, mock_reg, PROJECTS).reconcile(WorkerJob(1, "main", "notion.page_changed", "k1", {"page_id": str(proj["id"]), "authors": []}, "t", 1))
        JobOrderProjection(source, mock_reg, JOB_ORDERS).reconcile(WorkerJob(2, "main", "notion.page_changed", "k2", {"page_id": str(proj["id"]), "authors": []}, "t", 1))
        DeliverableProjection(source, mock_reg, DELIVERABLES).reconcile(WorkerJob(3, "main", "notion.page_changed", "k3", {"page_id": str(proj["id"]), "authors": []}, "t", 1))
        self.assertEqual(
            source.calls,
            [
                ("retrieve_page", str(proj["id"]), "live"),
                ("retrieve_page", str(proj["id"]), "live"),
                ("retrieve_page", str(proj["id"]), "live"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
