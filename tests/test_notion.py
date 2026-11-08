"""Exercise t6.notion through its public interface, against the in-memory fake."""

import asyncio
import unittest

from t6.domain import Project
from t6.notion import (
    NOTION_VERSION,
    AsyncNotionClient,
    NotionClient,
    NotionError,
    RetryPolicy,
)
from t6.notion.models import project_from_page
from t6.notion.testing import FakeNotion, project_properties

PROJECTS = "ds-projects"


class Sleeps:
    """Records retry delays instead of waiting."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def fake_with_project() -> tuple[FakeNotion, str]:
    fake = FakeNotion()
    fake.add_data_source(PROJECTS)
    page = fake.add_page(
        PROJECTS,
        project_properties(
            name="Harbour Lights",
            project_code="HBL",
            project_type="Limited Series",
            eps=6,
            status="In-Progress",
            studio_id="studio-1",
        ),
    )
    return fake, page["id"]


class ReadProjectTests(unittest.TestCase):
    def test_reading_a_project_page_returns_a_domain_project(self) -> None:
        fake, page_id = fake_with_project()
        with fake.client() as client:
            project = project_from_page(client.retrieve_page(page_id))
        self.assertIsInstance(project, Project)
        self.assertEqual(project.page_id, page_id)
        self.assertEqual(project.project_code, "HBL")
        self.assertEqual(project.name, "Harbour Lights")
        self.assertEqual(project.project_type, "Limited Series")
        self.assertEqual(project.eps, 6)
        self.assertEqual(project.status, "In-Progress")
        self.assertEqual(project.studio_id, "studio-1")
        self.assertEqual(len(project.episodes), 6)

    def test_blank_properties_become_none(self) -> None:
        fake = FakeNotion()
        fake.add_data_source(PROJECTS)
        page = fake.add_page(PROJECTS, project_properties(name="Untitled pitch"))
        with fake.client() as client:
            project = project_from_page(client.retrieve_page(page["id"]))
        self.assertIsNone(project.project_code)
        self.assertIsNone(project.eps)
        self.assertIsNone(project.studio_id)

    def test_unknown_properties_are_ignored(self) -> None:
        fake = FakeNotion()
        fake.add_data_source(PROJECTS)
        properties = project_properties(name="Quiet Valley", project_code="QV")
        properties["Brand New Column "] = {"type": "checkbox", "checkbox": True}
        page = fake.add_page(PROJECTS, properties)
        with fake.client() as client:
            project = project_from_page(client.retrieve_page(page["id"]))
        self.assertEqual(project.project_code, "QV")

    def test_a_missing_page_raises_notion_error(self) -> None:
        fake = FakeNotion()
        with fake.client() as client, self.assertRaises(NotionError) as caught:
            client.retrieve_page("nope")
        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(caught.exception.code, "object_not_found")


class PaginationTests(unittest.TestCase):
    def test_a_query_spanning_several_pages_returns_every_row(self) -> None:
        fake = FakeNotion()
        fake.add_data_source(PROJECTS)
        ids = {fake.add_page(PROJECTS, project_properties(name=f"Show {n}"))["id"] for n in range(7)}
        with fake.client() as client:
            rows = client.query_data_source(PROJECTS, page_size=3)
        self.assertEqual({row["id"] for row in rows}, ids)
        queries = [r for r in fake.requests if r.url.path.endswith("/query")]
        self.assertEqual(len(queries), 3)

    def test_a_filter_is_passed_through(self) -> None:
        fake = FakeNotion()
        fake.add_data_source(PROJECTS)
        fake.add_page(PROJECTS, project_properties(name="A", status="Archived"))
        wanted = fake.add_page(PROJECTS, project_properties(name="B", status="In-Progress"))
        with fake.client() as client:
            rows = client.query_data_source(
                PROJECTS, filter={"property": "Status", "select": {"equals": "In-Progress"}}
            )
        self.assertEqual([row["id"] for row in rows], [wanted["id"]])


class RetryTests(unittest.TestCase):
    def test_without_retry_a_429_raises_at_once(self) -> None:
        fake, page_id = fake_with_project()
        fake.fail_next(429, code="rate_limited", retry_after=2)
        with fake.client() as client, self.assertRaises(NotionError) as caught:
            client.retrieve_page(page_id)
        self.assertEqual(caught.exception.status, 429)
        self.assertEqual(caught.exception.retry_after, 2.0)

    def test_a_429_retries_on_a_read(self) -> None:
        fake, page_id = fake_with_project()
        fake.fail_next(429, code="rate_limited", retry_after=2)
        sleeps = Sleeps()
        with fake.client(retry=RetryPolicy(sleep=sleeps)) as client:
            page = client.retrieve_page(page_id)
        self.assertEqual(page["id"], page_id)
        self.assertEqual(sleeps.delays, [2.0])

    def test_a_429_retries_on_a_write(self) -> None:
        fake = FakeNotion()
        fake.add_data_source(PROJECTS)
        fake.fail_next(429, code="rate_limited")
        sleeps = Sleeps()
        with fake.client(retry=RetryPolicy(sleep=sleeps)) as client:
            page = client.create_page(
                {
                    "parent": {"type": "data_source_id", "data_source_id": PROJECTS},
                    "properties": project_properties(name="New Show"),
                }
            )
        self.assertEqual(len(sleeps.delays), 1)
        self.assertEqual(fake.page(page["id"])["id"], page["id"])

    def test_a_5xx_retries_on_a_read_with_backoff(self) -> None:
        fake, page_id = fake_with_project()
        fake.fail_next(502, code="bad_gateway", times=2)
        sleeps = Sleeps()
        policy = RetryPolicy(attempts=3, base_delay=0.5, sleep=sleeps)
        with fake.client(retry=policy) as client:
            client.retrieve_page(page_id)
        self.assertEqual(sleeps.delays, [0.5, 1.0])

    def test_a_query_is_a_read(self) -> None:
        fake, _ = fake_with_project()
        fake.fail_next(503, code="service_unavailable")
        with fake.client(retry=RetryPolicy(sleep=Sleeps())) as client:
            self.assertEqual(len(client.query_data_source(PROJECTS)), 1)

    def test_a_timeout_retries_on_a_read(self) -> None:
        fake, page_id = fake_with_project()
        fake.time_out_next()
        with fake.client(retry=RetryPolicy(sleep=Sleeps())) as client:
            self.assertEqual(client.retrieve_page(page_id)["id"], page_id)

    def test_a_write_with_a_5xx_raises(self) -> None:
        fake, page_id = fake_with_project()
        fake.fail_next(500, code="internal_server_error")
        sleeps = Sleeps()
        with fake.client(retry=RetryPolicy(sleep=sleeps)) as client:
            with self.assertRaises(NotionError) as caught:
                client.update_page(page_id, {"properties": {}})
        self.assertEqual(caught.exception.status, 500)
        self.assertEqual(sleeps.delays, [])

    def test_a_write_that_times_out_raises(self) -> None:
        fake = FakeNotion()
        fake.add_data_source(PROJECTS)
        fake.time_out_next()
        with fake.client(retry=RetryPolicy(sleep=Sleeps())) as client:
            with self.assertRaises(NotionError) as caught:
                client.create_page({"parent": {"data_source_id": PROJECTS}, "properties": {}})
        self.assertIsNone(caught.exception.status)
        self.assertEqual(caught.exception.code, "timeout")
        self.assertEqual(fake.page_count(), 0)

    def test_retries_stop_after_the_last_attempt(self) -> None:
        fake, page_id = fake_with_project()
        fake.fail_next(502, code="bad_gateway", times=5)
        sleeps = Sleeps()
        with fake.client(retry=RetryPolicy(attempts=3, sleep=sleeps)) as client:
            with self.assertRaises(NotionError):
                client.retrieve_page(page_id)
        self.assertEqual(len(sleeps.delays), 2)

    def test_a_4xx_other_than_429_does_not_retry(self) -> None:
        fake, page_id = fake_with_project()
        fake.fail_next(400, code="validation_error")
        sleeps = Sleeps()
        with fake.client(retry=RetryPolicy(sleep=sleeps)) as client:
            with self.assertRaises(NotionError):
                client.retrieve_page(page_id)
        self.assertEqual(sleeps.delays, [])


class NotionVersionTests(unittest.TestCase):
    def test_every_request_sends_the_pinned_version(self) -> None:
        fake, page_id = fake_with_project()
        with fake.client() as client:
            client.retrieve_page(page_id)
            client.query_data_source(PROJECTS)
        self.assertEqual({r.headers["Notion-Version"] for r in fake.requests}, {NOTION_VERSION})

    def test_the_version_can_be_overridden_per_instance(self) -> None:
        fake, page_id = fake_with_project()
        with fake.client(notion_version="2099-01-01") as overridden, fake.client() as pinned:
            overridden.retrieve_page(page_id)
            pinned.retrieve_page(page_id)
        self.assertEqual(
            [r.headers["Notion-Version"] for r in fake.requests], ["2099-01-01", NOTION_VERSION]
        )


class SyncMatchesAsyncTests(unittest.TestCase):
    def test_sync_wrapper_gives_the_same_results(self) -> None:
        fake, page_id = fake_with_project()
        for n in range(4):
            fake.add_page(PROJECTS, project_properties(name=f"Show {n}"))

        async def read_async() -> tuple[object, object, object]:
            async with fake.async_client() as client:
                return (
                    await client.retrieve_page(page_id),
                    await client.query_data_source(PROJECTS, page_size=2),
                    await client.retrieve_data_source(PROJECTS),
                )

        with fake.client() as client:
            sync_results = (
                client.retrieve_page(page_id),
                client.query_data_source(PROJECTS, page_size=2),
                client.retrieve_data_source(PROJECTS),
            )
        self.assertEqual(asyncio.run(read_async()), sync_results)

    def test_both_clients_are_constructible_without_a_fake(self) -> None:
        with NotionClient("not-a-real-token") as client:
            self.assertIsInstance(client, NotionClient)

        async def build() -> None:
            async with AsyncNotionClient("not-a-real-token") as client:
                self.assertIsInstance(client, AsyncNotionClient)

        asyncio.run(build())


if __name__ == "__main__":
    unittest.main()
