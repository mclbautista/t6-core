"""A guest's name from the share viewer's GraphQL comment list, with the guest fallback whenever it is missing (#601)."""

import json
from typing import Any
import unittest
from unittest import mock

from t6.frameio.api import FrameioApi
from t6.frameio.model import GUEST_ID, GUEST_NAME
from t6.frameio.reads import FrameioReads
from t6.frameio.requests import ResolveComment
from t6.frameio.share_comments import share_guest_name
from t6.frameio.share_page import fetch_share_page
from t6.frameio.testing import FakeFrameio, StaticTokens

ACCOUNT = "acc-1"


def answer(*comments: dict[str, Any]) -> dict[str, Any]:
    return {"data": {"share": {"id": "s1", "asset": {"id": "f1", "comments": {"result": list(comments)}}}}}


def comment(cid: str, name: object = None, typename: str = "IdentifiedUser") -> dict[str, Any]:
    return {"id": cid, "owner": None if name is None else {"__typename": typename, "name": name}}


class ShareGuestNameTest(unittest.TestCase):
    def read(self, post_answer: Any, comment_id: str = "c1") -> str | None:
        return share_guest_name(lambda headers, body: post_answer, "tok", "s1", "f1", comment_id)

    def test_the_name_is_the_owner_of_that_comment(self) -> None:
        self.assertEqual(self.read(answer(comment("c1", "Guest Ana"), comment("c2", "Guest Ben")), "c2"), "Guest Ben")

    def test_a_name_never_leaks_to_another_comment(self) -> None:
        self.assertIsNone(self.read(answer(comment("c1", "Guest Ana")), "c9"))

    def test_anything_else_is_no_name(self) -> None:
        cases: tuple[Any, ...] = (None, {}, {"data": None}, {"errors": [{"message": "x"}]}, answer(), answer(comment("c1")),
                      answer(comment("c1", 7)), answer(comment("c1", "  ")), {"data": {"share": {"asset": []}}})
        for given in cases:
            self.assertIsNone(self.read(given), given)

    def test_the_name_is_cleaned_and_cut(self) -> None:
        name = self.read(answer(comment("c1", "  Guest\x00 Ana  " + "x" * 200)))
        assert name is not None
        self.assertTrue(name.startswith("Guest Ana") and len(name) == 80 and "\x00" not in name)

    def test_a_failing_post_is_left_to_the_caller(self) -> None:
        def boom(headers: Any, body: bytes) -> Any:
            raise OSError("down")

        self.assertRaises(OSError, share_guest_name, boom, "tok", "s1", "f1", "c1")  # the caller owns the fallback

    def test_the_call_carries_the_share_token_and_the_ids(self) -> None:
        seen: dict[str, Any] = {}

        def post(headers: Any, body: bytes) -> Any:
            seen.update(headers=dict(headers), body=json.loads(body))
            return None

        share_guest_name(post, "tok", "s1", "f1", "c1")
        self.assertEqual(seen["headers"]["x-frameio-share-authentication"], "tok")
        self.assertEqual(seen["body"]["operationName"], "GetShareCommentsByAsset")
        self.assertEqual(seen["body"]["variables"], {"shareId": "s1", "assetId": "f1"})


class UserAgentTest(unittest.TestCase):
    def test_the_share_page_is_fetched_with_a_descriptive_user_agent(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.geturl.return_value = "https://next.frame.io/share/s1/"
        response.read.return_value = b"<html></html>"
        with mock.patch("urllib.request.urlopen", return_value=response) as opened:
            self.assertEqual(fetch_share_page("https://f.io/s1"), "<html></html>")
        self.assertEqual(opened.call_args.args[0].get_header("User-agent"), "t6-frameio-worker")


class GuestCommentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeFrameio()
        self.fake.add_project("fp1", "Project One")
        self.fake.add_file("f1", "Cut 1")
        self.fake.add_share("fp1", "s1", "Cut 1", file_ids=("f1",), created_at="2026-11-01T00:00:00Z")
        self.fake.add_comment("f1", "c1", "text", author_id=None)
        self.asked: list[tuple[str, str, str]] = []

    def resolve(self, share_name: Any) -> Any:
        reads = FrameioReads(FrameioApi(StaticTokens(), self.fake.fetch), account_id=ACCOUNT, share_name=share_name)
        return reads.resolve_comment(ResolveComment(comment_id="c1", account_id=ACCOUNT, frameio_project_id="fp1"))

    def test_a_guest_comment_takes_the_name_of_the_share_viewer(self) -> None:
        def share_name(share_id: str, file_id: str, comment_id: str) -> str | None:
            self.asked.append((share_id, file_id, comment_id))
            return "Guest Ana"

        found = self.resolve(share_name)
        self.assertEqual((found.author_id, found.author_name), (GUEST_ID, "Guest Ana"))
        self.assertEqual(self.asked, [("s1", "f1", "c1")])

    def test_no_name_or_a_failing_reader_keeps_the_guest_name(self) -> None:
        def boom(share_id: str, file_id: str, comment_id: str) -> str | None:
            raise RuntimeError("down")

        self.assertEqual(self.resolve(lambda *_: None).author_name, GUEST_NAME)
        self.assertEqual(self.resolve(boom).author_name, GUEST_NAME)

    def test_a_share_without_the_file_is_not_asked(self) -> None:
        self.fake.share_assets["s1"] = ["f2"]
        self.assertEqual(self.resolve(lambda *a: self.asked.append(a)).author_name, GUEST_NAME)
        self.assertEqual(self.asked, [])


if __name__ == "__main__":
    unittest.main()
