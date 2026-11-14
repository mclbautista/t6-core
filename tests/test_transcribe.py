import tempfile
import unittest
from pathlib import Path

import httpx
from elevenlabs.core.api_error import ApiError

from t6.transcribe import render_srt
from t6.transcribe.adapter import transcribe
from t6.transcribe.testing import FakeElevenLabs


class AdapterTests(unittest.TestCase):
    def test_retries_and_parameters(self) -> None:
        data = {"text": "Hi", "words": []}
        for failure in (ApiError(status_code=429), ApiError(status_code=500), httpx.ConnectError("offline")):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                wav = Path(directory) / "input.wav"
                wav.write_bytes(b"synthetic")
                client = FakeElevenLabs([failure, data])
                delays: list[float] = []
                self.assertEqual(transcribe(wav, client, language="en", sleep=delays.append), data)
                self.assertEqual(delays, [10])
                self.assertEqual([call["file"] for call in client.calls], [b"synthetic"] * 2)
                self.assertEqual(client.calls[0]["language_code"], "en")
                self.assertEqual(client.calls[0]["secondary_language_codes"], ["eng"])
                self.assertEqual(client.calls[0]["request_options"], {"max_retries": 0})
                self.assertEqual(client.calls[0]["model_id"], "scribe_v3")
                self.assertTrue(client.calls[0]["diarize"])
                self.assertFalse(client.calls[0]["tag_audio_events"])

    def test_final_error_and_nonretryable_error(self) -> None:
        for status, count, delays_expected in ((500, 3, [10, 20]), (400, 1, [])):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                wav = Path(directory) / "input.wav"
                wav.write_bytes(b"audio")
                errors = [ApiError(status_code=status) for _ in range(count)]
                client = FakeElevenLabs(errors)
                delays: list[float] = []
                with self.assertRaises(ApiError) as caught:
                    transcribe(wav, client, sleep=delays.append)
                self.assertIs(caught.exception, errors[-1])
                self.assertEqual(len(client.calls), count)
                self.assertEqual(delays, delays_expected)

    def test_old_sdk_error(self) -> None:
        class OldClient:
            speech_to_text = None

            def __init__(self) -> None:
                self.speech_to_text = self

            def convert(self) -> None:
                pass

        with tempfile.TemporaryDirectory() as directory:
            wav = Path(directory) / "input.wav"
            wav.touch()
            with self.assertRaisesRegex(RuntimeError, r"t6\[transcribe\]"):
                transcribe(wav, OldClient())


class SrtTests(unittest.TestCase):
    def test_render_and_offsets(self) -> None:
        words = [
            {"text": "A" * 50, "start": 0.0, "end": 0.5, "type": "word", "speaker_id": "speaker_0"},
            {"text": " ", "type": "spacing"},
            {"text": "hello", "start": 0.5, "end": 1.0, "type": "word", "speaker_id": "speaker_0"},
            {"text": " ", "type": "spacing"},
            {"text": "Hi!", "start": 2.0, "end": 3.0, "type": "word", "speaker_id": "speaker_1"},
        ]
        data = {"words": words}
        plain = render_srt(data)
        self.assertEqual(plain.count(" --> "), 2)
        self.assertTrue(all(len(line) <= 42 for cue in plain.strip().split("\n\n") for line in cue.splitlines()[2:]))
        self.assertIn("00:00:02,000 --> 00:00:03,000\nHi!", plain)
        shifted = render_srt(data, offset="01:00:00:12")
        self.assertIn("01:00:00,500 --> 01:00:01,500", shifted)
        self.assertIn("01:00:02,500 --> 01:00:03,500", shifted)
        self.assertEqual(shifted, render_srt(data, offset=3600.5))
        self.assertEqual(render_srt({"words": []}), "")
