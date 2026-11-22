"""The `@handle` of the Project's coordinators in the Frame.io review message (#547). Synthetic names and handles only."""

import unittest
from types import SimpleNamespace
from typing import Any

from t6.notion import NotionSource
from t6.notion.generated import ContactsRow, ProjectsRow
from t6.notion.reads import ContactReader, ProjectReader
from t6.notion.testing import FakeNotion, relation, rich_text, title
from t6.worker.client import WorkerJob
from t6.worker.gateway import GatewayClient
from t6.worker.review import SEND_ANYWAY_AT_ATTEMPT, ReviewMessage
from t6.worker.review_coordinators import ReviewCoordinators
from test_worker_review import CHAT, FakeNotify

PROJECTS, CONTACTS = "ds-projects", "ds-contacts"


class _Source(NotionSource):
    """Records what is read, to show a Project and its Contacts are read page by page, never by scanning a data source."""

    def __init__(self, client: Any) -> None:
        super().__init__(client)
        self.calls: list[tuple[str, str]] = []

    def retrieve_page(self, page_id: str, *, freshness: Any = "mirror") -> Any:
        self.calls.append((freshness, "page"))
        return super().retrieve_page(page_id, freshness=freshness)

    def query_data_source(self, data_source_id: str, **kwargs: Any) -> Any:
        self.calls.append((kwargs.get("freshness", "mirror"), "scan"))
        return super().query_data_source(data_source_id, **kwargs)


class Links:
    def __init__(self, *pages: str) -> None:
        self.pages = pages

    def media_links(self, kind: str, role: str | None = None) -> tuple[Any, ...]:
        return (SimpleNamespace(ref="fp1", kind=kind, pages=self.pages),) if self.pages else ()


class CoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.notion = FakeNotion()
        self.notion.add_snapshot_data_source(PROJECTS, "projects")
        self.notion.add_snapshot_data_source(CONTACTS, "contacts")

    def contact(self, name: str, handle: str | None) -> str:
        props: dict[str, Any] = {ContactsRow.notion_name("name"): title(name)}
        if handle is not None:
            props[ContactsRow.notion_name("telegram_handle")] = rich_text(handle)
        return str(self.notion.add_page(CONTACTS, props)["id"])

    def project(self, *coordinators: str) -> str:
        return str(self.notion.add_page(PROJECTS, {
            ProjectsRow.notion_name("name"): title("Show A"),
            ProjectsRow.notion_name("prod_coor_supe"): relation(*coordinators),
        })["id"])

    def lookup(self, page: str | None) -> tuple[ReviewCoordinators, _Source]:
        source = _Source(self.notion.client)
        links = Links(page) if page else Links()
        return ReviewCoordinators(links, ProjectReader(source, PROJECTS), ContactReader(source, CONTACTS)), source

    def test_handles_come_from_the_linked_projects_coordinators_by_page_reads_only(self) -> None:
        a, b = self.contact("Coord A", "@coord_a"), self.contact("Coord B", "coord_b")
        lookup, source = self.lookup(self.project(a, b))
        self.assertEqual(lookup("fp1"), ("coord_a", "coord_b"))
        self.assertEqual({kind for _, kind in source.calls}, {"page"})
        self.assertTrue(all(freshness == "mirror" for freshness, _ in source.calls))

    def test_nothing_to_tag_gives_no_handles(self) -> None:
        no_handle = self.contact("Coord C", None)
        blank = self.contact("Coord D", "  ")
        for page in (self.project(), self.project(no_handle, blank), None):
            with self.subTest(page=page):
                self.assertEqual(self.lookup(page)[0]("fp1"), ())
        self.assertEqual(self.lookup(self.project())[0]("another-frameio-project-with-no-link"), ())

    def test_one_unreadable_contact_does_not_hide_the_others(self) -> None:
        a = self.contact("Coord A", "coord_a")
        lookup, _ = self.lookup(self.project("00000000-0000-0000-0000-000000000000", a, a))
        self.assertEqual(lookup("fp1"), ("coord_a",))


def job(**extra: Any) -> WorkerJob:
    payload = {"file_id": "f1", "frameio_project_id": "fp1", "user_name": "Ana Reyes", "count": 1,
               "first_at": 1700000000.0, "last_at": 1700000000.0, **extra}
    return WorkerJob(1, "main", "frameio.review_done", "k", payload, "lease", SEND_ANYWAY_AT_ATTEMPT)


class MessageTests(unittest.TestCase):
    def send(self, lookup: Any) -> str:
        gateway = FakeNotify()
        review = ReviewMessage(GatewayClient(gateway, "secret"), CHAT, None, lambda request: SimpleNamespace(),  # type: ignore[arg-type,return-value]
                               coordinators=lookup)
        review.notify(job())
        (text,) = gateway.texts()
        return text

    def test_the_tags_end_the_message_once_each_and_escaped(self) -> None:
        text = self.send(lambda project: ("coord_a", "coord_b", "coord_a", "x<y"))
        self.assertTrue(text.endswith("in total\n@coord_a @coord_b @x&lt;y"), text)

    def test_no_handles_or_a_failing_lookup_still_sends_without_a_tag(self) -> None:
        def failing(project: str) -> tuple[str, ...]:
            raise RuntimeError("mirror unreadable")

        for lookup in (None, lambda project: (), failing):
            with self.subTest(lookup=lookup):
                text = self.send(lookup)
                self.assertNotIn("@", text)
                self.assertTrue(text.endswith("in total"))

    def test_the_worker_looks_coordinators_up_only_with_the_notion_token_and_both_data_sources(self) -> None:
        from t6.worker.__main__ import review_coordinators

        full = {"T6_NOTION_TOKEN": "t", "T6_NOTION_PROJECTS_DATA_SOURCE": PROJECTS, "T6_NOTION_CONTACTS_DATA_SOURCE": CONTACTS}
        self.assertIsNotNone(review_coordinators(full, Links()))
        for missing in full:
            with self.subTest(missing=missing):
                self.assertIsNone(review_coordinators({k: v for k, v in full.items() if k != missing}, Links()))


if __name__ == "__main__":
    unittest.main()
