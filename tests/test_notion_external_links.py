"""Exercise External Links reads and writes through the public interface, on the in-memory fake."""

import json
import unittest
from datetime import datetime, timezone

from t6.domain import ExternalRef
from t6.notion import NotionError, RetryPolicy
from t6.notion.external_links import ExternalLink, ExternalLinks, external_link_from_page
from t6.notion.testing import FakeNotion, external_link_properties

LINKS = "ds-external-links"
PROJECT = "project-1"
OTHER_PROJECT = "project-2"

FRAMEIO = ExternalRef(
    system="frameio", kind="project", external_id="f-1", url="https://f.io/p/f-1", internal_id="i-1"
)


async def no_sleep(delay: float) -> None:
    return None


def fake_links() -> FakeNotion:
    fake = FakeNotion()
    fake.add_data_source(LINKS)
    return fake


def bodies(fake: FakeNotion, method: str) -> list[dict[str, object]]:
    return [json.loads(r.content) for r in fake.requests if r.method == method]


class ReadTests(unittest.TestCase):
    def test_reading_links_for_a_project_returns_domain_external_refs(self) -> None:
        fake = fake_links()
        fake.add_page(
            LINKS,
            external_link_properties(
                system="frameio",
                kind="project",
                external_id="f-1",
                internal_id="i-1",
                url="https://f.io/p/f-1",
                projects=[PROJECT],
            ),
        )
        fake.add_page(
            LINKS,
            external_link_properties(
                system="resolve", kind="project", external_id="db/Show", projects=[PROJECT]
            ),
        )
        fake.add_page(
            LINKS,
            external_link_properties(
                system="frameio", kind="project", external_id="other", projects=[OTHER_PROJECT]
            ),
        )
        with fake.client() as client:
            refs = ExternalLinks(client, LINKS).refs_for_project(PROJECT)
        self.assertEqual(
            refs,
            (
                ExternalRef(system="frameio", kind="project", external_id="f-1"),
                ExternalRef(system="resolve", kind="project", external_id="db/Show"),
            ),
        )
        self.assertEqual(refs[0].url, "https://f.io/p/f-1")
        self.assertEqual(refs[0].internal_id, "i-1")
        self.assertIsNone(refs[1].internal_id)

    def test_a_project_without_links_has_no_refs(self) -> None:
        fake = fake_links()
        with fake.client() as client:
            self.assertEqual(ExternalLinks(client, LINKS).refs_for_project(PROJECT), ())

    def test_a_row_missing_system_kind_or_external_id_is_skipped(self) -> None:
        fake = fake_links()
        fake.add_page(LINKS, external_link_properties(system="frameio", projects=[PROJECT]))
        with fake.client() as client:
            self.assertEqual(ExternalLinks(client, LINKS).refs_for_project(PROJECT), ())

    def test_a_row_carries_active_and_last_activity_as_people_set_them(self) -> None:
        fake = fake_links()
        page = fake.add_page(
            LINKS,
            external_link_properties(
                system="frameio",
                kind="project",
                external_id="f-1",
                active=True,
                last_activity="2026-11-01T10:30:00+00:00",
            ),
        )
        link = external_link_from_page(page)
        assert link is not None
        self.assertEqual(link.page_id, page["id"])
        self.assertIs(link.active, True)
        self.assertEqual(link.last_activity, datetime(2026, 11, 1, 10, 30, tzinfo=timezone.utc))

    def test_an_unticked_row_is_not_active(self) -> None:
        fake = fake_links()
        page = fake.add_page(
            LINKS, external_link_properties(system="a", kind="b", external_id="c")
        )
        link = external_link_from_page(page)
        assert link is not None
        self.assertIs(link.active, False)
        self.assertIsNone(link.last_activity)


class EnsureTests(unittest.TestCase):
    def test_creating_a_missing_link_makes_one_row_with_every_field(self) -> None:
        fake = fake_links()
        with fake.client() as client:
            link = ExternalLinks(client, LINKS).ensure(FRAMEIO, project_id=PROJECT)
        self.assertIsInstance(link, ExternalLink)
        self.assertEqual(link.ref, FRAMEIO)
        self.assertEqual(link.ref.internal_id, "i-1")
        self.assertEqual(fake.page_count(), 1)
        with fake.client() as client:
            self.assertEqual(
                ExternalLinks(client, LINKS).refs_for_project(PROJECT), (FRAMEIO,)
            )

    def test_creating_a_link_that_exists_returns_it_and_makes_no_duplicate(self) -> None:
        fake = fake_links()
        with fake.client() as client:
            links = ExternalLinks(client, LINKS)
            first = links.ensure(FRAMEIO, project_id=PROJECT)
            writes = len(bodies(fake, "POST")) + len(bodies(fake, "PATCH"))
            again = links.ensure(
                ExternalRef(system="frameio", kind="project", external_id="f-1", url="https://new"),
                project_id=PROJECT,
            )
        self.assertEqual(again.page_id, first.page_id)
        self.assertEqual(fake.page_count(), 1)
        creates = [b for b in bodies(fake, "POST") if "parent" in b]
        self.assertEqual(len(creates), 1)
        self.assertEqual(
            len(bodies(fake, "POST")) + len(bodies(fake, "PATCH")),
            writes + 1,  # only the second lookup query
        )

    def test_same_external_id_in_another_system_or_kind_is_a_different_link(self) -> None:
        fake = fake_links()
        with fake.client() as client:
            links = ExternalLinks(client, LINKS)
            a = links.ensure(FRAMEIO, project_id=PROJECT)
            b = links.ensure(
                ExternalRef(system="resolve", kind="project", external_id="f-1"), project_id=PROJECT
            )
            c = links.ensure(
                ExternalRef(system="frameio", kind="share_link", external_id="f-1"),
                project_id=PROJECT,
            )
        self.assertEqual(len({a.page_id, b.page_id, c.page_id}), 3)
        self.assertEqual(fake.page_count(), 3)

    def test_a_link_can_relate_to_other_rows(self) -> None:
        fake = fake_links()
        with fake.client() as client:
            link = ExternalLinks(client, LINKS).ensure(
                FRAMEIO, project_id=PROJECT, deliverable_ids=["d-1"], contact_ids=["c-1", "c-2"]
            )
        props = fake.page(link.page_id)["properties"]
        self.assertEqual([r["id"] for r in props["Projects"]["relation"]], [PROJECT])
        self.assertEqual([r["id"] for r in props["Deliverables"]["relation"]], ["d-1"])
        self.assertEqual([r["id"] for r in props["Contacts"]["relation"]], ["c-1", "c-2"])

    def test_code_never_sets_active(self) -> None:
        fake = fake_links()
        with fake.client() as client:
            link = ExternalLinks(client, LINKS).ensure(FRAMEIO, project_id=PROJECT)
        self.assertNotIn("Active", fake.page(link.page_id)["properties"])
        for body in bodies(fake, "POST"):
            self.assertNotIn("Active", json.dumps(body))
        self.assertIs(link.active, False)

    def test_a_server_error_on_create_is_not_retried_into_a_duplicate(self) -> None:
        fake = fake_links()
        fake.fail_next(500, after=1)  # let the lookup query through, fail the create
        with fake.client(retry=RetryPolicy(sleep=no_sleep)) as client:
            with self.assertRaises(NotionError):
                ExternalLinks(client, LINKS).ensure(FRAMEIO, project_id=PROJECT)
        creates = [b for b in bodies(fake, "POST") if "parent" in b]
        self.assertEqual(len(creates), 1)
        self.assertEqual(fake.page_count(), 0)


class TouchTests(unittest.TestCase):
    WHEN = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)

    def test_code_can_update_last_activity(self) -> None:
        fake = fake_links()
        with fake.client() as client:
            links = ExternalLinks(client, LINKS)
            link = links.ensure(FRAMEIO, project_id=PROJECT)
            updated = links.touch(link.page_id, self.WHEN)
        self.assertEqual(updated.last_activity, self.WHEN)
        stored = fake.page(link.page_id)["properties"]["Last Activity"]
        self.assertEqual(stored["date"]["start"], "2026-11-08T12:00:00+00:00")

    def test_updating_last_activity_leaves_active_as_people_set_it(self) -> None:
        fake = fake_links()
        page = fake.add_page(
            LINKS,
            external_link_properties(
                system="frameio", kind="project", external_id="f-1", active=True
            ),
        )
        with fake.client() as client:
            links = ExternalLinks(client, LINKS)
            updated = links.touch(page["id"], self.WHEN)
        self.assertIs(updated.active, True)
        patches = bodies(fake, "PATCH")
        self.assertEqual(len(patches), 1)
        self.assertEqual(set(patches[0]["properties"]), {"Last Activity"})  # type: ignore[call-overload]

    def test_last_activity_must_carry_a_timezone(self) -> None:
        fake = fake_links()
        with fake.client() as client:
            links = ExternalLinks(client, LINKS)
            link = links.ensure(FRAMEIO, project_id=PROJECT)
            with self.assertRaises(ValueError):
                links.touch(link.page_id, datetime(2026, 11, 8, 12, 0))


class RoundTripTests(unittest.TestCase):
    def test_internal_id_url_and_identity_survive_a_round_trip(self) -> None:
        fake = fake_links()
        with fake.client() as client:
            links = ExternalLinks(client, LINKS)
            links.ensure(FRAMEIO, project_id=PROJECT)
            (read,) = links.refs_for_project(PROJECT)
        self.assertEqual(read, FRAMEIO)
        self.assertEqual(read.internal_id, "i-1")
        self.assertEqual(read.url, "https://f.io/p/f-1")


if __name__ == "__main__":
    unittest.main()
