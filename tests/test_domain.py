"""Exercise t6.domain through its public interface."""

from datetime import date
import subprocess
import sys
import unittest

from t6.domain import (
    Camera,
    Contact,
    Deliverable,
    Episode,
    ExternalRef,
    JobOrder,
    Machine,
    Project,
    Studio,
)


SERIES_TYPES = (
    "Serial TV",
    "Serial TV - Color",
    "Streamer Originals Series",
    "Limited Series",
    "Limited Documentary Series",
)


class EpisodeDerivationTests(unittest.TestCase):
    def test_each_series_type_yields_one_episode_per_eps(self) -> None:
        for project_type in SERIES_TYPES:
            with self.subTest(project_type=project_type):
                project = Project(page_id="p1", name="Show", project_type=project_type, eps=3)
                self.assertEqual(
                    project.episodes,
                    (
                        Episode(project_id="p1", number=1),
                        Episode(project_id="p1", number=2),
                        Episode(project_id="p1", number=3),
                    ),
                )

    def test_film_and_other_non_series_types_yield_no_episodes(self) -> None:
        for project_type in ("Film", "TVC", "Documentary", None):
            with self.subTest(project_type=project_type):
                project = Project(page_id="p1", name="Show", project_type=project_type, eps=8)
                self.assertEqual(project.episodes, ())

    def test_series_without_eps_yields_no_episodes(self) -> None:
        for eps in (None, 0):
            with self.subTest(eps=eps):
                project = Project(page_id="p1", name="Show", project_type="Limited Series", eps=eps)
                self.assertEqual(project.episodes, ())

    def test_negative_eps_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Project(page_id="p1", name="Show", project_type="Limited Series", eps=-1)


class ProjectIdentityTests(unittest.TestCase):
    def test_projects_with_the_same_page_id_are_the_same_project(self) -> None:
        before = Project(page_id="p1", name="Old name", project_code="ABC")
        after = Project(page_id="p1", name="New name", project_code="XYZ")
        self.assertEqual(before, after)
        self.assertEqual(len({before, after}), 1)

    def test_projects_with_different_page_ids_differ_even_with_the_same_code(self) -> None:
        self.assertNotEqual(
            Project(page_id="p1", name="Show", project_code="ABC"),
            Project(page_id="p2", name="Show", project_code="ABC"),
        )

    def test_project_code_is_carried_as_the_human_key(self) -> None:
        self.assertEqual(Project(page_id="p1", name="Show", project_code="ABC").project_code, "ABC")

    def test_project_code_may_be_blank(self) -> None:
        for code in (None, "", "   "):
            with self.subTest(code=code):
                self.assertIsNone(Project(page_id="p1", name="Show", project_code=code).project_code)

    def test_page_id_is_required(self) -> None:
        for page_id in ("", "  "):
            with self.subTest(page_id=page_id):
                with self.assertRaises(ValueError):
                    Project(page_id=page_id, name="Show")


class ExternalRefTests(unittest.TestCase):
    def test_holds_system_kind_external_id_and_url(self) -> None:
        ref = ExternalRef(
            system="frameio",
            kind="share_link",
            external_id="abc-123",
            url="https://f.io/abc",
        )
        self.assertEqual(
            (ref.system, ref.kind, ref.external_id, ref.url),
            ("frameio", "share_link", "abc-123", "https://f.io/abc"),
        )

    def test_url_is_optional(self) -> None:
        ref = ExternalRef(system="resolve", kind="project", external_id="db/folder/Show")
        self.assertIsNone(ref.url)

    def test_same_object_in_the_same_system_is_the_same_ref_even_if_the_url_moved(self) -> None:
        self.assertEqual(
            ExternalRef(system="frameio", kind="share_link", external_id="abc", url="https://a"),
            ExternalRef(system="frameio", kind="share_link", external_id="abc", url="https://b"),
        )
        self.assertNotEqual(
            ExternalRef(system="frameio", kind="share_link", external_id="abc"),
            ExternalRef(system="frameio", kind="project", external_id="abc"),
        )

    def test_internal_id_is_optional_and_not_part_of_the_identity(self) -> None:
        plain = ExternalRef(system="resolve", kind="project", external_id="db/Show")
        self.assertIsNone(plain.internal_id)
        with_id = ExternalRef(
            system="resolve", kind="project", external_id="db/Show", internal_id="p-77"
        )
        self.assertEqual(with_id.internal_id, "p-77")
        self.assertEqual(plain, with_id)
        self.assertEqual(
            with_id,
            ExternalRef(system="resolve", kind="project", external_id="db/Show", internal_id="p-9"),
        )

    def test_a_blank_internal_id_is_none(self) -> None:
        ref = ExternalRef(system="resolve", kind="project", external_id="x", internal_id="  ")
        self.assertIsNone(ref.internal_id)

    def test_system_kind_and_external_id_are_required(self) -> None:
        for blank in ("system", "kind", "external_id"):
            with self.subTest(blank=blank):
                values = {"system": "frameio", "kind": "share_link", "external_id": "abc"}
                values[blank] = " "
                with self.assertRaises(ValueError):
                    ExternalRef(**values)


class NotionEntityTests(unittest.TestCase):
    """Every entity stored as a Notion row is identified by its page ID."""

    ENTITIES = (Project, Deliverable, JobOrder, Contact, Studio, Machine, Camera)

    def test_same_page_id_means_same_entity(self) -> None:
        for entity in self.ENTITIES:
            with self.subTest(entity=entity.__name__):
                self.assertEqual(entity(page_id="x", name="A"), entity(page_id="x", name="B"))
                self.assertNotEqual(entity(page_id="x", name="A"), entity(page_id="y", name="A"))

    def test_page_id_is_required(self) -> None:
        for entity in self.ENTITIES:
            with self.subTest(entity=entity.__name__):
                with self.assertRaises(ValueError):
                    entity(page_id="", name="A")

    def test_entities_are_immutable(self) -> None:
        studio = Studio(page_id="s1", name="Studio")
        with self.assertRaises(AttributeError):
            studio.name = "Other"  # type: ignore[misc]

    def test_deliverable_belongs_to_a_project_and_may_have_a_due_date(self) -> None:
        deliverable = Deliverable(
            page_id="d1", name="Final mix", project_id="p1", due=date(2026, 12, 1), status="Pending"
        )
        self.assertEqual(
            (deliverable.project_id, deliverable.due, deliverable.status),
            ("p1", date(2026, 12, 1), "Pending"),
        )
        self.assertIsNone(Deliverable(page_id="d2", name="Trailer").due)

    def test_job_order_carries_project_department_and_telegram_key(self) -> None:
        job_order = JobOrder(
            page_id="j1", name="Conform", project_id="p1", department="Online", telegram_key="JO-7"
        )
        self.assertEqual(
            (job_order.project_id, job_order.department, job_order.telegram_key),
            ("p1", "Online", "JO-7"),
        )

    def test_contact_carries_studio_and_telegram_identity(self) -> None:
        contact = Contact(
            page_id="c1",
            name="Ana",
            studio_id="s1",
            email="ana@example.com",
            telegram_handle="ana",
            telegram_user_id=42,
        )
        self.assertEqual(
            (contact.studio_id, contact.email, contact.telegram_handle, contact.telegram_user_id),
            ("s1", "ana@example.com", "ana", 42),
        )

    def test_project_may_link_a_studio(self) -> None:
        self.assertEqual(Project(page_id="p1", name="Show", studio_id="s1").studio_id, "s1")

    def test_machine_is_a_bay_with_network_identity(self) -> None:
        machine = Machine(
            page_id="m1", name="Sixto", hostname="sixto", lan_ip="192.0.2.10", os_version="11.7.10"
        )
        self.assertEqual(
            (machine.hostname, machine.lan_ip, machine.os_version),
            ("sixto", "192.0.2.10", "11.7.10"),
        )

    def test_camera_carries_tags(self) -> None:
        self.assertEqual(
            Camera(page_id="k1", name="ALEXA 35", tags=("ARRI", "Large format")).tags,
            ("ARRI", "Large format"),
        )
        self.assertEqual(Camera(page_id="k2", name="FX6").tags, ())

    def test_entities_link_external_refs(self) -> None:
        ref = ExternalRef(system="frameio", kind="project", external_id="f1")
        for entity in self.ENTITIES:
            with self.subTest(entity=entity.__name__):
                self.assertEqual(entity(page_id="x", name="A", external_refs=(ref,)).external_refs, (ref,))
                self.assertEqual(entity(page_id="x", name="A").external_refs, ())


class NoIOTests(unittest.TestCase):
    def test_importing_the_domain_loads_no_io_modules(self) -> None:
        probe = (
            "import sys, t6.domain; "
            "io = {'socket', 'http.client', 'urllib.request', 'sqlite3', 'subprocess', 'asyncio'}; "
            "print(sorted(io & set(sys.modules)))"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe], check=True, capture_output=True, text=True
        )
        self.assertEqual(result.stdout.strip(), "[]")


if __name__ == "__main__":
    unittest.main()
