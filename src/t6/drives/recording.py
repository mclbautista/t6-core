"""Admission of recording-channel content under ADR 0009."""
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RecordingInput:
    text: str
    photo: bytes


def recording_input(
    chat_id: int, text: str, photo: bytes, *, recording_chat_id: int | None,
    topic_id: int | None = None, recording_topic_id: int | None = None,
) -> RecordingInput | None:
    """Admit content only from the configured chat and, when set, topic."""
    if recording_chat_id is None or chat_id != recording_chat_id:
        return None
    if recording_topic_id is not None and topic_id != recording_topic_id:
        return None
    return RecordingInput(text, photo)
