"""Frame.io reads and the briefing feed on a fake Frame.io API and the fake Notion. No real client data."""

from datetime import datetime, timezone
import unittest

from t6.frameio import (
    Approvals,
    BriefingFeed,
    Comments,
    CreateProjectFolders,
    FrameioProjects,
    ShareLinks,
    SignInRequired,
    Versions,
)
from t6.frameio.api import RATE_LIMIT_TRIES, FrameioApi, FrameioError
from t6.frameio.model import Approval, BriefingShow, Comment, FrameioProject, ShareLink, Version, decode
from t6.frameio.reads import FrameioReads
from t6.frameio.testing import FakeFrameio, StaticTokens
from t6.notion.external_links import ExternalLinks
from t6.notion.testing import FakeNotion, external_link_properties

LINKS = "ds-external-links"
NOW = datetime(2026, 11, 8, 8, 0, tzinfo=timezone.utc)
SINCE = datetime(2026, 11, 1, 8, 0, tzinfo=timezone.utc)
BEFORE, AFTER = "2026-10-30T10:00:00Z", "2026-11-03T10:00:00Z"


class ReadsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.frameio = FakeFrameio()
        self.tokens = StaticTokens()
        self.waits: list[float] = []
        self.api = FrameioApi(self.tokens, self.frameio.fetch, self.waits.append)
        self.notion = FakeNotion()
        self.notion.add_data_source(LINKS)
        client = self.notion.client()
        self.addCleanup(client.close)
        self.links = ExternalLinks(client, LINKS)
        self.reads = FrameioReads(self.api, self.links, now=lambda: NOW)

    def rows(self, kind: str = "share_link") -> dict[str, tuple[str, ...]]:
        return {row.ref.external_id: row.project_ids for row in self.links.links_for_system("frameio", kind)}


class ReadTests(ReadsTestCase):
    def test_projects_are_typed_and_every_page_is_read(self) -> None:
        for number in range(1, 6):
            self.frameio.add_project(f"fp{number}", f"Example Show {number}")
        projects = decode(FrameioProjects, self.reads(FrameioProjects()))
        self.assertEqual([project.id for project in projects], ["fp1", "fp2", "fp3", "fp4", "fp5"])
        self.assertIsInstance(projects[0], FrameioProject)
        self.assertEqual(projects[0].name, "Example Show 1")
        self.assertEqual(projects[0].updated_at, datetime(2026, 11, 1, tzinfo=timezone.utc))

    def test_share_links_are_typed_and_know_whether_they_are_live(self) -> None:
        self.frameio.add_project("fp1", "Example Show")
        self.frameio.add_share("fp1", "s-live", "Cut 1", last_viewed_at=AFTER)
        self.frameio.add_share("fp1", "s-off", "Cut 2", enabled=False)
        self.frameio.add_share("fp1", "s-expired", "Cut 3", expiration="2026-11-08T07:59:59Z")
        self.frameio.add_share("fp1", "s-expiring", "Cut 4", expiration="2026-11-08T08:00:01Z")
        links = decode(ShareLinks, self.reads(ShareLinks(frameio_project_id="fp1")))
        self.assertIsInstance(links[0], ShareLink)
        self.assertEqual(links[0].url, "https://f.io/s-live")
        self.assertEqual(links[0].frameio_project_id, "fp1")
        self.assertEqual(links[0].last_viewed_at, datetime(2026, 11, 3, 10, tzinfo=timezone.utc))
        self.assertEqual({link.id: link.live(NOW) for link in links},
                         {"s-live": True, "s-off": False, "s-expired": False, "s-expiring": True})

    def test_comments_are_typed(self) -> None:
        self.frameio.add_file("f1", "cut-v1.mov")
        self.frameio.add_comment("f1", "c1", "Trim the open.")
        self.frameio.add_comment("f1", "c2", "Louder here.")
        self.frameio.add_comment("f1", "c3", "Approved.")
        comments = decode(Comments, self.reads(Comments(file_id="f1")))
        self.assertEqual([comment.text for comment in comments], ["Trim the open.", "Louder here.", "Approved."])
        self.assertEqual(
            comments[0],
            Comment(id="c1", file_id="f1", text="Trim the open.", author="Reviewer One",
                    created_at=datetime(2026, 11, 2, 9, tzinfo=timezone.utc)),
        )

    def test_versions_are_the_files_of_the_stack_the_file_is_in(self) -> None:
        for number in (1, 2, 3):
            self.frameio.add_file(f"f{number}", f"cut-v{number}.mov")
        self.frameio.add_version_stack("stack-1", ["f1", "f2", "f3"])
        versions = decode(Versions, self.reads(Versions(file_id="f2")))
        self.assertEqual([version.file_id for version in versions], ["f1", "f2", "f3"])
        self.assertIsInstance(versions[0], Version)
        self.assertEqual(versions[2].name, "cut-v3.mov")

    def test_a_file_outside_a_stack_is_its_own_only_version(self) -> None:
        self.frameio.add_file("f1", "cut-v1.mov")
        versions = decode(Versions, self.reads(Versions(file_id="f1")))
        self.assertEqual([(version.file_id, version.name) for version in versions], [("f1", "cut-v1.mov")])

    def test_approvals_are_the_files_status_field(self) -> None:
        self.frameio.add_file("f1", "cut-v1.mov")
        self.frameio.add_file("f2", "cut-v2.mov")
        self.frameio.set_status("f1", "Approved")
        self.assertEqual(
            decode(Approvals, self.reads(Approvals(file_id="f1"))),
            (Approval(file_id="f1", field="Status", values=("Approved",)),),
        )
        self.assertEqual(decode(Approvals, self.reads(Approvals(file_id="f2"))), ())

    def test_an_id_cannot_name_another_route(self) -> None:
        self.frameio.add_project("fp1", "Example Show")
        with self.assertRaises(FrameioError):
            self.reads(ShareLinks(frameio_project_id="fp1/shares/../.."))
        self.assertNotIn("..", self.frameio.requests[-1].split("/"))

    def test_the_folder_step_is_not_a_read(self) -> None:
        with self.assertRaisesRegex(NotImplementedError, "t6-core#23"):
            self.reads(CreateProjectFolders(project_id="t6p_1", name="Example Show"))

    def test_a_sign_in_that_sees_two_accounts_needs_the_account_named(self) -> None:
        self.frameio.accounts.append({"id": "acc-2"})
        with self.assertRaisesRegex(RuntimeError, "T6_FRAMEIO_ACCOUNT_ID"):
            self.reads(FrameioProjects())
        self.frameio.add_project("fp1", "Example Show")
        named = FrameioReads(self.api, account_id="acc-1")
        self.assertEqual(len(decode(FrameioProjects, named(FrameioProjects()))), 1)

    def test_an_answer_that_is_not_the_requests_result_does_not_decode(self) -> None:
        for value in ({"answered": "x"}, [{"name": "no id"}], [7]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                decode(FrameioProjects, value)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            decode(CreateProjectFolders, [])  # type: ignore[arg-type]


class ApiTests(ReadsTestCase):
    def test_a_refused_token_is_refreshed_once_and_the_request_tried_again(self) -> None:
        self.frameio.add_project("fp1", "Example Show")
        self.frameio.refuse_next(401)
        self.assertEqual(len(self.reads.projects()), 1)
        self.assertEqual(self.tokens.refreshes, 1)
        self.assertEqual(self.frameio.tokens_seen[:2], ["Bearer access-0", "Bearer access-1"])

    def test_a_fresh_token_that_is_refused_too_asks_for_sign_in(self) -> None:
        self.frameio.refuse_next(401, times=2)
        with self.assertRaises(SignInRequired):
            self.reads.projects()
        self.assertEqual(self.tokens.refreshes, 1)

    def test_a_rate_limit_waits_and_tries_again(self) -> None:
        self.frameio.add_project("fp1", "Example Show")
        self.frameio.refuse_next(429, {"Retry-After": "7"})
        self.frameio.refuse_next(429)
        self.assertEqual(len(self.reads.projects()), 1)
        self.assertEqual(self.waits, [7.0, 15.0])

    def test_a_rate_limit_that_does_not_lift_is_an_error_not_a_loop(self) -> None:
        self.frameio.refuse_next(429, times=RATE_LIMIT_TRIES + 1)
        with self.assertRaisesRegex(FrameioError, "429"):
            self.reads.projects()
        self.assertEqual(len(self.waits), RATE_LIMIT_TRIES)

    def test_a_frameio_error_names_the_status_and_no_token(self) -> None:
        self.frameio.refuse_next(500)
        with self.assertRaises(FrameioError) as raised:
            self.reads.projects()
        self.assertEqual(raised.exception.status, 500)
        self.assertNotIn("access-", str(raised.exception))


class BriefingFeedTests(ReadsTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.frameio.add_project("fp1", "Example Show")
        self.frameio.add_project("fp2", "Second Show")
        self.frameio.add_project("fp3", "Quiet Show")
        self.notion.add_page(
            LINKS, external_link_properties(system="frameio", kind="project", external_id="fp1", projects=["show-1"])
        )

    def feed(self, since: datetime = SINCE) -> tuple[BriefingShow, ...]:
        return decode(BriefingFeed, self.reads(BriefingFeed(since=since)))

    def test_only_live_links_with_activity_after_since_are_returned_grouped_per_show(self) -> None:
        self.frameio.add_share("fp1", "s-viewed", "Cut 1", last_viewed_at=AFTER)
        self.frameio.add_share("fp1", "s-stale", "Cut 2", last_viewed_at=BEFORE)
        self.frameio.add_share("fp1", "s-never", "Cut 3")
        self.frameio.add_share("fp1", "s-off", "Cut 4", enabled=False, last_viewed_at=AFTER)
        self.frameio.add_share("fp1", "s-expired", "Cut 5", expiration="2026-11-07T00:00:00Z", last_viewed_at=AFTER)
        self.frameio.add_share("fp2", "s-commented", "Trailer")
        self.frameio.add_activity("s-commented", BEFORE)
        self.frameio.add_activity("s-commented", "2026-11-05T12:00:00Z", "comment_created")
        self.frameio.add_activity("s-commented", AFTER)
        self.frameio.add_share("fp3", "s-quiet", "Old cut", last_viewed_at=BEFORE)
        shows = self.feed()
        self.assertEqual([show.frameio_project_id for show in shows], ["fp1", "fp2"])
        first, second = shows
        self.assertEqual((first.frameio_project_name, first.project_page_ids), ("Example Show", ("show-1",)))
        self.assertEqual([(link.share_id, link.url) for link in first.links], [("s-viewed", "https://f.io/s-viewed")])
        self.assertEqual(second.project_page_ids, ())
        self.assertEqual([link.share_id for link in second.links], ["s-commented"])
        self.assertEqual(second.links[0].last_activity, datetime(2026, 11, 5, 12, tzinfo=timezone.utc))

    def test_activity_exactly_at_since_was_in_the_last_briefing(self) -> None:
        self.frameio.add_share("fp1", "s1", "Cut 1", last_viewed_at="2026-11-01T08:00:00Z")
        self.assertEqual(self.feed(), ())

    def test_every_live_link_gets_one_external_links_row_and_a_second_run_adds_none(self) -> None:
        self.frameio.add_share("fp1", "s-viewed", "Cut 1", last_viewed_at=AFTER)
        self.frameio.add_share("fp1", "s-never", "Cut 2")
        self.frameio.add_share("fp1", "s-off", "Cut 3", enabled=False)
        self.frameio.add_share("fp2", "s-unlinked", "Trailer")
        self.feed()
        self.assertEqual(self.rows(), {"s-viewed": ("show-1",), "s-never": ("show-1",), "s-unlinked": ()})
        pages = self.notion.page_count()
        self.feed()
        self.assertEqual(self.notion.page_count(), pages)

    def test_a_link_that_already_has_a_row_keeps_it_and_its_active_box_is_reported_not_written(self) -> None:
        self.frameio.add_share("fp1", "s1", "Cut 1", last_viewed_at=AFTER)
        self.frameio.add_share("fp1", "s2", "Cut 2", last_viewed_at=AFTER)
        self.notion.add_page(
            LINKS, external_link_properties(system="frameio", kind="share_link", external_id="s1", active=True)
        )
        links = {link.share_id: link.active for link in self.feed()[0].links}
        self.assertEqual(links, {"s1": True, "s2": False})
        self.assertEqual(len(self.rows()), 2)
        self.assertFalse(any(b'"Active"' in request.content for request in self.notion.requests))
        self.assertTrue(self.links.links_for_system("frameio", "share_link")[0].active)

    def test_last_activity_on_the_row_moves_forward_only(self) -> None:
        self.frameio.add_share("fp1", "s1", "Cut 1", last_viewed_at=AFTER)
        self.feed()
        (row,) = self.links.links_for_system("frameio", "share_link")
        self.assertEqual(row.last_activity, datetime(2026, 11, 3, 10, tzinfo=timezone.utc))
        patches = sum(request.method == "PATCH" for request in self.notion.requests)
        self.feed()
        self.assertEqual(sum(request.method == "PATCH" for request in self.notion.requests), patches)

    def test_without_external_links_the_feed_fails_and_the_reads_still_work(self) -> None:
        reads = FrameioReads(self.api, now=lambda: NOW)
        with self.assertRaisesRegex(RuntimeError, "T6_NOTION_EXTERNAL_LINKS_DATA_SOURCE"):
            reads(BriefingFeed(since=SINCE))
        self.assertEqual(len(decode(FrameioProjects, reads(FrameioProjects()))), 3)


if __name__ == "__main__":
    unittest.main()
