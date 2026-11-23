"""A guest's name from the public share page DOM, with the guest fallback whenever it is missing (#545)."""

import json
import unittest
from unittest import mock
import urllib.request

from t6.frameio.api import FrameioApi
from t6.frameio.model import GUEST_ID, GUEST_NAME, ResolvedComment
from t6.frameio.reads import FrameioReads
from t6.frameio.requests import ResolveComment
from t6.frameio.share_comments import COMMENTS_QUERY, share_guest_names
from t6.frameio.share_page import fetch_share_page, guest_name
from t6.frameio.testing import FakeFrameio, StaticTokens
from t6.frameio.tokens import USER_AGENT

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

    def test_the_page_is_fetched_as_the_t6_worker_not_as_python(self) -> None:
        # Frame.io answers Python's default User-Agent with a bare 403 page, so the name was never read (#545).
        sent: list[urllib.request.Request] = []

        class Response:
            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *exc: object) -> None:
                return None

            def geturl(self) -> str:
                return "https://f.io/s1"

            def read(self, limit: int) -> bytes:
                return b"<html></html>"

        def urlopen(request: urllib.request.Request, timeout: float) -> Response:
            sent.append(request)
            return Response()

        with mock.patch("urllib.request.urlopen", urlopen):
            self.assertEqual(fetch_share_page("https://f.io/s1"), "<html></html>")
        self.assertEqual(sent[0].get_header("User-agent"), USER_AGENT)
        self.assertEqual(sent[0].full_url, "https://f.io/s1")

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


class ShareGuestNamesTest(unittest.TestCase):
    """The guest's name from Frame.io's GraphQL share comments (#545); a passphrase share needs only a built header (#602)."""

    def setUp(self) -> None:
        self.posts: list[tuple[dict[str, object], dict[str, str]]] = []
        self.answers: list[object] = []

    def post(self, body: object, headers: object) -> object:
        self.posts.append((dict(body), dict(headers)))  # type: ignore[call-overload]
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    @staticmethod
    def comments(*rows: dict[str, object]) -> dict[str, object]:
        return {"data": {"share": {"asset": {"comments": {"result": list(rows)}}}}}

    def test_an_open_share_answers_names_by_comment_without_a_token(self) -> None:
        self.answers = [self.comments({"id": "c1", "owner": {"name": " Cyrene Lubigan "}}, {"id": "c2", "owner": None})]
        self.assertEqual(share_guest_names("s1", "f1", None, self.post), {"c1": "Cyrene Lubigan"})
        body, headers = self.posts[0]
        self.assertEqual((body["query"], body["variables"]), (COMMENTS_QUERY, {"shareId": "s1", "assetId": "f1"}))
        self.assertNotIn("x-frameio-share-authentication", headers)

    def test_a_passphrase_share_sends_base64_of_share_id_and_passphrase_in_one_call(self) -> None:
        self.answers = [self.comments({"id": "c1", "owner": {"name": "Ana"}})]
        self.assertEqual(share_guest_names("s1", "f1", "secret", self.post), {"c1": "Ana"})
        self.assertEqual(len(self.posts), 1)
        body, headers = self.posts[0]
        self.assertEqual(body["query"], COMMENTS_QUERY)
        self.assertEqual(headers["x-frameio-share-authentication"], "czE6c2VjcmV0")  # base64("s1:secret")
        self.assertEqual(headers["apollographql-client-name"], "web-app")
        self.assertEqual(headers["x-gql-op"], "GetShareCommentsByAsset")
        self.assertIn("apollographql-client-version", headers)

    def test_a_failed_unlock_or_call_or_odd_answer_is_no_names(self) -> None:
        for answers in (
            [{"errors": [{"message": "unknown field"}]}],
            [OSError("down")],
            [ValueError("not json")],
        ):
            self.answers = list(answers)
            self.assertEqual(share_guest_names("s1", "f1", "secret", self.post), {}, answers)
        for answer in ({"data": None}, {"data": {"share": {"asset": {"comments": {"result": "x"}}}}}, OSError("down")):
            self.answers = [answer]
            self.assertEqual(share_guest_names("s1", "f1", None, self.post), {}, answer)


class GuestNameFromShareCommentsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeFrameio()
        self.fake.add_project("fp1", "Project One")
        self.fake.add_file("f1", "Cut 1")
        self.fake.add_share("fp1", "s1", "Cut 1", file_ids=("f1",), created_at="2026-11-01T00:00:00Z", passphrase="kb-pass")
        self.fake.add_comment("f1", "c1", "text", author_id=None)
        self.asked: list[tuple[str, str, str | None]] = []
        self.names: dict[str, str] = {"c1": "Cyrene Lubigan"}

    def resolve(self, page_fetch: object = None) -> ResolvedComment:
        def names(share_id: str, file_id: str, passphrase: str | None) -> dict[str, str]:
            self.asked.append((share_id, file_id, passphrase))
            return self.names

        reads = FrameioReads(
            FrameioApi(StaticTokens(), self.fake.fetch), account_id=ACCOUNT, share_names=names, page_fetch=page_fetch  # type: ignore[arg-type]
        )
        return reads.resolve_comment(ResolveComment(comment_id="c1", account_id=ACCOUNT, frameio_project_id="fp1"))

    def test_the_name_comes_from_the_share_comments_with_the_share_passphrase(self) -> None:
        self.assertEqual(self.resolve().author_name, "Cyrene Lubigan")
        self.assertEqual(self.asked, [("s1", "f1", "kb-pass")])

    def test_no_name_there_falls_back_to_the_page_then_to_the_guest_name(self) -> None:
        self.names = {}
        self.assertEqual(self.resolve(lambda url: page({"id": "c1", "owner": {"name": "Guest Ana"}})).author_name, "Guest Ana")
        self.assertEqual(self.resolve().author_name, GUEST_NAME)


if __name__ == "__main__":
    unittest.main()
