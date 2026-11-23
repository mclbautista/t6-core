"""Photo reads through the typed task and fake provider; all evidence is invented."""

import json
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from pydantic import JsonValue

from t6.llm import Image
from t6.llm.drive_photo import read_drive_photo
from t6.llm.drive_photo_evals import CASES, OFFLINE_REPLIES, grade_photo
from t6.llm.testing import FakeProvider


def photo(directory: str) -> Image:
    # Build a synthetic blank PNG at test time, never a real photo.
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    data = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\xff\xff\xff")) + chunk(b"IEND", b""))
    path = Path(directory) / "synthetic.png"
    path.write_bytes(data)
    return Image(path.read_bytes())


class DrivePhotoTests(unittest.TestCase):
    def test_synthetic_eval_cases_cover_one_several_and_unreadable(self) -> None:
        self.assertEqual({case.id for case in CASES}, {"one-drive", "several-drives", "unreadable"})
        for case in CASES:
            with self.subTest(case=case.id), tempfile.TemporaryDirectory() as directory:
                self.assertEqual(case.provenance.kind, "synthetic")
                self.assertTrue(case.expected_properties)
                result = read_drive_photo(photo(directory), provider=FakeProvider().reply(OFFLINE_REPLIES[case.id]))
                output: JsonValue = {"drives": [drive.model_dump() for drive in result.drives]}
                self.assertTrue(grade_photo(case, output).passed)
                self.assertFalse(grade_photo(case, {"drives": [
                    {"label": "SYNTHETIC WRONG", "serial": None, "confidence": 0.9},
                ]}).passed)
                if case.id != "unreadable":
                    self.assertFalse(grade_photo(case, {"drives": []}).passed)
                self.assertFalse(grade_photo(case, {"drives": "malformed"}).passed)

    def test_unreadable_photo_returns_no_drives_with_a_reason(self) -> None:
        result = read_drive_photo(Image(b"synthetic"), provider=FakeProvider().reply('{"drives": []}'))
        self.assertEqual(result.drives, [])
        self.assertEqual(result.reason, "unreadable")

    def test_retention_policy_refuses_before_sending_and_failures_have_no_guesses(self) -> None:
        unknown = FakeProvider(retention="unknown")
        scripts = [(unknown, "policy"), (FakeProvider().timeout(), "timeout"),
                   (FakeProvider().api_error(), "api_error"), (FakeProvider().reply(None), "empty")]
        for fake, reason in scripts:
            with self.subTest(reason=reason):
                result = read_drive_photo(Image(b"synthetic"), provider=fake)
                self.assertEqual(result.drives, [])
                self.assertEqual(result.reason, reason)
        self.assertEqual(unknown.calls, [])

    def test_malformed_answers_discard_the_entire_result_with_a_reason(self) -> None:
        valid = {"label": "SYNTHETIC MASTER 01", "serial": None, "confidence": 0.9}
        invalid_drives = [
            {**valid, "confidence": -0.1}, {**valid, "confidence": 1.1},
            {**valid, "confidence": "0.9"}, {**valid, "confidence": True},
            {**valid, "confidence": float("nan")}, {**valid, "confidence": float("inf")},
            {**valid, "label": None}, {**valid, "label": ""},
            {**valid, "label": "   "}, {**valid, "serial": 123},
            {**valid, "guessed_label": "SYNTHETIC GUESS"},
            {"label": "SYNTHETIC MASTER 01", "confidence": 0.9},
        ]
        replies = ["not JSON", "[]", "{}", '{"drives": null}']
        replies += [json.dumps({"drives": [valid, invalid]}) for invalid in invalid_drives]
        replies += [json.dumps({"drives": [valid], "extra": "unrequested"})]
        for reply in replies:
            with self.subTest(reply=reply):
                result = read_drive_photo(Image(b"synthetic"), provider=FakeProvider().reply(reply))
                self.assertEqual(result.drives, [])
                self.assertIn(result.reason, ("invalid_json", "schema_violation"))

    def test_reads_several_drives_without_normalizing_label_or_serial(self) -> None:
        answer = {"drives": [
            {"label": "SYNTHETIC MASTER 01", "serial": "FAKE-SN-001", "confidence": 0.98},
            {"label": "Synthetic backup 02", "serial": None, "confidence": 0.75},
            {"label": None, "serial": "fake-sn-003", "confidence": 0.9},
        ]}
        fake = FakeProvider().reply(json.dumps(answer))
        with tempfile.TemporaryDirectory() as directory:
            image = photo(directory)
            result = read_drive_photo(image, provider=fake)
        self.assertEqual([drive.model_dump() for drive in result.drives], answer["drives"])
        self.assertEqual(result.reason, "")
        self.assertEqual(fake.calls[0].messages[-1]["image"], image)
        self.assertTrue(fake.calls[0].zdr)
        self.assertEqual(result.trace.prompt_tag, "drive-photo-read-v1")


if __name__ == "__main__":
    unittest.main()
