"""Which shares hold a file: the assets of each live share, newest first, cached (#297).

Frame.io's Share object has no list of its files. The files come from `GET /shares/{id}/assets`.
"""

import unittest

from t6.frameio.api import FrameioApi, FrameioError
from t6.frameio.reads import FrameioReads
from t6.frameio.testing import FakeFrameio, StaticTokens

ACCOUNT = "acc-1"
PROJECT = "fp1"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class ShareFilesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeFrameio(page_size=100)
        self.fake.add_project(PROJECT, "Example Show")
        self.fake.add_file("f1", "Cut 1")
        self.fake.add_file("f2", "Cut 2")
        self.clock = Clock()
        self.reads = self.make()

    def make(self, **settings: float) -> FrameioReads:
        return FrameioReads(FrameioApi(StaticTokens(), self.fake.fetch), account_id=ACCOUNT, clock=self.clock, **settings)  # type: ignore[arg-type]

    def asset_reads(self) -> list[str]:
        return [path for path in self.fake.requests if path.endswith("/assets")]

    def test_the_files_of_a_live_share_come_from_its_assets(self) -> None:
        self.fake.add_share(PROJECT, "s1", "Cut 1", file_ids=("f1", "f2"))
        (share,) = self.reads.share_links(PROJECT)
        self.assertEqual(share.file_ids, ("f1", "f2"))
        self.assertTrue(share.files_known)

    def test_a_version_stack_adds_its_head_version(self) -> None:
        self.fake.add_version_stack("vs1", ["f1", "f2"])
        self.fake.add_share(PROJECT, "s1", "Cut", file_ids=("vs1",))
        (share,) = self.reads.share_links(PROJECT)
        self.assertEqual(share.file_ids, ("vs1", "f2"))  # the stack, then its newest file

    def test_a_folder_is_listed_as_the_folder(self) -> None:
        self.fake.add_share(PROJECT, "s1", "Folder share", file_ids=("folder-9",))
        (share,) = self.reads.share_links(PROJECT)
        self.assertEqual(share.file_ids, ("folder-9",))

    def test_only_live_shares_have_their_assets_read(self) -> None:
        self.fake.add_share(PROJECT, "s-live", "a", file_ids=("f1",))
        self.fake.add_share(PROJECT, "s-off", "b", enabled=False, file_ids=("f1",))
        self.fake.add_share(PROJECT, "s-expired", "c", expiration="2020-01-01T00:00:00Z", file_ids=("f1",))
        shares = {s.id: s for s in self.reads.share_links(PROJECT)}
        self.assertEqual(self.asset_reads(), [f"/v4/accounts/{ACCOUNT}/shares/s-live/assets"])
        self.assertEqual(shares["s-off"].file_ids, ())
        self.assertTrue(shares["s-off"].files_known)  # not live: nothing to know

    def test_only_the_newest_live_shares_are_scanned(self) -> None:
        reads = self.make(share_scan_limit=2)
        for index, created in enumerate(("2026-11-01T00:00:00Z", "2026-11-03T00:00:00Z", "2026-11-02T00:00:00Z")):
            self.fake.add_share(PROJECT, f"s{index}", "x", file_ids=("f1",), created_at=created)
        shares = {s.id: s for s in reads.share_links(PROJECT)}
        self.assertFalse(shares["s0"].files_known)  # the oldest of three, past the limit of two
        self.assertEqual(shares["s0"].file_ids, ())
        self.assertTrue(shares["s1"].files_known and shares["s2"].files_known)
        self.assertEqual(len(self.asset_reads()), 2)

    def test_a_scan_that_runs_out_of_time_stops_and_the_next_lookup_carries_on(self) -> None:
        for index, created in enumerate(("2026-11-01T00:00:00Z", "2026-11-03T00:00:00Z", "2026-11-02T00:00:00Z")):
            self.fake.add_share(PROJECT, f"s{index}", "x", file_ids=("f1",), created_at=created)
        real_fetch = self.fake.fetch

        def slow_fetch(url, headers):  # type: ignore[no-untyped-def]
            if "/assets" in url:
                self.clock.now += 6  # each asset read takes 6 s
            return real_fetch(url, headers)

        self.fake.fetch = slow_fetch  # type: ignore[method-assign]
        reads = self.make(share_scan_budget_seconds=10)
        shares = {s.id: s for s in reads.share_links(PROJECT)}
        self.assertTrue(shares["s1"].files_known and shares["s2"].files_known)  # the two newest were read (6 s, 12 s)
        self.assertFalse(shares["s0"].files_known)  # the oldest was left: the 10 s budget was spent
        self.assertEqual(len(self.asset_reads()), 2)
        shares = {s.id: s for s in reads.share_links(PROJECT)}  # the two read shares are cached, so there is time for the third
        self.assertTrue(shares["s0"].files_known)
        self.assertEqual(len(self.asset_reads()), 3)

    def test_a_second_lookup_inside_the_cache_time_makes_no_call(self) -> None:
        reads = self.make(share_cache_seconds=3600)
        self.fake.add_share(PROJECT, "s1", "a", file_ids=("f1",))
        reads.share_links(PROJECT)
        made = len(self.fake.requests)
        self.clock.now += 3599
        (share,) = reads.share_links(PROJECT)
        self.assertEqual(len(self.fake.requests), made)
        self.assertEqual(share.file_ids, ("f1",))

    def test_after_the_cache_time_it_reads_again(self) -> None:
        reads = self.make(share_cache_seconds=3600)
        self.fake.add_share(PROJECT, "s1", "a", file_ids=("f1",))
        reads.share_links(PROJECT)
        made = len(self.fake.requests)
        self.clock.now += 3601
        reads.share_links(PROJECT)
        self.assertGreater(len(self.fake.requests), made)

    def test_a_cache_time_of_zero_turns_the_cache_off(self) -> None:
        reads = self.make(share_cache_seconds=0)
        self.fake.add_share(PROJECT, "s1", "a", file_ids=("f1",))
        reads.share_links(PROJECT)
        made = len(self.fake.requests)
        reads.share_links(PROJECT)
        self.assertEqual(len(self.fake.requests), 2 * made)

    def test_a_failed_read_is_not_cached(self) -> None:
        reads = self.make(share_cache_seconds=3600)
        self.fake.add_share(PROJECT, "s1", "a", file_ids=("f1",))
        reads.share_links(PROJECT)  # warm the share list; the assets are cached too
        reads = self.make(share_cache_seconds=3600)  # a new worker: empty cache
        self.fake.refuse_next(500)
        with self.assertRaises(FrameioError):
            reads.share_links(PROJECT)  # the share list read fails
        (share,) = reads.share_links(PROJECT)  # the next call reads again and works
        self.assertEqual(share.file_ids, ("f1",))

    def test_a_share_that_is_gone_holds_nothing(self) -> None:
        self.fake.add_share(PROJECT, "s1", "a", file_ids=("f1",))
        del self.fake.share_assets["s1"]  # Frame.io answers 404 for its assets
        (share,) = self.reads.share_links(PROJECT)
        self.assertEqual(share.file_ids, ())

    def test_the_share_list_alone_reads_no_assets(self) -> None:
        self.fake.add_share(PROJECT, "s1", "a", file_ids=("f1",))
        shares = self.reads.share_links(PROJECT, with_files=False)
        self.assertEqual(self.asset_reads(), [])
        self.assertEqual(shares[0].file_ids, ())


if __name__ == "__main__":
    unittest.main()


class ShareSettingsTest(unittest.TestCase):
    """`T6_FRAMEIO_SHARE_CACHE_SECONDS` and `T6_FRAMEIO_SHARE_SCAN_LIMIT` on the Frame.io worker."""

    def test_the_defaults_are_sixty_minutes_and_thirty_shares(self) -> None:
        from t6.frameio.reads import SHARE_CACHE_SECONDS, SHARE_SCAN_LIMIT

        self.assertEqual(SHARE_CACHE_SECONDS, 3600.0)
        self.assertEqual(SHARE_SCAN_LIMIT, 30)

    def test_settings_are_read_and_bad_values_refused(self) -> None:
        from t6.worker.__main__ import ConfigError, _number_setting

        read = _number_setting
        self.assertEqual(read({}, "X", 3600.0, minimum=0), 3600.0)
        self.assertEqual(read({"X": " "}, "X", 3600.0, minimum=0), 3600.0)
        self.assertEqual(read({"X": "0"}, "X", 3600.0, minimum=0), 0.0)  # the cache off
        self.assertEqual(read({"X": "12"}, "X", 30, minimum=1), 12.0)
        for bad in ("abc", "-1", "nan", "inf"):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                read({"X": bad}, "X", 3600.0, minimum=0)
        with self.assertRaises(ConfigError):
            read({"X": "0"}, "X", 30, minimum=1)  # a scan limit of 0 is refused


class ReviewAskTimeTest(unittest.TestCase):
    """The review message waits longer for the share links, and the leases cover the waits (#301)."""

    def test_the_share_links_get_a_longer_wait_than_the_other_reads(self) -> None:
        from t6.frameio.requests import Comments, FileInfo, ShareLinks
        from t6.worker.__main__ import REVIEW_ASK_SECONDS, SHARE_ASK_SECONDS, review_ask_seconds

        self.assertEqual(review_ask_seconds(ShareLinks(frameio_project_id="fp1")), SHARE_ASK_SECONDS)
        self.assertEqual(review_ask_seconds(FileInfo(file_id="f1")), REVIEW_ASK_SECONDS)
        self.assertEqual(review_ask_seconds(Comments(file_id="f1")), REVIEW_ASK_SECONDS)
        self.assertGreater(SHARE_ASK_SECONDS, REVIEW_ASK_SECONDS)

    def test_the_main_lease_covers_the_three_asks_of_one_review_message(self) -> None:
        from t6.worker.__main__ import MAIN_LEASE_SECONDS, REVIEW_ASK_SECONDS, SHARE_ASK_SECONDS

        self.assertGreater(MAIN_LEASE_SECONDS, 2 * REVIEW_ASK_SECONDS + SHARE_ASK_SECONDS)

    def test_the_frameio_lease_covers_a_cold_share_lookup(self) -> None:
        from t6.worker.__main__ import FRAMEIO_LEASE_SECONDS, SHARE_ASK_SECONDS

        self.assertGreaterEqual(FRAMEIO_LEASE_SECONDS, 2 * SHARE_ASK_SECONDS)
