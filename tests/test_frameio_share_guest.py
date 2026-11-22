"""A guest's name from the public share page DOM, with the guest fallback whenever it is missing (#545)."""

import json
import unittest

from t6.frameio.api import FrameioApi
from t6.frameio.model import GUEST_ID, GUEST_NAME, ResolvedComment
from t6.frameio.reads import FrameioReads
from t6.frameio.requests import ResolveComment
from t6.frameio.share_page import fetch_share_page, guest_name
from t6.frameio.testing import FakeFrameio, StaticTokens

ACCOUNT = "acc-1"


def page(*comments: dict[str, object], wrapper: str = "script") -> str:
    state = json.dumps({"props": {"pageProps": {"comments": list(comments)}}})
    return f'<html><body><div id="app"></div><{wrapper} id="__NEXT_DATA__" type="application/json">{state}</{wrapper}></body></html>'


class GuestNameParsingTest(unittest.TestCase):
    def test_the_name_of_the_comment_is_read_from_the_embedded_json(self) -> None:
        html = page({"id": "c1", "owner": {"display_name": "Guest Ana"}}, {"id": "c2", "author": {"name": "Guest Ben"}})
        self.assertEqual(guest_name(html, "c1"), "Guest Ana")
        self.assertEqual(guest_name(html, "c2"), "Guest Ben")

    def test_a_name_never_leaks_to_another_comment(self) -> None:
        self.assertIsNone(guest_name(page({"id": "c1", "owner": {"name": "Guest Ana"}}), "c9"))

    def test_html_without_a_name_or_json_is_no_name(self) -> None:
        for html in ("", "<html><body>nothing</body></html>", page({"id": "c1"}), '<script>{broken</script>', "<<<script"):
            self.assertIsNone(guest_name(html, "c1"), html)

    def test_the_name_is_cleaned_and_cut(self) -> None:
        html = page({"id": "c1", "owner": {"name": "  Guest\x00 Ana  " + "x" * 200}})
        name = guest_name(html, "c1")
        assert name is not None
        self.assertTrue(name.startswith("Guest Ana") and len(name) == 80 and "\x00" not in name)

    def test_only_frame_io_https_pages_are_fetched(self) -> None:
        for url in ("http://f.io/s1", "https://example.com/s1", "https://frame.io.example.com/s1", "file:///etc/passwd"):
            self.assertIsNone(fetch_share_page(url), url)


class GuestCommentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeFrameio()
        self.fake.add_project("fp1", "Project One")
        self.fake.add_file("f1", "Cut 1")
        self.fake.add_share("fp1", "s1", "Cut 1", file_ids=("f1",), created_at="2026-11-01T00:00:00Z")
        self.fake.add_comment("f1", "c1", "text", author_id=None)
        self.fetched: list[str] = []
        self.pages: dict[str, str | None] = {"https://f.io/s1": page({"id": "c1", "owner": {"name": "Guest Ana"}})}

    def resolve(self, *, fetch: bool = True) -> ResolvedComment:
        def read(url: str) -> str | None:
            self.fetched.append(url)
            page_ = self.pages[url]
            if page_ is None:
                raise OSError("down")
            return page_

        reads = FrameioReads(FrameioApi(StaticTokens(), self.fake.fetch), account_id=ACCOUNT, page_fetch=read if fetch else None)
        return reads.resolve_comment(ResolveComment(comment_id="c1", account_id=ACCOUNT, frameio_project_id="fp1"))

    def test_a_guest_comment_takes_the_name_on_the_share_page(self) -> None:
        found = self.resolve()
        self.assertEqual((found.author_id, found.author_name), (GUEST_ID, "Guest Ana"))  # the id stays shared, so one timer per file
        self.assertEqual(self.fetched, ["https://f.io/s1"])

    def test_no_page_reader_keeps_the_guest_name(self) -> None:
        self.assertEqual(self.resolve(fetch=False).author_name, GUEST_NAME)

    def test_a_failed_read_keeps_the_guest_name(self) -> None:
        self.pages["https://f.io/s1"] = None
        self.assertEqual(self.resolve().author_name, GUEST_NAME)

    def test_a_page_with_no_matching_name_keeps_the_guest_name(self) -> None:
        self.pages["https://f.io/s1"] = page({"id": "c7", "owner": {"name": "Guest Ben"}})
        self.assertEqual(self.resolve().author_name, GUEST_NAME)

    def test_a_file_in_no_live_share_reads_no_page(self) -> None:
        self.fake.shares["fp1"][0]["enabled"] = False
        self.assertEqual(self.resolve().author_name, GUEST_NAME)
        self.assertEqual(self.fetched, [])

    def test_a_share_without_the_file_reads_no_page(self) -> None:
        self.fake.share_assets["s1"] = ["f2"]
        self.assertEqual(self.resolve().author_name, GUEST_NAME)
        self.assertEqual(self.fetched, [])

    def test_a_comment_with_an_owner_reads_no_page(self) -> None:
        self.fake.add_comment("f1", "c2", "text", author="Reviewer One", author_id="u1")
        reads = FrameioReads(FrameioApi(StaticTokens(), self.fake.fetch), account_id=ACCOUNT, page_fetch=lambda url: self.fetched.append(url))
        found = reads.resolve_comment(ResolveComment(comment_id="c2", account_id=ACCOUNT, frameio_project_id="fp1"))
        self.assertEqual(found.author_name, "Reviewer One")
        self.assertEqual(self.fetched, [])


if __name__ == "__main__":
    unittest.main()
