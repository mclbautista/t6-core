"""How much of a clip is left after a comment: the file's Duration and Frame Rate, and the comment's frame (#297)."""

import unittest

from t6.frameio.api import FrameioApi
from t6.frameio.model import clip_remaining_seconds, format_timecode
from t6.frameio.reads import FrameioReads
from t6.frameio.requests import ResolveComment
from t6.frameio.testing import FakeFrameio, StaticTokens

ACCOUNT = "acc-1"
CLIP_SECONDS = 166.792
FRAME_RATE = 23.976
FRAME = 2108  # 1:27.9 into the clip


class RemainingSecondsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeFrameio()
        self.fake.add_file("f1", "Cut 1")
        self.reads = FrameioReads(FrameioApi(StaticTokens(), self.fake.fetch), account_id=ACCOUNT)

    def resolve(self, comment_id: str = "c1"):  # type: ignore[no-untyped-def]
        return self.reads.resolve_comment(
            ResolveComment(comment_id=comment_id, account_id=ACCOUNT, frameio_project_id="fp1")
        )

    def test_the_rest_of_the_clip_after_the_comment(self) -> None:
        self.fake.set_clip("f1", CLIP_SECONDS, FRAME_RATE)
        self.fake.add_comment("f1", "c1", "text", timestamp=FRAME)
        found = self.resolve()
        self.assertEqual(found.frame, FRAME)
        assert found.remaining_seconds is not None
        self.assertAlmostEqual(found.remaining_seconds, 166.792 - 2108 / 23.976, places=3)  # 78.87 s
        self.assertEqual(found.comment_timecode, "01:27")
        self.assertEqual(found.total_timecode, "02:46")

    def test_the_numbers_may_come_as_text(self) -> None:
        self.fake.metadata["f1"] = [
            {"field_definition_name": "Duration", "value": "100"},
            {"field_definition_name": "Frame Rate", "value": "25"},
        ]
        self.fake.add_comment("f1", "c1", "text", timestamp=500)
        found = self.resolve()
        self.assertAlmostEqual(found.remaining_seconds or 0, 80.0)  # 100 s - 500 frames at 25 fps
        self.assertEqual(found.comment_timecode, "00:20")
        self.assertEqual(found.total_timecode, "01:40")

    def test_a_comment_past_the_end_leaves_nothing(self) -> None:
        self.assertEqual(clip_remaining_seconds(10.0, 25.0, 1000), 0.0)

    def test_a_comment_at_the_very_start_leaves_the_whole_clip(self) -> None:
        self.fake.set_clip("f1", CLIP_SECONDS, FRAME_RATE)
        self.fake.add_comment("f1", "c1", "text", timestamp=0)
        found = self.resolve()
        self.assertAlmostEqual(found.remaining_seconds or 0, CLIP_SECONDS)
        self.assertEqual(found.comment_timecode, "00:00")
        self.assertEqual(found.total_timecode, "02:46")

    def test_a_file_with_no_duration_leaves_it_unknown(self) -> None:
        self.fake.metadata["f1"] = [{"field_definition_name": "Frame Rate", "value": FRAME_RATE}]
        self.fake.add_comment("f1", "c1", "text", timestamp=FRAME)
        found = self.resolve()
        self.assertIsNone(found.remaining_seconds)
        self.assertEqual(found.comment_timecode, "01:27")
        self.assertIsNone(found.total_timecode)

    def test_a_file_with_no_frame_rate_leaves_it_unknown(self) -> None:
        self.fake.metadata["f1"] = [{"field_definition_name": "Duration", "value": CLIP_SECONDS}]
        self.fake.add_comment("f1", "c1", "text", timestamp=FRAME)
        found = self.resolve()
        self.assertIsNone(found.remaining_seconds)
        self.assertIsNone(found.comment_timecode)
        self.assertEqual(found.total_timecode, "02:46")

    def test_a_comment_with_no_position_leaves_it_unknown(self) -> None:
        self.fake.set_clip("f1", CLIP_SECONDS, FRAME_RATE)
        self.fake.add_comment("f1", "c1", "text")  # no timestamp (a comment on an image)
        found = self.resolve()
        self.assertIsNone(found.frame)
        self.assertIsNone(found.remaining_seconds)
        self.assertIsNone(found.comment_timecode)
        self.assertEqual(found.total_timecode, "02:46")

    def test_a_metadata_read_that_fails_does_not_refuse_the_comment(self) -> None:
        self.fake.add_comment("gone", "c1", "text", timestamp=FRAME)  # the file is not in the fake: metadata is a 404
        found = self.resolve()
        self.assertEqual(found.file_id, "gone")
        self.assertIsNone(found.remaining_seconds)
        self.assertIsNone(found.comment_timecode)
        self.assertIsNone(found.total_timecode)

    def test_zero_or_nonsense_clip_numbers_are_unknown(self) -> None:
        for duration, rate in ((0, 25), (100, 0), ("long", 25), (100, True)):
            with self.subTest(duration=duration, rate=rate):
                self.fake.metadata["f1"] = [
                    {"field_definition_name": "Duration", "value": duration},
                    {"field_definition_name": "Frame Rate", "value": rate},
                ]
                self.fake.comments.clear()
                self.fake.add_comment("f1", "c1", "text", timestamp=FRAME)
                self.assertIsNone(self.resolve().remaining_seconds)


class TimecodeFormattingTest(unittest.TestCase):
    def test_formats_mm_ss_and_hh_mm_ss_and_edge_cases(self) -> None:
        cases = (
            (0.0, "00:00"),
            (-3.5, "00:00"),
            (float("nan"), "00:00"),
            (float("inf"), "00:00"),
            (59.9999999999, "01:00"),
            (87.92, "01:27"),
            (3599.0, "59:59"),
            (3600.0, "01:00:00"),
            (3661.4, "01:01:01"),
            (2108 / 23.976, "01:27"),
            (1800 / 29.97, "01:00"),
            (3600 / 59.94, "01:00"),
        )
        for seconds, want in cases:
            with self.subTest(seconds=seconds):
                self.assertEqual(format_timecode(seconds), want)


if __name__ == "__main__":
    unittest.main()
