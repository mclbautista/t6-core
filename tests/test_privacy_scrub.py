"""Exercise t6.privacy.Scrubber and ScrubbingFormatter through their public interface."""

import io
import logging
import unittest

from t6.privacy import REDACTED, Scrubber, ScrubbingFormatter


class ScrubberTests(unittest.TestCase):
    def test_removes_every_occurrence(self) -> None:
        text = Scrubber(["tok-123"]).scrub("a tok-123 b tok-123")
        self.assertEqual(text, f"a {REDACTED} b {REDACTED}")

    def test_empty_values_are_ignored(self) -> None:
        self.assertEqual(Scrubber(["", "k"]).scrub("keep"), f"{REDACTED}eep")
        self.assertEqual(Scrubber([""]).scrub("keep"), "keep")

    def test_overlapping_secrets_remove_the_longer_whole(self) -> None:
        text = Scrubber(["abc", "abcdef"]).scrub("x abcdef y abc")
        self.assertEqual(text, f"x {REDACTED} y {REDACTED}")
        self.assertNotIn("def", text)

    def test_secret_inside_a_url(self) -> None:
        url = "https://api.example.test/bot123:SECRETVALUE/send?key=SECRETVALUE"
        text = Scrubber(["SECRETVALUE"]).scrub(f"HTTPError for {url}")
        self.assertNotIn("SECRETVALUE", text)
        self.assertIn(f"/bot123:{REDACTED}/send?key={REDACTED}", text)


class ScrubbingFormatterTests(unittest.TestCase):
    def _logger(self, scrubber: Scrubber) -> tuple[logging.Logger, io.StringIO]:
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(ScrubbingFormatter(scrubber, "%(levelname)s %(message)s"))
        logger = logging.Logger("test.scrub")
        logger.addHandler(handler)
        return logger, stream

    def test_scrubs_message_and_arguments(self) -> None:
        logger, stream = self._logger(Scrubber(["s3cret"]))
        logger.warning("call %s failed", "https://x.test/?k=s3cret")
        self.assertEqual(stream.getvalue(), f"WARNING call https://x.test/?k={REDACTED} failed\n")

    def test_scrubs_exceptions(self) -> None:
        logger, stream = self._logger(Scrubber(["s3cret"]))
        try:
            raise RuntimeError("bad token s3cret")
        except RuntimeError:
            logger.exception("boom")
        self.assertNotIn("s3cret", stream.getvalue())
        self.assertIn(f"bad token {REDACTED}", stream.getvalue())


if __name__ == "__main__":
    unittest.main()
