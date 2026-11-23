"""ADR 0009's content boundary; capture persistence and resolution follow separately."""
import unittest
from pathlib import Path
import tempfile

from t6.drives.recording import recording_input


class RecordingChannelTests(unittest.TestCase):
    def test_only_the_configured_chat_admits_text_and_photo(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            photo = Path(directory) / 'synthetic-photo'
            photo.write_bytes(b'synthetic drive photo')
            for configured in (None, -900001, -900002):
                for chat in (-900001, -900002, -900003):
                    with self.subTest(configured=configured, chat=chat):
                        admitted = recording_input(
                            chat, 'IN SHELF', photo.read_bytes(),
                            recording_chat_id=configured,
                        )
                        if chat == configured:
                            assert admitted is not None
                            self.assertEqual(admitted.text, 'IN SHELF')
                            self.assertEqual(admitted.photo, b'synthetic drive photo')
                        else:
                            self.assertIsNone(admitted)

    def test_only_the_configured_topic_admits_text_and_photo(self) -> None:
        for topic in (4801, 4802, None):
            with self.subTest(topic=topic):
                admitted = recording_input(
                    -900001, 'IN SHELF', b'synthetic drive photo', topic_id=topic,
                    recording_chat_id=-900001, recording_topic_id=4801,
                )
                if topic == 4801:
                    assert admitted is not None
                    self.assertEqual(admitted.text, 'IN SHELF')
                    self.assertEqual(admitted.photo, b'synthetic drive photo')
                else:
                    self.assertIsNone(admitted)
